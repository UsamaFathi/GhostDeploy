"""Executed only inside the dedicated Odoo fixture image with its isolated PostgreSQL container."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from ghostdeploy.config import validate
from ghostdeploy.core import Runner, env_without
from ghostdeploy.runtime import Host


def check_upgrade(root: Path, series: str) -> None:
    runner = Runner()
    env = {**env_without("PG"), "PGHOST": "database", "PGPORT": "5432", "PGCONNECT_TIMEOUT": "5"}
    password = uuid4().hex
    admin = {
        **env,
        "PGUSER": "ghost_fixture_admin",
        "PGPASSWORD": "synthetic-fixture-admin-password",
        "PGDATABASE": "postgres",
    }
    dbs = [f"company_{letter}_{uuid4().hex}" for letter in "abc"]

    def scalar(sql: str, connection: dict[str, str]) -> bytes:
        return runner.run(
            ["psql", "-X", "-w", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
            label="Odoo fixture SQL",
            env=connection,
        ).strip()

    scalar(
        f"CREATE ROLE ghost_fixture_odoo LOGIN NOSUPERUSER NOCREATEDB PASSWORD '{password}'", admin
    )
    for database in dbs:
        scalar(f"CREATE DATABASE {database} OWNER ghost_fixture_odoo", admin)

    checkout = root / "custom"
    addon = checkout / "attendance_fixture"
    addon.mkdir(parents=True)
    (addon / "__init__.py").touch()
    store = root / "data"
    store.mkdir()
    config_file = root / "odoo.conf"
    config_file.write_text(
        "[options]\n"
        f"addons_path = {checkout}\ndata_dir = {store}\n"
        f"db_host = database\ndb_port = 5432\ndb_user = ghost_fixture_odoo\ndb_password = {password}\n"
        "list_db = False\nworkers = 0\nmax_cron_threads = 0\n",
        encoding="utf-8",
    )
    config_file.chmod(0o600)

    def revision(minor: int) -> None:
        (addon / "__manifest__.py").write_text(
            repr(
                {
                    "name": "GhostDeploy fixture",
                    "version": f"1.{minor}",
                    "depends": ["base"],
                    "data": ["data.xml"],
                    "license": "LGPL-3",
                }
            ),
            encoding="utf-8",
        )
        (addon / "data.xml").write_text(
            '<odoo><record id="integration_value" model="ir.config_parameter">'
            '<field name="key">ghostdeploy.integration</field>'
            f'<field name="value">revision-{minor}</field></record></odoo>',
            encoding="utf-8",
        )

    config = validate(
        {
            "schema_version": 1,
            "project": {"name": "odoo_fixture"},
            "odoo": {
                "version": series,
                "service": "fixture",
                "executable": "/usr/bin/odoo",
                "config_file": str(config_file),
                "addons_path": [str(checkout)],
            },
            "repository": {"checkout_path": str(checkout)},
            "deployment": {"environment": "development", "databases": dbs},
        }
    )
    host = Host(config, runner)
    host.read_odoo_config()
    revision(0)
    for index, database in enumerate(dbs):
        # Installation is fixture setup. Product execution only invokes explicit upgrades.
        runner.run(
            [
                "/usr/bin/odoo",
                "-c",
                str(config_file),
                "-d",
                database,
                "-i",
                "attendance_fixture" if index < 2 else "base",
                "--stop-after-init",
                "--no-http",
            ],
            label=f"Initialize synthetic Odoo {series} database {index + 1}",
            timeout=300,
        )
    before = host.snapshot_databases()
    assert ["attendance_fixture" in before[database].versions for database in dbs] == [
        True,
        True,
        False,
    ]
    revision(1)
    host.prepare_upgrades({"attendance_fixture"})
    for database in dbs[:2]:
        host.require_database_role(database)
        host.upgrade(database, ("attendance_fixture",))
    after = host.snapshot_databases()
    for database in dbs[:2]:
        assert after[database].versions["attendance_fixture"] == f"{series}.1.1"
        conn = {**env, **host.pg_env(database)}
        assert (
            scalar(
                "SELECT value FROM ir_config_parameter WHERE key = 'ghostdeploy.integration'", conn
            )
            == b"revision-1"
        )
    assert before[dbs[2]] == after[dbs[2]]
    print(f"Odoo {series}: two explicit upgrades and third database unchanged")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="odoo-fixture-") as directory:
        check_upgrade(Path(directory), os.environ["ODOO_TEST_VERSION"])
