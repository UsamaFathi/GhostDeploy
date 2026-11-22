import hashlib
import json
import os
import sys
import tarfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ghostdeploy.backup import Backups
from ghostdeploy.config import validate
from ghostdeploy.core import GhostError, Runner
from ghostdeploy.deploy import Executor, Journal, Report, deployment_lock, select_upgrades
from ghostdeploy.planning import Plan
from ghostdeploy.runtime import Host

BASE = "a" * 40
TARGET = "b" * 40
UPGRADES = {"company_a": ("attendance",), "company_b": ("attendance",)}
INSTALLED = {database: frozenset(modules) for database, modules in UPGRADES.items()}


class FakeRunner:
    def __init__(self, fail=None):
        self.fail = fail
        self.active = True
        self.updated = []
        self.pg_roles = b"t\n"
        self.pg_modules = b"attendance|installed\n"

    def run(self, args, *, label, env=None, **kwargs):
        if label == self.fail:
            raise GhostError(label + " failed")
        if label == "Inspect Odoo upgrade options":
            return b"  --skip-auto-install\n"
        if args[0] == "psql":
            assert env["PGDATABASE"] in UPGRADES
            if "pg_database_size" in args[-1]:
                return b"1000000\n"
            if "pg_roles" in args[-1]:
                return self.pg_roles
            return self.pg_modules
        if args[0] == "pg_dump":
            assert not self.active
            Path(args[-1]).write_bytes(f"archive of {env['PGDATABASE']}".encode())
        if args[0] == "pg_restore":
            assert args[1] == f"--file={os.devnull}"
            assert Path(args[-1]).exists()
        if args[0].endswith("/sudo"):
            self.active = args[3] == "start"
        if "-u" in args:
            assert not self.active
            assert args[args.index("-u") + 1] == "attendance"
            assert "--no-http" in args and "--max-cron-threads=0" in args
            self.updated.append(args[args.index("-d") + 1])
        return b""


class SimulatedHost(Host):
    def systemd(self):
        return {
            "ActiveState": "active" if self.runner.active else "inactive",
            "MainPID": "42" if self.runner.active else "0",
        }

    def health(self):
        if self.runner.fail == "health":
            raise GhostError("health-check timeout")
        assert self.runner.active

    def service_process(self, state):
        assert state["ActiveState"] == "active"


class SimulatedRepo:
    def __init__(self, fail):
        self.head = BASE
        self.fail = fail

    def clean_head(self):
        return self.head

    def sync(self, base, target):
        assert self.head == base
        if self.fail == "git":
            raise GhostError("Git sync failed")
        self.head = target


@pytest.fixture
def planned():
    return Plan(
        BASE,
        TARGET,
        (),
        ("attendance",),
        ("attendance",),
        tuple(UPGRADES),
        (),
        (),
        True,
        (),
        UPGRADES,
    )


def test_manual_policy_requires_all_explicit_installed_candidates(config_data, planned):
    config = validate(config_data)
    with pytest.raises(GhostError, match="Missing: attendance"):
        select_upgrades(config, planned, [], False)
    with pytest.raises(GhostError, match="only name candidates"):
        select_upgrades(config, planned, ["all"], False)
    assert select_upgrades(config, planned, ["attendance"], False) == UPGRADES
    reserved = replace(planned, installed_by_database={"company_a": ("all",), "company_b": ()})
    with pytest.raises(GhostError, match="reserves 'all'"):
        select_upgrades(config, reserved, ["all"], False)


@pytest.mark.parametrize(
    ("failure", "phase", "active", "checkout", "completed"),
    [
        (None, "complete", True, TARGET, ["company_a", "company_b"]),
        ("git", "synchronizing_code", False, BASE, []),
        ("Back up company_b", "creating_backups", True, BASE, []),
        ("Verify PostgreSQL archive for company_a", "creating_backups", True, BASE, []),
        ("Upgrade modules in company_b", "upgrading_database", False, TARGET, ["company_a"]),
        ("Start odoo19.service", "starting_service", False, TARGET, ["company_a", "company_b"]),
        ("health", "checking_health", False, TARGET, ["company_a", "company_b"]),
    ],
)
def test_operational_lifecycle(
    tmp_path, monkeypatch, config_data, planned, failure, phase, active, checkout, completed
):
    configuration = validate(config_data)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    fake = FakeRunner(failure)
    host = SimulatedHost(configuration, fake)
    store = tmp_path / "filestore" / "company_a" / "ab"
    store.mkdir(parents=True)
    (store / "object").write_bytes(b"attachment")
    host.filestore = tmp_path / "filestore"
    repo = SimulatedRepo(failure)
    backups = Backups(host)
    backups.root = backup
    if os.name == "nt":
        monkeypatch.setattr("ghostdeploy.backup.private_directory", lambda path: None)
    report = Report(
        "test_id", "odoo19.service", datetime.now(UTC).isoformat(), BASE, TARGET, upgrades=UPGRADES
    )
    journal = Journal(state, report)
    journal.record("prepared", create=True)
    Executor(configuration, fake, host).apply(journal, repo, planned, INSTALLED, backups)

    persisted = json.loads((state / "test_id.json").read_text())
    assert persisted["phase"] == phase
    assert persisted["upgraded_databases"] == completed
    assert fake.updated == completed
    assert fake.active is active
    assert repo.head == checkout
    assert journal.pending.exists() is (failure is not None and phase != "creating_backups")
    if phase == "upgrading_database":
        assert persisted["in_progress_database"] == "company_b"
    if failure is None:
        assert persisted["status"] == "succeeded"
        first, second = persisted["backups"]
        assert second["filestore"]["status"] == "absent_at_snapshot"
        with tarfile.open(first["filestore"]["path"]) as archive:
            assert archive.extractfile("filestore/ab/object").read() == b"attachment"
        dump = Path(first["dump"]["path"])
        assert hashlib.sha256(dump.read_bytes()).hexdigest() == first["dump"]["sha256"]
        if os.name == "posix":
            assert dump.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform != "linux", reason="Linux flock required")
def test_second_deployment_cannot_take_the_same_service_lock(tmp_path):
    with (
        deployment_lock(tmp_path, "odoo.service"),
        pytest.raises(GhostError, match="already running"),
        deployment_lock(tmp_path, "odoo.service"),
    ):
        pytest.fail("Second deployment acquired the service lock")
    with deployment_lock(tmp_path, "odoo.service"):
        assert (tmp_path / "odoo.service.lock").is_file()


def test_pg_role_and_allowlist(config_data, monkeypatch):
    monkeypatch.setenv("PGHOST", "untrusted.example.invalid")
    host = Host(validate(config_data), FakeRunner())
    host.pg_settings = {"PGHOST": "/run/postgresql"}
    assert host.pg_env("company_a")["PGHOST"] == "/run/postgresql"
    assert host.inspect_databases() == INSTALLED
    with pytest.raises(GhostError, match="outside"):
        host.pg_env("other_db")
    host.runner.pg_roles = b"f\n"
    with pytest.raises(GhostError, match="NOCREATEDB"):
        host.require_database_role("company_a")
    host.runner.pg_modules = b"attendance|to upgrade\n"
    with pytest.raises(GhostError, match="pending addon operations"):
        host.inspect_databases()


def test_systemd_unit_validation(config_data):
    host = Host(validate(config_data), FakeRunner())
    with pytest.raises(GhostError, match="missing, masked or unloaded"):
        host.systemd()


def test_doctor_reports_missing_odoo_binary(config_data):
    config_data["odoo"]["executable"] = "/ghostdeploy-test-fixture/absent-odoo-bin"
    result = Host(validate(config_data), FakeRunner()).inspect()
    executable = next(item for item in result if item.check == "Odoo executable")
    assert executable.ok is False
    assert "missing" in executable.detail


def test_shadowed_addon_is_rejected_and_runtime_options_are_detected(config_data, tmp_path):
    host = Host(validate(config_data), FakeRunner())
    host.external_addons = {tmp_path}
    host.prepare_upgrades({"attendance"})
    assert host.skip_auto_install
    (tmp_path / "attendance").mkdir()
    (tmp_path / "attendance" / "__manifest__.py").touch()
    with pytest.raises(GhostError, match="unmanaged Odoo addon root"):
        host.prepare_upgrades({"attendance"})


def test_health_timeout_is_reported_from_inactive_service(config_data, monkeypatch):
    host = Host(validate(config_data), FakeRunner())
    monkeypatch.setattr(host, "systemd", lambda: {"ActiveState": "inactive", "MainPID": "0"})
    times = iter((1.0, 100.0))
    monkeypatch.setattr("ghostdeploy.runtime.time.monotonic", lambda: next(times))
    with pytest.raises(GhostError, match="before the timeout"):
        host.health()


def test_command_errors_do_not_include_child_output():
    with pytest.raises(GhostError, match="exited with status 7") as exc:
        Runner().run(
            [sys.executable, "-c", "import sys; sys.stderr.write('secret'); sys.exit(7)"],
            label="fixture",
        )
    assert "secret" not in str(exc.value)
