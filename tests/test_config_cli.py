import json

import pytest
import yaml
from typer.testing import CliRunner

from ghostdeploy.cli import app
from ghostdeploy.config import load, validate
from ghostdeploy.core import GhostError


@pytest.mark.parametrize("path", ["../../etc/passwd", "/opt/../etc", "//host/path", "/a,b"])
def test_unsafe_path(config_data, path):
    config_data["odoo"]["executable"] = path
    with pytest.raises(GhostError, match="absolute Linux path"):
        validate(config_data)


@pytest.mark.parametrize("value", ["--all", "foo;bar", "postgresql://host/db", "a=b"])
def test_database_allowlist(config_data, value):
    config_data["deployment"]["databases"].append(value)
    with pytest.raises(GhostError, match=r"deployment.databases.2"):
        validate(config_data)


def test_production_requires_stricter_config(config_data):
    config_data["deployment"]["environment"] = "production"
    with pytest.raises(GhostError, match="production requires"):
        validate(config_data)


@pytest.mark.parametrize("content", ["a: &x [*x]", "a: 1\na: 2", "a: !!python/object:secret"])
def test_yaml_rejects_aliases_duplicates_and_python_tags(tmp_path, content):
    config = tmp_path / "ghostdeploy.yml"
    config.write_text(f"schema_version: 1\n{content}", encoding="utf-8")
    with pytest.raises(GhostError, match="Invalid YAML"):
        load(config)


def test_non_interactive_init_preserves_file_and_supports_json(tmp_path):
    runner = CliRunner()
    config = tmp_path / "ghostdeploy.yml"
    request = [
        "init",
        "--config",
        str(config),
        "--non-interactive",
        "--databases",
        "test_a,test_b",
    ]
    assert runner.invoke(app, request).exit_code == 0
    previous = config.read_bytes()
    assert runner.invoke(app, [*request, "--project", "other"]).exit_code == 1
    assert config.read_bytes() == previous
    validated = runner.invoke(app, ["validate", "--config", str(config), "--output", "json"])
    assert validated.exit_code == 0
    assert json.loads(validated.stdout)["ok"]


def test_interactive_cancel_leaves_no_file(tmp_path):
    config = tmp_path / "ghostdeploy.yml"
    result = CliRunner().invoke(app, ["init", "--config", str(config)], input="\n" * 16 + "n\n")
    assert result.exit_code == 1
    assert not config.exists()


def test_unknown_credential_is_never_echoed(tmp_path):
    config = tmp_path / "ghostdeploy.yml"
    config.write_text(
        "schema_version: 1\nsecret-credential-test: another-secret-test\n", encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--config", str(config), "--output", "json"])
    assert result.exit_code == 1
    assert not json.loads(result.stdout)["ok"]
    assert "secret-test" not in result.stdout
    assert "secret-credential-test" not in result.stdout


@pytest.mark.parametrize(
    ("path", "value", "pattern"),
    [
        (("schema_version",), True, "schema_version"),
        (("odoo", "service"), "odoo.service;bad", "odoo.service"),
        (("odoo", "version"), "17.0", "odoo.version"),
        (("repository", "branch"), "main*", "repository.branch"),
        (("repository", "branch"), "refs/heads/../main", "repository.branch"),
        (("deployment", "databases"), [], "deployment.databases"),
        (("deployment", "state_path"), "/var/backups/ghostdeploy/state", "may not overlap"),
    ],
)
def test_rejects_invalid_schema_boundaries(config_data, path, value, pattern):
    section = config_data if len(path) == 1 else config_data[path[0]]
    section[path[-1]] = value
    with pytest.raises(GhostError, match=pattern):
        validate(config_data)


def test_declined_execution_is_nonzero_with_structured_output():
    result = CliRunner().invoke(app, ["deploy", "--execute", "--dry-run", "--output", "json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["ok"] is False


def test_unexpected_preflight_error_keeps_json_and_hides_internal_details(
    tmp_path, config_data, monkeypatch
):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic confidential details")

    monkeypatch.setattr("ghostdeploy.cli.Executor.execute", fail)
    config = tmp_path / "deploy.yml"
    config.write_text(yaml.safe_dump(config_data), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "deploy",
            "--config",
            str(config),
            "--execute",
            "--target-sha",
            "a" * 40,
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "ok": False,
        "error": "An unexpected GhostDeploy error occurred.",
    }
