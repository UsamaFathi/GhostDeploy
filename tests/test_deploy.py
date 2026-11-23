import hashlib
import json
import os
import subprocess
import sys
import tarfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ghostdeploy.backup import Backups
from ghostdeploy.config import validate
from ghostdeploy.core import CommandError, GhostError, Runner
from ghostdeploy.deploy import (
    Executor,
    Journal,
    Report,
    bind_state_directory,
    deployment_lock,
    select_upgrades,
)
from ghostdeploy.planning import Plan
from ghostdeploy.runtime import HealthEvidence, Host

BASE = "a" * 40
TARGET = "b" * 40
UPGRADES = {"company_a": ("attendance",), "company_b": ("attendance",)}
INSTALLED = {
    "company_a": frozenset({"attendance", "payroll"}),
    "company_b": frozenset({"attendance", "inventory"}),
    "company_c": frozenset({"inventory"}),
}
VERSIONS = {"attendance": "19.0.2.0"}


class FakeRunner:
    def __init__(self, fail=None):
        self.fail = fail
        self.active = True
        self.updated = []
        self.pg_roles = b"t\n"
        self.pending = False
        self.inspected = set()
        self.new_oid = False
        self.uninstalled = False
        self.available = {**INSTALLED, "company_unmanaged": frozenset({"attendance"})}

    def run(self, args, *, label, env=None, **kwargs):
        if label == self.fail:
            raise GhostError(label + " failed")
        if self.fail == "interrupted backup" and label == "Back up company_b":
            raise KeyboardInterrupt
        if self.fail == "unexpected stop" and label == "Stop odoo19.service":
            raise RuntimeError("secret internal details")
        if label == "Upgrade modules in company_b":
            if self.fail == "cancel upgrade":
                raise KeyboardInterrupt
            if self.fail == "timeout upgrade":
                raise CommandError(label, "timed out after 10 seconds")
        if label == "Inspect Odoo upgrade options":
            return b"  --skip-auto-install\n"
        if args[0] == "psql":
            database = env["PGDATABASE"]
            if database not in self.available:
                raise CommandError(label, "exited with status 2", 2)
            if "pg_database_size" in args[-1]:
                return b"1000000\n"
            if "pg_roles" in args[-1]:
                return self.pg_roles
            self.inspected.add(database)
            modules = self.available[database]
            if self.uninstalled and database == "company_a":
                modules = modules - {"attendance"}
            rows = [
                [
                    module,
                    "to upgrade" if self.pending else "installed",
                    VERSIONS[module]
                    if module == "attendance"
                    and database in self.updated
                    and self.fail != "no new version"
                    else "19.0.1.0",
                ]
                for module in sorted(modules)
            ]
            oid = list(self.available).index(database) + 100
            if self.new_oid and database == "company_b":
                oid += 100
            return json.dumps([oid, ["started", "", "5432"], rows]).encode()
        if args[0] == "pg_dump":
            assert not self.active
            Path(args[-1]).write_bytes(f"archive of {env['PGDATABASE']}".encode())
        if args[0] == "pg_restore":
            assert args[1] == f"--file={os.devnull}"
            assert Path(args[-1]).exists()
        if args[0].endswith("/sudo"):
            self.active = args[3] == "start"
            if self.active and self.fail == "interrupt after launch":
                raise KeyboardInterrupt
            if not self.active:
                self.new_oid = self.fail == "replacement database"
                self.uninstalled = self.fail == "uninstalled module"
                if self.fail == "missing database":
                    del self.available["company_b"]
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
        if self.runner.fail == "interrupt during health":
            raise KeyboardInterrupt
        if self.runner.fail == "health":
            raise GhostError("health-check timeout")
        assert self.runner.active
        return HealthEvidence(True, 42, None)

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
        if self.fail == "interrupt after sync":
            raise KeyboardInterrupt


@pytest.fixture
def planned():
    return Plan(
        BASE,
        TARGET,
        (),
        ("attendance",),
        ("attendance",),
        tuple(INSTALLED),
        (),
        (),
        True,
        (),
        {**UPGRADES, "company_c": ()},
        expected_versions=VERSIONS,
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
        ("interrupt after sync", "synchronizing_code", False, TARGET, []),
        ("Stop odoo19.service", "stopping_service", True, BASE, []),
        ("unexpected stop", "stopping_service", True, BASE, []),
        ("Back up company_b", "creating_backups", False, BASE, []),
        ("interrupted backup", "creating_backups", False, BASE, []),
        ("Verify PostgreSQL archive for company_a", "creating_backups", False, BASE, []),
        ("Inspect installed modules in company_b", "service_stopped", False, BASE, []),
        ("replacement database", "service_stopped", False, BASE, []),
        ("uninstalled module", "service_stopped", False, BASE, []),
        ("missing database", "service_stopped", False, BASE, []),
        ("Upgrade modules in company_a", "upgrading_database", False, TARGET, []),
        ("Upgrade modules in company_b", "upgrading_database", False, TARGET, ["company_a"]),
        ("timeout upgrade", "upgrading_database", False, TARGET, ["company_a"]),
        ("cancel upgrade", "upgrading_database", False, TARGET, ["company_a"]),
        ("no new version", "verifying_database", False, TARGET, ["company_a"]),
        ("Start odoo19.service", "starting_service", False, TARGET, ["company_a", "company_b"]),
        ("interrupt after launch", "starting_service", False, TARGET, ["company_a", "company_b"]),
        ("health", "checking_health", False, TARGET, ["company_a", "company_b"]),
        ("interrupt during health", "checking_health", False, TARGET, ["company_a", "company_b"]),
    ],
)
def test_operational_lifecycle(
    tmp_path, monkeypatch, config_data, planned, failure, phase, active, checkout, completed
):
    config_data["deployment"]["databases"].append("company_c")
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
        "test_id",
        "odoo19.service",
        datetime.now(UTC).isoformat(),
        BASE,
        TARGET,
        upgrades=UPGRADES,
        expected_versions=VERSIONS,
    )
    journal = Journal(state, report)
    journal.record("prepared", create=True)
    if failure == "Inspect installed modules in company_b":
        fake.fail = None
        snapshots = host.snapshot_databases()
        fake.fail = failure
    else:
        snapshots = host.snapshot_databases()
    Executor(configuration, fake, host).apply(journal, repo, planned, snapshots, backups)

    persisted = json.loads((state / "test_id.json").read_text())
    assert persisted["phase"] == phase
    assert persisted["upgraded_databases"] == completed
    assert fake.updated == completed
    assert fake.active is active
    assert repo.head == checkout
    assert journal.pending.exists() is (failure is not None)
    assert fake.inspected == set(INSTALLED)
    assert persisted["status"] == ("succeeded" if failure is None else "failed")
    assert datetime.fromisoformat(persisted["completed_at"]) >= datetime.fromisoformat(
        persisted["started_at"]
    )
    assert [
        step for step in persisted["completed_phases"] if step.startswith("database_verified:")
    ] == [f"database_verified:{name}" for name in persisted["verified_databases"]]
    assert ("service_checks_verified" in persisted["completed_phases"]) is (failure is None)
    assert "secret internal details" not in json.dumps(persisted)
    assert persisted["verified_databases"] == ([] if failure == "no new version" else completed)
    if phase == "upgrading_database":
        assert persisted["in_progress_database"] == ("company_b" if completed else "company_a")
    if failure in {"Back up company_b", "interrupted backup"}:
        assert [item["database"] for item in persisted["backups"]] == ["company_a"]
        assert "backups_verified" not in persisted["completed_phases"]
        assert (backup / "test_id" / "company_b.dump.partial").is_file()
    if failure is None:
        assert persisted["status"] == "succeeded"
        assert persisted["completed_phases"] == [
            "service_stopped",
            "backups_verified",
            "code_synchronized",
            "database_verified:company_a",
            "database_verified:company_b",
            "service_checks_verified",
        ]
        first, second = persisted["backups"]
        assert second["filestore"]["status"] == "absent_at_snapshot"
        assert first["test_restore"] == "not_performed"
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


@pytest.mark.skipif(sys.platform != "linux", reason="Linux namespace lock and flock required")
def test_crash_releases_kernel_lock_across_state_roots(tmp_path):
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    child = """from pathlib import Path
from sys import argv
from time import sleep
from ghostdeploy.deploy import deployment_lock
with deployment_lock(Path(argv[1]), 'fixture-odoo.service'):
    print('locked', flush=True)
    sleep(20)
"""
    with subprocess.Popen(
        [sys.executable, "-c", child, str(tmp_path)], stdout=subprocess.PIPE, text=True
    ) as process:
        try:
            assert process.stdout.readline() == "locked\n"
            with (
                pytest.raises(GhostError, match="already running"),
                deployment_lock(other, "fixture-odoo.service"),
            ):
                pytest.fail("Another state path bypassed the service lock")
        finally:
            process.kill()
    assert (tmp_path / "fixture-odoo.service.lock").exists()
    with deployment_lock(tmp_path, "fixture-odoo.service"):
        assert process.poll() is not None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux private state binding required")
def test_service_binding_refuses_alternate_recovery_history(tmp_path):
    original, other = tmp_path / "one", tmp_path / "two"
    original.mkdir(mode=0o700)
    other.mkdir(mode=0o700)
    bind_state_directory("fixture.service", original, home=tmp_path)
    (original / "fixture.service.pending.json").write_text(
        '{"phase":"upgrading_database"}', encoding="utf-8"
    )
    with pytest.raises(GhostError, match="bound to another state directory"):
        bind_state_directory("fixture.service", other, home=tmp_path)
    assert (original / "fixture.service.pending.json").is_file()
    bind_state_directory("fixture.service", original, home=tmp_path)


@pytest.mark.parametrize("phase", ["stopping_service", "finalizing"])
def test_disk_failure_keeps_previous_durable_transition(
    tmp_path, monkeypatch, config_data, planned, phase
):
    import errno

    from ghostdeploy import deploy
    from ghostdeploy.deploy import JournalError

    configuration = validate(config_data)
    fake = FakeRunner()
    host = SimulatedHost(configuration, fake)
    repo = SimulatedRepo(None)
    report = Report("test_id", "odoo19.service", datetime.now(UTC).isoformat(), BASE, TARGET)
    journal = Journal(tmp_path, report)
    journal.record("prepared", create=True)
    original = deploy.write_atomic

    def disk_full(path, content, **kwargs):
        if json.loads(content)["phase"] == phase:
            raise OSError(errno.ENOSPC, "no space left")
        original(path, content, **kwargs)

    monkeypatch.setattr(deploy, "write_atomic", disk_full)
    executor = Executor(configuration, fake, host)
    if phase == "finalizing":
        with pytest.raises(JournalError, match="finalizing"):
            executor.apply(
                journal,
                repo,
                replace(planned, restart_required=False),
                host.snapshot_databases(),
                Backups(host),
            )
        assert json.loads(journal.pending.read_text())["phase"] == "code_synchronized"
        assert not (tmp_path / "test_id.json").exists()
    else:
        executor.apply(journal, repo, planned, host.snapshot_databases(), Backups(host))
        assert report.status == "failed"
        assert json.loads(journal.pending.read_text())["phase"] == "prepared"
        assert fake.active and repo.head == BASE


def test_command_timeout_is_a_sanitized_failure():
    with pytest.raises(CommandError, match="timed out") as failure:
        Runner().run(
            [sys.executable, "-c", "import time; time.sleep(30)"], label="fixture", timeout=1
        )
    assert failure.value.returncode is None


def test_pg_role_and_allowlist(config_data, monkeypatch):
    monkeypatch.setenv("PGHOST", "untrusted.example.invalid")
    host = Host(validate(config_data), FakeRunner())
    host.pg_settings = {"PGHOST": "/run/postgresql"}
    assert host.pg_env("company_a")["PGHOST"] == "/run/postgresql"
    assert host.inspect_databases() == {
        database: INSTALLED[database] for database in ("company_a", "company_b")
    }
    with pytest.raises(GhostError, match="outside"):
        host.pg_env("other_db")
    host.runner.pg_roles = b"f\n"
    with pytest.raises(GhostError, match="NOCREATEDB"):
        host.require_database_role("company_a")
    host.runner.pending = True
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


def test_successful_http_health_is_not_application_validation(config_data, monkeypatch):
    config_data["deployment"]["health_check"] = {"url": "http://127.0.0.1:8069/web/health"}
    host = SimulatedHost(validate(config_data), FakeRunner())
    monkeypatch.setattr("ghostdeploy.runtime.build_opener", lambda *_: SuccessfulProbe())

    class SuccessfulProbe:
        @staticmethod
        def open(url, timeout):
            from contextlib import nullcontext
            from types import SimpleNamespace

            return nullcontext(SimpleNamespace(status=200))

    health = Host.health(host)
    assert health == HealthEvidence(True, 42, 200, application_verified=False)


def test_command_errors_do_not_include_child_output():
    with pytest.raises(GhostError, match="exited with status 7") as exc:
        Runner().run(
            [sys.executable, "-c", "import sys; sys.stderr.write('secret'); sys.exit(7)"],
            label="fixture",
        )
    assert "secret" not in str(exc.value)
