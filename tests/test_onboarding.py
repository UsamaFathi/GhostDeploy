import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from ghostdeploy import diagnostics, discovery
from ghostdeploy.cli import app
from ghostdeploy.config import load, validate
from ghostdeploy.core import GhostError, Runner
from ghostdeploy.deploy import Report
from ghostdeploy.diagnostics import diagnose
from ghostdeploy.discovery import InstallHints, ServiceCandidate
from ghostdeploy.git import GitRepository
from ghostdeploy.runtime import SUDO, SYSTEMCTL, Finding, Host


class SetupHost(Host):
    def __init__(self, config, runner, failing=""):
        super().__init__(config, self)
        self.failing = failing
        self.probed = []

    def inspect(self):
        return [
            Finding(check, check != self.failing, "Fixture prerequisite check.")
            for check in (
                "Operating system",
                "Python",
                "git",
                "psql",
                "pg_dump",
                "pg_restore",
                SYSTEMCTL,
                SUDO,
                "Git checkout",
                "Odoo executable",
                "Odoo configuration",
                "Service",
                "State directory",
                "Backup directory",
            )
        ]

    def require_identity(self):
        if self.failing == "Service account":
            raise GhostError("Wrong service account.")

    def require_service_permissions(self):
        if self.failing == "Systemd stop/start permissions":
            raise GhostError("sudo permission missing.")

    def read_odoo_config(self):
        self.pg_settings = {"PGPASSWORD": "fixture-secret", "PGHOST": "127.0.0.1"}

    def systemd(self):
        return {}

    def service_process(self, state):
        pass

    def run(self, args, *, env, label, **kwargs):
        assert args[0] == "psql"
        assert env["PGOPTIONS"] == "-c default_transaction_read_only=on"
        database = env["PGDATABASE"]
        if "pg_roles" in args[-1]:
            return b"t\n"
        self.probed.append(database)
        if self.failing == f"Database {database}":
            raise GhostError("Connection failed.")
        return database.encode() + b"\n"


@pytest.fixture
def addons_repo(tmp_path):
    if not shutil.which("git"):
        pytest.skip("Git required for the onboarding checkout fixture")
    checkout = tmp_path / "addons checkout"
    checkout.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "GhostDeploy fixture")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", str(tmp_path / "no-hooks"))
    manifest = checkout / "primary" / "attendance" / "__manifest__.py"
    second = checkout / "secondary" / "inventory" / "__manifest__.py"
    for path in (manifest, second):
        path.parent.mkdir(parents=True)
        path.write_text(repr({"name": path.parent.name, "version": "1.0"}), encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Set up the addon roots")
    base = git("rev-parse", "HEAD")
    (manifest.parent / "shift.py").write_text("# Future schema change", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Change attendance")
    target = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", target)
    git("switch", "--detach", base)
    git("branch", "-f", "main", base)
    git("switch", "main")
    return checkout, base, target


@pytest.mark.parametrize("series", ["18.0", "19.0"])
def test_complete_setup_journey(monkeypatch, tmp_path, addons_repo, series):
    checkout, base, target = addons_repo
    runner = CliRunner()
    file = tmp_path / "ghostdeploy.yml"
    candidates = (
        ServiceCandidate("odoo_test", "active"),
        ServiceCandidate("other_odoo", "inactive"),
    )
    selected = []

    def hints(candidate, runner):
        selected.append(candidate.name)
        return InstallHints(
            "odoo_test",
            "/opt/odoo/odoo-bin",
            "/opt/odoo/venv/bin/python",
            "/etc/odoo.conf",
            series,
            ("/srv/addons/primary", "/srv/addons/secondary", "/opt/odoo/addons"),
            ("/srv/addons", "/opt/odoo"),
            ("company_a", "company_unmanaged"),
        )

    monkeypatch.setattr("ghostdeploy.cli.is_ubuntu", lambda: True)
    monkeypatch.setattr("ghostdeploy.cli.list_services", lambda runner: candidates)
    monkeypatch.setattr("ghostdeploy.cli.inspect_service", hints)
    monkeypatch.setattr("ghostdeploy.cli.Host", SetupHost)
    monkeypatch.setattr(
        diagnostics,
        "GitRepository",
        lambda path, branch, runner: GitRepository(checkout, branch, Runner()),
    )
    monkeypatch.setattr(
        diagnostics.shutil, "disk_usage", lambda path: SimpleNamespace(free=4096 * 1024**2)
    )
    answers = ["1", *([""] * 11), "company_a,company_b,company_c", *([""] * 5), "y"]
    init = runner.invoke(
        app, ["init", "--verify-databases", "--config", str(file)], input="\n".join(answers) + "\n"
    )
    assert init.exit_code == 0, init.output
    assert selected == ["odoo_test"]
    assert "other_odoo.service" in init.output
    assert "company_unmanaged" in init.output
    assert "company_c: read-only connection confirmed." in init.output
    assert "company_unmanaged: read-only connection confirmed." not in init.output
    assert "fixture-secret" not in init.output
    config = load(file)
    assert config.odoo.version == series
    assert config.odoo.addons_path == ["/srv/addons/primary", "/srv/addons/secondary"]
    assert config.deployment.databases == ["company_a", "company_b", "company_c"]
    assert b"company_unmanaged" not in file.read_bytes()
    original = file.read_bytes()

    def run(*args):
        return runner.invoke(app, [*args, "--config", str(file)])

    validated = run("validate", "--output", "json")
    assert validated.exit_code == 0 and json.loads(validated.stdout)["ok"]
    doctor = run("doctor", "--output", "json")
    assert doctor.exit_code == 0, doctor.output
    report = json.loads(doctor.stdout)
    assert {
        item["status"] for item in report["checks"] if item["check"].startswith("Database ")
    } == {"PASS"}
    assert "fixture-secret" not in doctor.stdout
    assert "company_unmanaged" not in doctor.stdout
    assert any(item["status"] == "SKIP" and item["remediation"] for item in report["checks"])
    workflow = tmp_path / ".github" / "workflows" / "staging.yml"
    made = run("workflow", "generate", "--manual-only", "--path", str(workflow))
    assert made.exit_code == 0, made.output
    assert set(yaml.safe_load(workflow.read_text(encoding="utf-8"))["on"]) == {"workflow_dispatch"}
    setup = run("workflow", "setup", "--output", "json")
    assert setup.exit_code == 0
    assert json.loads(setup.stdout)["authorized_key_example"].startswith("restrict,command=")
    args = ["--local-checkout", str(checkout), "--target-sha", target, "--output", "json"]
    plan = run("plan", "--base-sha", base, *args)
    assert plan.exit_code == 0, plan.output
    plan_json = json.loads(plan.stdout)
    assert plan_json["project"] == config.project.name and plan_json["environment"] == "staging"
    assert plan_json["installed_by_database"] is None and plan_json["shared_service_impact"]
    assert plan_json["candidate_upgrades"] == ["attendance"]
    assert [step["action"] for step in plan_json["operations"]] == [
        "preflight",
        "stop_service",
        "verify_backups",
        "fast_forward",
        "upgrade_and_verify",
        "start_service",
        "record_result",
    ]
    assert json.loads(run("deploy", "--dry-run", *args).stdout)["dry_run"]
    assert run("deploy", "--execute", *args).exit_code == 1
    assert run("init", "--non-interactive", "--databases", "unreviewed").exit_code == 1
    assert file.read_bytes() == original
    assert GitRepository(checkout, "main", Runner()).clean_head() == base


@pytest.mark.parametrize(
    ("failure", "skipped"),
    [
        ("git", "Git repository"),
        ("psql", "Database company_a"),
        ("Odoo executable", None),
        ("Service", "Database company_a"),
        ("Service account", "Systemd stop/start permissions"),
        ("Systemd stop/start permissions", None),
        ("State directory", None),
        ("Backup directory", "Available backup storage"),
        ("Database company_a", None),
    ],
)
def test_diagnostics_reports_failure_and_dependency_skips(
    failure, skipped, config_data, monkeypatch
):
    host = SetupHost(validate(config_data), Runner(), failure)
    monkeypatch.setattr(
        diagnostics,
        "GitRepository",
        lambda *args: SimpleNamespace(
            clean_head=lambda: "sha", resolve=lambda ref: "sha", remote_ref="origin/main"
        ),
    )
    monkeypatch.setattr(
        diagnostics.shutil, "disk_usage", lambda path: SimpleNamespace(free=4096 * 1024**2)
    )
    findings = {check.check: check for check in diagnose(host)}
    assert findings[failure].status == "FAIL"
    assert findings[failure].remediation
    if skipped:
        assert findings[skipped].status == "SKIP"
    assert set(host.probed) <= {"company_a", "company_b"}
    if failure in {"psql", "Service", "Service account"}:
        assert not host.probed


def test_interactive_service_discovery_only_reads_selected_process(monkeypatch):
    calls = []

    class DiscoveryRunner:
        def run(self, args, **kwargs):
            calls.append(args)
            if "list-units" in args:
                return b"odoo18.service loaded active running Fixture\nodoo19.service loaded inactive dead Fixture\n"
            return b"Id=odoo18.service\nUser=odoo\nMainPID=1234\n"

    read = []

    def read_fixture(path, limit=65536):
        read.append(path)
        if path == Path("/proc/1234/cmdline"):
            return b"/opt/venv/bin/python\0/opt/odoo/odoo-bin\0-c\0/etc/odoo.conf\0"
        if path == Path("/etc/odoo.conf"):
            return b"[options]\naddons_path=/srv/addons\ndb_name=company_a,bad/name\ndb_password=fixture-secret\n"
        if path.name == "release.py":
            return b"version_info = (18, 0, 0, 'final', 0)"
        pytest.fail(f"Unexpected read: {path}")

    real_stat = Path.stat
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, **opts: (
            SimpleNamespace(st_uid=1000) if self == Path("/proc/1234") else real_stat(self, **opts)
        ),
    )
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(discovery, "read_regular", read_fixture)
    monkeypatch.setattr(discovery, "find_checkouts", lambda roots: roots)
    runner = DiscoveryRunner()
    candidates = discovery.list_services(runner)
    assert [candidate.name for candidate in candidates] == ["odoo18", "odoo19"]
    info = discovery.inspect_service(candidates[0], runner)
    assert info.version == "18.0" and info.declared_databases == ("company_a",)
    assert "fixture-secret" not in repr(info)
    assert len(read) == 3
    assert len(calls) == 2 and calls[-1][-1] == "odoo18.service"


def test_wizard_preview_requires_confirmation_before_writing(tmp_path):
    path = tmp_path / "declined.yml"
    responses = [
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "reviewed_company",
        "",
        "",
        "",
        "",
        "",
        "n",
    ]
    run = CliRunner().invoke(
        app, ["init", "--no-discover", "--config", str(path)], input="\n".join(responses) + "\n"
    )
    assert run.exit_code == 1 and "Configuration preview" in run.stdout
    assert "reviewed_company" in run.stdout and not path.exists()


def test_wizard_cannot_probe_a_remote_postgresql_endpoint(config_data, monkeypatch, capsys):
    class RemoteSetupHost(SetupHost):
        def read_odoo_config(self):
            self.pg_settings = {"PGHOST": "fixture.example.invalid"}

        def run(self, *args, **kwargs):
            pytest.fail("Wizard must not issue a remote database query.")

    monkeypatch.setattr("ghostdeploy.cli.Host", RemoteSetupHost)
    from ghostdeploy.cli import preview_database_connections

    preview_database_connections(validate(config_data))
    assert "require a local PostgreSQL socket or loopback" in " ".join(
        capsys.readouterr().out.split()
    )


def test_production_configuration_and_failure_report(tmp_path, monkeypatch):
    runner = CliRunner()
    path = tmp_path / "production.yml"
    init = [
        "init",
        "--non-interactive",
        "--config",
        str(path),
        "--environment",
        "production",
        "--databases",
        "company_a,company_b",
    ]
    assert runner.invoke(app, init).exit_code == 1
    assert not path.exists()
    assert (
        runner.invoke(app, [*init, "--health-url", "http://127.0.0.1:8069/web/health"]).exit_code
        == 0
    )
    assert load(path).deployment.environment == "production"
    execution = ["deploy", "--config", str(path), "--execute", "--target-sha", "b" * 40]
    assert runner.invoke(app, [*execution, "--output", "json"]).exit_code == 1
    assert "--confirm-production" in runner.invoke(app, execution).output
    report = Report(
        "fixture-id",
        "odoo.service",
        "2026-01-01T00:00:00+00:00",
        "a" * 40,
        "b" * 40,
        phase="upgrading_database",
        status="failed",
        completed_at="2026-01-01T00:01:00+00:00",
        completed_phases=["preflight", "database_verified:company_a"],
        in_progress_database="company_b",
        upgraded_databases=["company_a"],
        verified_databases=["company_a"],
        backups=[{"database": "company_a", "dump": {"path": "/var/backups/example.dump"}}],
        recovery="Inspect the pending journal before retrying.",
    )
    monkeypatch.setattr("ghostdeploy.cli.Executor.execute", lambda self, *args, **kwargs: report)
    result = runner.invoke(app, [*execution, "--confirm-production"])
    assert result.exit_code == 1
    assert "Uncertain database: company_b" in result.output
    assert "Verified addon versions: company_a" in result.output
    assert "/var/backups/example.dump" in result.output
    assert "Inspect the pending journal" in result.output
