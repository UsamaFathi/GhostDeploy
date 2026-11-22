import json
import os
import shutil
import subprocess
from contextlib import nullcontext

import pytest
import yaml
from typer.testing import CliRunner

from ghostdeploy.cli import app
from ghostdeploy.config import validate
from ghostdeploy.core import GhostError, Runner
from ghostdeploy.deploy import Executor
from ghostdeploy.git import GitRepository
from ghostdeploy.planning import build_plan, manifest_version
from ghostdeploy.runtime import DatabaseState


def git(path, *args):
    return (
        subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.STDOUT)
        .decode()
        .strip()
    )


@pytest.fixture
def repository(tmp_path):
    if not shutil.which("git"):
        pytest.skip("Git required")
    path = tmp_path / "addons repository"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "GhostDeploy Test")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "core.autocrlf", "false")
    git(path, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    for addon in ("attendance", "inventory"):
        (path / addon).mkdir()
        (path / addon / "__manifest__.py").write_text(repr({"name": addon}), encoding="utf-8")
        (path / addon / "old.py").write_text(f"MODEL = '{addon}'\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "Base")
    base = git(path, "rev-parse", "HEAD")
    git(path, "mv", "attendance/old.py", "inventory/renamed.py")
    (path / "attendance" / "new.py").write_text("FIELD = 'schema candidate'\n", encoding="utf-8")
    (path / "README.md").write_text("# Release\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "Target")
    target = git(path, "rev-parse", "HEAD")
    git(path, "update-ref", "refs/remotes/origin/main", target)
    return path, base, target


def test_real_git_plan_handles_rename_and_multidatabase(repository, config_data):
    path, base, target = repository
    settings = validate(config_data)
    repo = GitRepository(path, "main", Runner())
    data = build_plan(
        settings,
        repo,
        base_sha=base,
        target_sha=target,
        installed={
            "company_a": frozenset({"attendance", "inventory"}),
            "company_b": frozenset({"inventory"}),
        },
    )
    assert data.candidate_upgrades == ("attendance", "inventory")
    assert data.installed_by_database == {
        "company_a": ("attendance", "inventory"),
        "company_b": ("inventory",),
    }
    assert data.restart_required
    assert not data.blockers
    assert any(
        change.status.startswith("R") and change.addons == ("attendance", "inventory")
        for change in data.changes
    )
    assert any(change.classifications == ("documentation",) for change in data.changes)
    assert git(path, "rev-parse", "HEAD") == target
    assert git(path, "status", "--porcelain") == ""


def test_plan_reports_installation_unknown_and_blocks_downgrade(tmp_path, repository, config_data):
    path, base, target = repository
    file = tmp_path / "ghostdeploy.yml"
    file.write_text(yaml.safe_dump(config_data), encoding="utf-8")
    command = ["plan", "--local-checkout", str(path), "--config", str(file), "--output", "json"]
    runner = CliRunner()
    result = runner.invoke(app, [*command, "--base-sha", base, "--target-sha", target])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["installed_by_database"] is None
    result = runner.invoke(app, [*command, "--base-sha", target, "--target-sha", base])
    assert result.exit_code == 1
    assert "fast-forward" in json.loads(result.stdout)["blockers"][0]


def test_fast_forward_and_dirty_tree_protection(repository):
    path, base, target = repository
    git(path, "switch", "--detach", base)
    git(path, "branch", "-f", "main", base)
    git(path, "switch", "main")
    repo = GitRepository(path, "main", Runner())
    (path / "untracked").touch()
    with pytest.raises(GhostError, match="untracked"):
        repo.sync(base, target)
    assert git(path, "rev-parse", "HEAD") == base
    (path / "untracked").unlink()
    repo.sync(base, target)
    assert repo.clean_head() == target
    with pytest.raises(GhostError, match="full lowercase"):
        repo.commit("--help")
    with pytest.raises(GhostError, match="Resolve"):
        repo.commit("0" * 40)
    git(path, "switch", "-c", "different")
    with pytest.raises(GhostError, match="configured branch"):
        repo.clean_head()


def test_manifest_coverage_with_multiple_roots_and_three_databases(repository, config_data):
    path, _, _ = repository
    (path / "primary").mkdir()
    git(path, "mv", "attendance", "primary/attendance")
    (path / "secondary").mkdir()
    git(path, "mv", "inventory", "secondary/inventory")
    module = path / "primary" / "attendance"
    manifest = module / "__manifest__.py"
    manifest.write_text(
        repr({"name": "Attendance", "depends": ["inventory"], "version": "1.1"}), encoding="utf-8"
    )
    git(path, "add", ".")
    git(path, "commit", "-m", "Set up the two roots")
    base = git(path, "rev-parse", "HEAD")
    for relative in (
        "models/shift.py",
        "views/shift.xml",
        "security/ir.model.access.csv",
        "static/src/shift.js",
        "misc/résumé file.dat",
        "docs/guide.md",
    ):
        output = module / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(relative, encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "Mixed attendance changes")
    target = git(path, "rev-parse", "HEAD")
    config_data["odoo"]["addons_path"] = ["/opt/addons/primary", "/opt/addons/secondary"]
    config_data["deployment"]["databases"].append("company_c")
    config = validate(config_data)
    repo = GitRepository(path, "main", Runner())
    installed = {
        "company_a": frozenset({"attendance", "inventory", "payroll"}),
        "company_b": frozenset({"attendance", "inventory"}),
        "company_c": frozenset({"inventory"}),
    }
    summary = build_plan(config, repo, base_sha=base, target_sha=target, installed=installed)
    assert summary.candidate_upgrades == ("attendance",)
    assert summary.installed_by_database == {
        "company_a": ("attendance",),
        "company_b": ("attendance",),
        "company_c": (),
    }
    assert {category for item in summary.changes for category in item.classifications} == {
        "potential_schema",
        "odoo_data",
        "security",
        "code",
        "unknown",
        "documentation",
    }
    assert summary.expected_versions == {"attendance": "19.0.1.1"}
    assert summary.restart_required and summary.review and not summary.blockers
    installed["company_a"] -= {"inventory"}
    mismatched = build_plan(config, repo, base_sha=base, target_sha=target, installed=installed)
    assert any(
        "company_a lacks installed dependencies" in reason for reason in mismatched.blockers
    )
    manifest.write_text(
        repr({"name": "Attendance", "depends": ["base"], "version": "1.2"}), encoding="utf-8"
    )
    git(path, "add", ".")
    git(path, "commit", "-m", "Change manifest dependencies")
    changed = build_plan(config, repo, base_sha=target, target_sha=repo.resolve("HEAD"))
    assert any("changed depends" in reason for reason in changed.blockers)
    duplicate = path / "secondary" / "attendance"
    duplicate.mkdir()
    (duplicate / "__manifest__.py").write_text("{'name': 'Duplicate'}", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "Add duplicate technical name")
    with pytest.raises(GhostError, match="Ambiguous addon"):
        build_plan(config, repo, base_sha=target, target_sha=repo.resolve("HEAD"))
    git(path, "rm", "secondary/attendance/__manifest__.py")
    manifest.write_text("{'name': ", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "Introduce a manifest syntax error")
    with pytest.raises(GhostError, match="dictionary literal"):
        build_plan(config, repo, base_sha=target, target_sha=repo.resolve("HEAD"))


@pytest.mark.parametrize(
    ("series", "raw", "result"),
    [
        ("18.0", "1.2", "18.0.1.2"),
        ("19.0", "19.0.1.2.3", "19.0.1.2.3"),
    ],
)
def test_normalizes_target_versions_for_supported_runtime_series(series, raw, result):
    assert manifest_version(raw, series) == result


def test_removed_addon_is_blocked(repository, config_data):
    path, _, target = repository
    git(path, "rm", "-r", "attendance")
    git(path, "commit", "-m", "Remove addon")
    repo = GitRepository(path, "main", Runner())
    data = build_plan(
        validate(config_data), repo, base_sha=target, target_sha=repo.resolve("HEAD")
    )
    assert any("attendance was removed" in reason for reason in data.blockers)
    assert "attendance" in data.affected_addons


def test_renamed_addon_is_blocked_and_new_addon_requires_review(repository, config_data):
    path, _, base = repository
    git(path, "mv", "attendance", "new_attendance")
    git(path, "commit", "-m", "Rename an addon")
    repo = GitRepository(path, "main", Runner())
    report = build_plan(
        validate(config_data), repo, base_sha=base, target_sha=repo.resolve("HEAD")
    )
    assert any("attendance was removed or renamed" in item for item in report.blockers)
    assert any("New addon new_attendance requires review" in item for item in report.review)


def test_execute_applies_a_docs_revision_then_reports_duplicate_as_unchanged(
    tmp_path,
    monkeypatch,
    repository,
    config_data,
):
    path, _, base = repository
    (path / "README.md").write_text("# Docs release\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "Docs release")
    target = git(path, "rev-parse", "HEAD")
    git(path, "update-ref", "refs/remotes/origin/main", target)
    git(path, "switch", "--detach", base)
    git(path, "branch", "-f", "main", base)
    git(path, "switch", "main")

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    config = validate(config_data)
    config = config.model_copy(
        update={
            "repository": config.repository.model_copy(update={"checkout_path": str(path)}),
            "odoo": config.odoo.model_copy(update={"addons_path": [str(path)]}),
            "deployment": config.deployment.model_copy(update={"state_path": str(state)}),
        }
    )
    if os.name == "nt":
        monkeypatch.setattr("ghostdeploy.deploy.deployment_lock", lambda *_: nullcontext())
    monkeypatch.setattr("ghostdeploy.deploy.bind_state_directory", lambda *_: None)

    class RunningHost:
        filestore = None

        def __init__(self, settings):
            self.config = settings

        def require_identity(self):
            pass  # The fixture never invokes a real service or database.

        def preflight(self):
            assert git(path, "status", "--porcelain") == ""

        def snapshot_databases(self):
            return {
                "company_a": DatabaseState(1, ("fixture",), {"attendance": "19.0.1.0"}),
                "company_b": DatabaseState(2, ("fixture",), {}),
            }

    executor = Executor(config, Runner(), RunningHost(config))
    options = {
        "selected": [],
        "approve_review": False,
        "confirm_production": False,
        "fetch": False,
    }
    monkeypatch.setattr(
        GitRepository, "fetch", lambda self: None
    )  # Keep all Git operations local.
    with pytest.raises(GhostError, match="no longer the fetched deployment branch tip"):
        executor.execute(base, **{**options, "fetch": True})
    assert git(path, "rev-parse", "HEAD") == base
    first = executor.execute(target, **options)
    second = executor.execute(target, **options)
    assert first.status == "succeeded"
    assert second.status == "unchanged"
    assert git(path, "rev-parse", "HEAD") == target
    assert (state / f"{first.id}.json").is_file()
    assert not list(state.glob("*.pending.json"))
    with pytest.raises(GhostError, match="fast-forward"):
        executor.execute(base, **options)
    pending = state / "odoo19.service.pending.json"
    pending.write_text('{"phase":"upgrading_database"}', encoding="utf-8")
    with pytest.raises(GhostError, match="unfinished deployment"):
        executor.execute(target, **options)
