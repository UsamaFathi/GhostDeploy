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


def test_production_dispatch_requires_configured_branch(config_data):
    config_data["deployment"]["environment"] = "production"
    config_data["deployment"]["health_check"] = {"url": "http://127.0.0.1:8069/web/health"}
    workflow = yaml.safe_load(generate(validate(config_data)))
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["jobs"]["deploy"]["if"] == "github.ref == 'refs/heads/main'"
    assert workflow["permissions"] == {}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["jobs"]["deploy"]["environment"] == "production"


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


@pytest.mark.parametrize("mode", ["missing_secret", "bad_modules", "valid"])
def test_generated_bash_with_fake_ssh(config_data, tmp_path, mode):
    if os.name == "nt":
        git = shutil.which("git")
        bash = Path(git).resolve().parents[1] / "bin" / "bash.exe" if git else None
    else:
        bash = shutil.which("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("Bash required")
    workflow = yaml.safe_load(generate(validate(config_data)))
    assert workflow["on"]["push"] == {"branches": ["main"]}
    script = workflow["jobs"]["deploy"]["steps"][0]["run"]
    stub = "ssh() { printf 'fake-ssh %s\\n' \"$@\"; }\n"
    env = {
        **os.environ,
        "DEPLOY_ENVIRONMENT": "staging",
        "TARGET_SHA": TARGET,
        "SSH_KNOWN_HOSTS": "fixture-host-key",
        "SSH_USER": "odoo",
        "SSH_HOST": "fixture.example.invalid",
        "SSH_PRIVATE_KEY": "" if mode == "missing_secret" else "fixture-private-key",
        "UPGRADE_MODULES": "attendance;id" if mode == "bad_modules" else "attendance",
        "APPROVE_REVIEW": "true",
    }
    run = subprocess.run(
        [str(bash), "-c", stub + script],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "fixture-private-key" not in run.stdout + run.stderr
    if mode != "valid":
        assert run.returncode == 1
        assert not run.stdout
        return
    assert run.returncode == 0, run.stderr
    assert "StrictHostKeyChecking=yes" in run.stdout
    assert f"deploy staging {TARGET} attendance yes" in run.stdout
