"""Explicit opt-in. The test starts and owns a fresh loopback PostgreSQL cluster."""

import hashlib
import os
import shutil
import socket
import subprocess
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest

from ghostdeploy.backup import Backups
from ghostdeploy.config import validate
from ghostdeploy.core import CommandError, Runner, env_without
from ghostdeploy.runtime import Host

pytestmark = pytest.mark.skipif(
    os.environ.get("GHOSTDEPLOY_TEST_POSTGRESQL") != "1",
    reason="Set GHOSTDEPLOY_TEST_POSTGRESQL=1 to start a disposable cluster",
)

CLIENT = ["psql", "-X", "-w", "-At", "-v", "ON_ERROR_STOP=1", "-c"]


@pytest.fixture
def postgres(tmp_path):
    for tool in ("initdb", "pg_ctl", "psql", "pg_dump", "pg_restore"):
        if not shutil.which(tool):
            pytest.fail(f"Opted into PostgreSQL integration but {tool} is missing from PATH")
    if os.name == "posix" and os.geteuid() == 0:
        pytest.fail("Run the disposable PostgreSQL integration as a non-root account")
    cluster = tmp_path / "cluster"
    log = tmp_path / "postgres.log"
    password = uuid4().hex
    password_file = tmp_path / "init-secret"
    descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(password + "\n")
    env = env_without("PG")
    try:
        setup = subprocess.run(
            [
                "initdb",
                "-D",
                str(cluster),
                "-U",
                "gd_fixture_admin",
                "-A",
                "scram-sha-256",
                "--encoding=UTF8",
                "--locale=C",
                f"--pwfile={password_file}",
            ],
            env=env,
            check=False,
            capture_output=True,
            timeout=90,
        )
        if setup.returncode:
            pytest.fail(setup.stderr.decode(errors="replace").strip())
    finally:
        password_file.unlink()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (cluster / "postgresql.conf").open("a", encoding="utf-8") as config:
        config.write(
            f"\nlisten_addresses = '127.0.0.1'\nport = {port}\nunix_socket_directories = ''\n"
        )
    control = ["pg_ctl", "-D", str(cluster), "-w", "-t", "45"]
    try:
        started = subprocess.run(
            [*control, "-l", str(log), "start"],
            env=env,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        if started.returncode:
            pytest.fail(
                log.read_text(encoding="utf-8", errors="replace")[-3000:]
                if log.exists()
                else "Disposable pg_ctl start failed"
            )
        params = {
            "PGHOST": "127.0.0.1",
            "PGPORT": str(port),
            "PGCONNECT_TIMEOUT": "5",
            "PGUSER": "gd_fixture_admin",
            "PGPASSWORD": password,
            "PGDATABASE": "postgres",
        }
        directory = (
            Runner()
            .run(
                [*CLIENT, "SHOW data_directory"],
                label="Identify disposable cluster",
                env={**env, **params},
            )
            .decode()
            .strip()
        )
        assert Path(directory).resolve() == cluster.resolve()
        yield params
    finally:
        if (cluster / "postmaster.pid").is_file():
            subprocess.run(
                [*control, "-m", "fast", "stop"],
                env=env,
                check=True,
                capture_output=True,
                timeout=60,
            )


def test_pg_archive_restore_to_fresh_database(postgres, tmp_path, config_data, monkeypatch):
    source, destination, missing = [f"gd_{uuid4().hex}" for _ in range(3)]
    runner = Runner()
    env = {**env_without("PG"), **postgres}
    role_password = uuid4().hex

    def sql(query, *, database="postgres", params=env):
        return runner.run(
            [*CLIENT, query], label="Execute fixture SQL", env={**params, "PGDATABASE": database}
        ).strip()

    sql(f"CREATE ROLE gd_fixture_odoo LOGIN NOSUPERUSER NOCREATEDB PASSWORD '{role_password}'")
    sql(f"CREATE DATABASE {source} OWNER gd_fixture_odoo")
    sql(f"CREATE DATABASE {destination} OWNER gd_fixture_odoo")
    role = {**env, "PGUSER": "gd_fixture_odoo", "PGPASSWORD": role_password}
    sql(
        "CREATE TABLE ir_module_module (name text PRIMARY KEY, state text, latest_version text);"
        "INSERT INTO ir_module_module VALUES ('base','installed','19.0.1.0'), ('attendance','installed','19.0.2.0');"
        "CREATE TABLE attachment_fixture (id integer PRIMARY KEY, payload bytea);"
        "INSERT INTO attachment_fixture VALUES (1,decode('0011ff','hex'))",
        database=source,
        params=role,
    )
    config_data["deployment"]["databases"] = [source]
    config_data["deployment"]["backup"] = {"min_free_mib": 1}
    host = Host(validate(config_data), runner)
    host.pg_settings = {key: role[key] for key in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD")}
    host.require_database_role(source)
    state = host.snapshot_databases()[source]
    assert state.oid > 0 and state.versions == {"base": "19.0.1.0", "attendance": "19.0.2.0"}
    if os.name == "nt":
        # Production storage ownership checks require Linux. The temp directory is owned by this test.
        monkeypatch.setattr("ghostdeploy.backup.private_directory", lambda path: None)
    host.filestore = tmp_path / "filestore"
    attachments = {"ab/attachment": b"\x00\x11\xff", "ab/nested/صورة": b"\x19\x91" * 524_288}
    for relative, contents in attachments.items():
        item = host.filestore / source / relative
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_bytes(contents)
    backups = Backups(host)
    backups.root = tmp_path / "backups"
    backups.root.mkdir(mode=0o700)
    backups.check_space([source])
    (record,) = backups.create(uuid4().hex, "a" * 40, "b" * 40, [source])
    assert record["test_restore"] == "not_performed"
    dump = record["dump"]
    assert hashlib.sha256(Path(dump["path"]).read_bytes()).hexdigest() == dump["sha256"]
    assert (
        sql(
            "SELECT pg_catalog.to_regclass('public.attachment_fixture') IS NULL",
            database=destination,
            params=role,
        )
        == b"t"
    )
    runner.run(
        ["pg_restore", "--no-owner", "--exit-on-error", "--dbname", destination, dump["path"]],
        label="Restore to fresh fixture database",
        env=role,
    )
    assert (
        sql(
            "SELECT encode(payload,'hex') FROM attachment_fixture",
            database=destination,
            params=role,
        )
        == b"0011ff"
    )
    assert (
        sql(
            "SELECT latest_version FROM ir_module_module WHERE name = 'attendance'",
            database=destination,
            params=role,
        )
        == b"19.0.2.0"
    )
    with tarfile.open(record["filestore"]["path"]) as archive:
        assert {
            entry.name.removeprefix("filestore/"): archive.extractfile(entry).read()
            for entry in archive
            if entry.isfile()
        } == attachments

    config_data["deployment"]["databases"] = [missing]
    missing_host = Host(validate(config_data), runner)
    missing_host.pg_settings = host.pg_settings
    with pytest.raises(CommandError, match=f"Inspect installed modules in {missing}"):
        missing_host.snapshot_databases()
