"""Test-only helpers for a disposable Compose project."""

import json
import os
import re
import socket
import subprocess
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ghostdeploy.config import Config, validate
from ghostdeploy.core import GhostError, Runner, env_without
from ghostdeploy.runtime import Host

ADDON = "ghostdeploy_test_addon"
ATTACHMENT = b"GhostDeploy recovery fixture\x00\x01\xff"


class Cluster:
    """Prove cluster ownership before any setup SQL."""

    def __init__(self, runner: Runner, token: str):
        if (
            not re.fullmatch(r"[0-9a-f]{32}", token)
            or not Path("/fixture/.disposable-odoo-image").is_file()
            or os.geteuid() == 0
        ):
            raise GhostError("Use the disposable Compose runner as the Odoo account.")
        self.runner = runner
        self.password = uuid4().hex
        self.created: set[str] = set()
        self.connection = {
            **env_without("PG"),
            "PGHOST": "database",
            "PGPORT": "5432",
            "PGCONNECT_TIMEOUT": "5",
        }
        self.connection.pop("GHOSTDEPLOY_FIXTURE_TOKEN", None)
        self.admin = {
            **self.connection,
            "PGUSER": "ghost_fixture_admin",
            "PGPASSWORD": token,
            "PGDATABASE": "ghostdeploy_fixture",
        }
        if (
            self.sql("SELECT token FROM public.ghostdeploy_fixture_marker", self.admin)
            != token.encode()
        ):
            raise GhostError("PostgreSQL did not prove ownership of the disposable project.")
        print("PostgreSQL " + self.sql("SHOW server_version", self.admin).decode(), flush=True)
        self.sql(
            f"CREATE ROLE ghost_fixture_odoo LOGIN NOSUPERUSER NOCREATEDB PASSWORD '{self.password}'",
            self.admin,
        )

    def sql(self, query: str, connection: dict[str, str]) -> bytes:
        return self.runner.run(
            ["psql", "-X", "-w", "-At", "-v", "ON_ERROR_STOP=1", "-c", query],
            label="Fixture PostgreSQL query",
            env=connection,
        ).strip()

    def database(self, name: str, *, template: str | None = None) -> str:
        if not re.fullmatch(r"ghostdeploy_test_[a-f0-9]{12}_[a-z]+", name) or name in self.created:
            raise GhostError("The database is outside the fixture's newly created namespace.")
        if template is not None and template not in self.created:
            raise GhostError("The template was not created by this fixture.")
        self.sql(
            f"CREATE DATABASE {name} OWNER ghost_fixture_odoo"
            + (f" TEMPLATE {template}" if template else ""),
            self.admin,
        )
        self.created.add(name)
        owner = self.sql(
            "SELECT pg_get_userbyid(datdba) FROM pg_catalog.pg_database WHERE datname = current_database()",
            self.role(name),
        )
        if owner != b"ghost_fixture_odoo":
            raise GhostError("Database ownership does not match the disposable role.")
        return name

    def role(self, name: str) -> dict[str, str]:
        if name not in self.created:
            raise GhostError("The database was not created by this fixture.")
        return {
            **self.connection,
            "PGUSER": "ghost_fixture_odoo",
            "PGPASSWORD": self.password,
            "PGDATABASE": name,
        }


class TracedRunner(Runner):
    def __init__(self):
        self.events: list[tuple[str, str]] = []

    def run(self, args, *, label, **kwargs):
        if args[0] in {"pg_dump", "pg_restore"}:
            self.events.append((args[0], label.rsplit(" ", 1)[-1]))
        elif args[0] == "/usr/bin/odoo" and "-u" in args:
            assert args[args.index("-u") + 1] == ADDON
            self.events.append(("upgrade", args[args.index("-d") + 1]))
        elif args[0] == "git" and "merge" in args:
            self.events.append(("sync", args[-1]))
        return super().run(args, label=label, **kwargs)


class ProcessHost(Host):
    """Replace systemd and sudo with a real private child process in the container."""

    def __init__(self, config: Config, runner: TracedRunner):
        super().__init__(config, runner)
        self.process: subprocess.Popen | None = None
        self.runner = runner

    def preflight(self) -> None:
        self.require_identity()
        self.read_odoo_config()
        self.health()

    def systemd(self) -> dict[str, str]:
        if self.process is not None and self.process.poll() is None:
            return {"ActiveState": "active", "MainPID": str(self.process.pid)}
        return {"ActiveState": "inactive", "MainPID": "0"}

    def service(self, action: str) -> None:
        self.runner.events.append((action, self.odoo.unit))
        if action == "start":
            self.require_inactive()
            self.process = subprocess.Popen(
                [*self.odoo_command(), "-c", self.odoo.config_file],
                env=self.odoo_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif action == "stop":
            self.stop()
        else:
            raise AssertionError(f"Unexpected fixture service operation: {action}")

    def odoo_env(self) -> dict[str, str]:
        env = super().odoo_env()
        env.pop("GHOSTDEPLOY_FIXTURE_TOKEN", None)
        return env

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
                raise

    @contextmanager
    def running(self):
        try:
            self.service("start")
            self.health()
            yield self
        finally:
            self.stop()


def make_host(
    root: Path, checkout: Path, cluster: Cluster, series: str, dbs: list[str]
) -> ProcessHost:
    root.mkdir()
    for name in ("data", "backups", "state"):
        (root / name).mkdir(mode=0o700)
    if any(db not in cluster.created for db in dbs):
        raise GhostError("The Odoo configuration can only name databases created by the fixture.")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config_file = root / "odoo.conf"
    config_file.touch(mode=0o600)
    config_file.write_text(
        "[options]\n"
        f"addons_path = {checkout}\ndata_dir = {root / 'data'}\nlogfile = {root / 'odoo.log'}\n"
        f"db_host = database\ndb_port = 5432\ndb_user = ghost_fixture_odoo\ndb_password = {cluster.password}\n"
        f"db_name = {','.join(dbs)}\nhttp_interface = 127.0.0.1\nhttp_port = {port}\n"
        "list_db = False\nworkers = 0\nmax_cron_threads = 0\n",
        encoding="utf-8",
    )
    config = validate(
        {
            "schema_version": 1,
            "project": {"name": "odoo_fixture"},
            "odoo": {
                "version": series,
                "service": f"fixture-{uuid4().hex[:12]}",
                "service_user": "odoo",
                "executable": "/usr/bin/odoo",
                "config_file": str(config_file),
                "addons_path": [str(checkout)],
            },
            "repository": {"checkout_path": str(checkout)},
            "deployment": {
                "environment": "development",
                "databases": dbs,
                "command_timeout_seconds": 120,
                "state_path": str(root / "state"),
                "backup": {"path": str(root / "backups"), "min_free_mib": 16},
                "health_check": {
                    "url": f"http://127.0.0.1:{port}/web/health",
                    "timeout_seconds": 90,
                },
            },
        }
    )
    host = ProcessHost(config, TracedRunner())
    host.read_odoo_config()
    return host


def shell(host: ProcessHost, database: str, script: str) -> dict:
    if database not in host.config.deployment.databases:
        raise GhostError("The Odoo shell is limited to this fixture's configured databases.")
    # A single exec line works with both Odoo's pipe and interactive console modes.
    process = subprocess.run(
        [
            *host.odoo_command(),
            "shell",
            "-c",
            host.odoo.config_file,
            "-d",
            database,
            "--no-http",
            "--shell-interface",
            "python",
        ],
        input=f"exec({script!r})\n",
        text=True,
        capture_output=True,
        env=host.odoo_env(),
        timeout=120,
    )
    match = re.search(r"GHOSTDEPLOY_FIXTURE_RESULT=({[^\r\n]+})", process.stdout)
    if process.returncode or not match:
        raise GhostError(
            f"Fixture Odoo shell did not return an application result for {database}."
        )
    return json.loads(match[1])
