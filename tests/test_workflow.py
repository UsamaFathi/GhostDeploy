import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ghostdeploy.config import validate
from ghostdeploy.core import GhostError
from ghostdeploy.workflow import generate, ssh_request

TARGET = "b" * 40
INTEGRATION = Path(__file__).resolve().parents[1] / ".github/workflows/integration.yml"


def bash_executable() -> str:
    if os.name == "nt":
        git = shutil.which("git")
        bash = Path(git).resolve().parents[1] / "bin" / "bash.exe" if git else None
    else:
        bash = shutil.which("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("Bash required")
    return str(bash)


def test_production_dispatch_requires_configured_branch(config_data):
    config_data["deployment"]["environment"] = "production"
    config_data["deployment"]["health_check"] = {"url": "http://127.0.0.1:8069/web/health"}
    workflow = yaml.safe_load(generate(validate(config_data)))
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["jobs"]["deploy"]["if"] == "github.ref == 'refs/heads/main'"
    assert workflow["permissions"] == {}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["jobs"]["deploy"]["environment"] == "production"
    assert workflow["on"]["workflow_dispatch"]["inputs"]["confirm_production"]["default"] is False
    assert (
        workflow["jobs"]["deploy"]["steps"][0]["env"]["PRODUCTION_CONFIRMED"]
        == "${{ inputs.confirm_production }}"
    )


@pytest.mark.parametrize(
    "payload",
    [
        "deploy staging HEAD - no",
        f"deploy production {TARGET} - no",
        f"deploy staging {TARGET} attendance;id no",
        "whoami",
    ],
)
def test_forced_ssh_command_rejects_unexpected_requests(config_data, payload):
    with pytest.raises(GhostError):
        ssh_request(payload, validate(config_data))


@pytest.mark.parametrize(
    "mode",
    [
        "missing_secret",
        "bad_modules",
        "valid",
        "remote_failure",
        "unconfirmed_prod",
        "push_prod",
        "confirmed_prod",
    ],
)
def test_generated_bash_with_fake_ssh(config_data, tmp_path, mode):
    bash = bash_executable()
    production = mode.endswith("_prod")
    if production:
        config_data["deployment"]["environment"] = "production"
        config_data["deployment"]["health_check"] = {"url": "http://127.0.0.1:8069/web/health"}
    workflow = yaml.safe_load(generate(validate(config_data)))
    if not production:
        assert workflow["on"]["push"] == {"branches": ["main"]}
    script = workflow["jobs"]["deploy"]["steps"][0]["run"]
    stub = 'ssh() { printf \'fake-ssh %s\\n\' "$@"; return "$STUB_EXIT"; }\n'
    environment = "production" if production else "staging"
    env = {
        **os.environ,
        "DEPLOY_ENVIRONMENT": environment,
        "TARGET_SHA": TARGET,
        "SSH_KNOWN_HOSTS": "fixture-host-key",
        "SSH_USER": "odoo",
        "SSH_HOST": "fixture.example.invalid",
        "SSH_PRIVATE_KEY": "" if mode == "missing_secret" else "fixture-private-key",
        "UPGRADE_MODULES": "attendance;id" if mode == "bad_modules" else "attendance",
        "APPROVE_REVIEW": "true",
        "PRODUCTION_CONFIRMED": "false" if mode == "unconfirmed_prod" else "true",
        "GITHUB_EVENT_NAME": "push" if mode == "push_prod" else "workflow_dispatch",
        "STUB_EXIT": "23" if mode == "remote_failure" else "0",
    }
    run = subprocess.run(
        [bash, "-c", stub + script],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "fixture-private-key" not in run.stdout + run.stderr
    if mode in {"missing_secret", "bad_modules", "unconfirmed_prod", "push_prod"}:
        assert run.returncode == 1
        assert not run.stdout
        return
    assert run.returncode == (23 if mode == "remote_failure" else 0), run.stderr
    assert "StrictHostKeyChecking=yes" in run.stdout
    assert f"deploy {environment} {TARGET} attendance yes" in run.stdout


def test_requested_systemd_without_sudo_exits_nonzero_and_reports_skip(tmp_path):
    steps = yaml.safe_load(INTEGRATION.read_text())["jobs"]["hosted-systemd"]["steps"]
    assertions = next(step["run"] for step in steps if step.get("id") == "assertions")
    summary = next(
        step["run"] for step in steps if step.get("name") == "Report requested systemd gate"
    )
    env = {
        **os.environ,
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_OUTPUT": "outputs.txt",
        "GITHUB_STEP_SUMMARY": "summary.txt",
        "SUITE": "systemd",
    }
    bash = bash_executable()
    check = subprocess.run(
        [
            bash,
            "-eo",
            "pipefail",
            "-c",
            "cat() { echo systemd; }; sudo() { return 1; }\n" + assertions,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert check.returncode == 77 and "SKIPPED" in check.stdout
    assert (tmp_path / "outputs.txt").read_text().strip() == "result=SKIPPED"
    subprocess.run(
        [bash, "-eo", "pipefail", "-c", summary],
        cwd=tmp_path,
        env={**env, "RESULT": "SKIPPED", "JOB_STATUS": "failure"},
        check=True,
        timeout=15,
    )
    assert (tmp_path / "summary.txt").read_text().splitlines() == [
        "systemd: SKIPPED",
        "The required assertions did not run. The release gate remains PENDING.",
    ]
