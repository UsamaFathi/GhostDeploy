"""Exercise GhostDeploy's real systemd and sudo operations as the fixture user."""

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import patch

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).with_name("lib")))

from ghostdeploy.cli import app  # noqa: E402
from ghostdeploy.config import validate  # noqa: E402
from ghostdeploy.core import CommandError, GhostError, Runner  # noqa: E402
from ghostdeploy.runtime import DatabaseState, Host  # noqa: E402

DB = "fixture_db"
ADDON = "fixture_addon"


class SyntheticDatabaseRunner(Runner):
    """Pass every command through except synthetic PostgreSQL operations."""

    def run(self, args, *, label, **kwargs):
        if args[0] == "psql":
            assert label == f"Estimate backup size for {DB}"
            return b"1024"
        if args[0] == "pg_dump":
            assert label == f"Back up {DB}"
            Path(args[-1]).write_bytes(b"synthetic snapshot")
            return b""
        if args[0] == "pg_restore":
            assert label == f"Verify PostgreSQL archive for {DB}"
            assert Path(args[-1]).read_bytes() == b"synthetic snapshot"
            return b""
        return super().run(args, label=label, **kwargs)


class SyntheticDatabaseHost(Host):
    def __init__(self, config, runner, mode: Path):
        super().__init__(config, runner)
        self.mode = mode
        self.version = "19.0.1.0"

    def snapshot_databases(self) -> dict[str, DatabaseState]:
        return {DB: DatabaseState(42, ("synthetic", "", "1"), {ADDON: self.version})}

    def require_database_role(self, database: str) -> None:
        assert database == DB

    def upgrade(self, database: str, modules: tuple[str, ...]) -> None:
        assert database == DB and modules == (ADDON,)
        self.require_inactive()
        self.version = "19.0.2.0"
        self.mode.write_text("failed-start")


def expect_error(call, message: str) -> GhostError:
    try:
        call()
    except GhostError as error:
        assert message in str(error), str(error)
        return error
    raise AssertionError(f"Expected failure containing {message!r}.")


def git(checkout: Path, *args: str) -> str:
    return Runner().run(["git", *args], cwd=checkout, label=f"Git {args[0]}").decode().strip()


def revisions(checkout: Path) -> tuple[str, str]:
    checkout.mkdir()
    git(checkout, "init", "-b", "main")
    git(checkout, "config", "user.name", "GhostDeploy fixture")
    git(checkout, "config", "user.email", "fixture@example.invalid")
    addon = checkout / ADDON
    addon.mkdir()
    (addon / "__manifest__.py").write_text("{'name': 'Fixture', 'version': '1.0'}\n")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "Fixture base")
    base = git(checkout, "rev-parse", "HEAD")
    (addon / "__manifest__.py").write_text("{'name': 'Fixture', 'version': '2.0'}\n")
    git(checkout, "commit", "-am", "Fixture upgrade")
    target = git(checkout, "rev-parse", "HEAD")
    git(checkout, "update-ref", "refs/remotes/origin/main", target)
    git(checkout, "reset", "--hard", base)
    return base, target


def verify_lifecycle(host: Host, mode: Path) -> None:
    host.require_identity()
    host.require_service_permissions()
    for command in (
        ["/usr/bin/systemctl", "restart", host.odoo.unit],
        ["/usr/bin/systemctl", "start", host.odoo.unit + "-unowned"],
        ["/usr/bin/true"],
    ):
        denied = subprocess.run(
            ["/usr/bin/sudo", "-n", "-l", "--", *command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        assert denied.returncode != 0
        print(f"Denied sudo policy probe {command[0]} {command[1:]} exit={denied.returncode}")

    mode.write_text("healthy")
    host.service("start")
    first = host.health()
    assert first.http_status == 200
    host.service("stop")
    host.require_inactive()
    assert not Path(f"/proc/{first.process_pid}").exists()
    print(f"PASS synthetic unit start/stop exit=0, pid={first.process_pid}, stopped=inactive")

    mode.write_text("bad-health")
    host.service("start")
    expect_error(host.health, "health check before the timeout")
    state = host.systemd()
    assert state["ActiveState"] == "active"
    pid = int(state["MainPID"])
    assert pid != first.process_pid
    os.kill(pid, signal.SIGTERM)
    deadline = monotonic() + 6
    while Path(f"/proc/{pid}").exists() and monotonic() < deadline:
        sleep(0.05)
    assert not Path(f"/proc/{pid}").exists()
    host.service("stop")
    host.require_inactive()
    print(f"PASS HTTP 503 rejected, SIGTERM removed pid={pid}, stop exit=0, state=inactive")

    mode.write_text("startup-timeout")
    started = monotonic()
    error = expect_error(lambda: host.service("start"), "exited with status")
    state = host.systemd()
    assert isinstance(error, CommandError) and error.returncode != 0
    assert state["ActiveState"] == "failed" and state["MainPID"] == "0"
    result = host.runner.run(
        ["/usr/bin/systemctl", "show", "--property=Result", "--value", host.odoo.unit],
        label="Inspect fixture timeout",
    )
    assert result.strip() == b"timeout" and 2 <= monotonic() - started < 15
    host.service("stop")
    host.require_inactive()
    print(f"PASS startup timeout exit={error.returncode}, Result=timeout, MainPID=0")


def verify_recovery(host: SyntheticDatabaseHost, work: Path, base: str, target: str) -> None:
    import io
    from contextlib import redirect_stdout

    host.mode.write_text("healthy")
    host.service("start")
    config = work / "config.json"
    config.write_text(host.config.model_dump_json())
    args = [
        "deploy",
        "--execute",
        "--config",
        str(config),
        "--target-sha",
        target,
        "--upgrade",
        ADDON,
        "--approve-review",
        "--output",
        "json",
    ]

    def deploy() -> tuple[int, dict]:
        output = io.StringIO()
        with (
            patch("ghostdeploy.cli.Host", return_value=host),
            patch("ghostdeploy.cli.Runner", return_value=host.runner),
            redirect_stdout(output),
        ):
            code = app(args=args, standalone_mode=False)
        return int(code or 0), json.loads(output.getvalue())

    code, report = deploy()
    assert (code, report["status"], report["phase"]) == (1, "failed", "starting_service"), report
    assert (report["base_sha"], report["target_sha"]) == (base, target)
    assert report["verified_databases"] == [DB] and report["in_progress_database"] is None
    assert "exited with status" in report["error"] and "Odoo is stopped" in report["recovery"]
    assert host.version == "19.0.2.0" and git(work / "addons", "rev-parse", "HEAD") == target
    backup = report["backups"][0]
    assert (backup["database"], backup["base_sha"], backup["target_sha"]) == (DB, base, target)
    assert Path(backup["dump"]["path"]).read_bytes() == b"synthetic snapshot"
    pending = work / "state" / f"{host.odoo.unit}.pending.json"
    assert json.loads(pending.read_text()) == {k: v for k, v in report.items() if k != "ok"}
    retry, error = deploy()
    assert retry == 1 and "unfinished deployment" in error["error"] and pending.is_file()
    host.require_inactive()
    print(
        "PASS real systemd startup failure: deploy and retry exit=1, journal preserved, unit inactive"
    )


def main(root: Path) -> None:
    work = root / "work"
    assert os.geteuid() != 0 and root.parent == Path("/run")
    assert work.stat().st_uid == os.geteuid() and root.stat().st_uid == 0
    base, target = revisions(work / "addons")
    for folder in ("data", "backup", "state"):
        (work / folder).mkdir(mode=0o700)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    (work / "port").write_text(str(port))
    config = validate(
        {
            "schema_version": 1,
            "project": {"name": "systemd_fixture"},
            "odoo": {
                "version": "19.0",
                "service": root.name,
                "service_user": root.name,
                "executable": str(root / "service.py"),
                "interpreter": "/usr/bin/python3",
                "config_file": str(root / "fixture.conf"),
                "addons_path": [str(work / "addons")],
            },
            "repository": {"checkout_path": str(work / "addons")},
            "deployment": {
                "environment": "development",
                "databases": [DB],
                "state_path": str(work / "state"),
                "backup": {"path": str(work / "backup"), "min_free_mib": 1},
                "health_check": {"url": f"http://127.0.0.1:{port}/health", "timeout_seconds": 2},
            },
        }
    )
    host = SyntheticDatabaseHost(config, SyntheticDatabaseRunner(), work / "mode")
    verify_lifecycle(host, host.mode)
    verify_recovery(host, work, base, target)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
