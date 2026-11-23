"""Verify the generated workflow against a loopback-only OpenSSH server."""

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch
from uuid import uuid4

import yaml

from ghostdeploy import workflow
from ghostdeploy.config import load
from ghostdeploy.core import GhostError
from ghostdeploy.deploy import Report

UNIT = "ghostfixture.service"
TARGET = "b" * 40
USER = "ghostfixture"
CONFIG = Path("/fixture/ghostdeploy.yml")
MARKER = Path("/fixture/.disposable-ssh-image")


class FixtureExecutor:
    """Only the deployment effect is substituted. The SSH forced command is real."""

    def __init__(self, config, runner, host):
        assert config.deployment.environment == "production"

    def execute(self, target, *, selected, approve_review, confirm_production, fetch):
        if target != TARGET or not (approve_review and confirm_production and fetch):
            raise GhostError("The fixture requires its approved exact revision and approvals.")
        if selected not in (["fixture_addon"], ["fixture_failure"]):
            raise GhostError("Unexpected fixture modules.")
        failed = selected == ["fixture_failure"]
        return Report(
            "fixture",
            UNIT,
            "fixture",
            "a" * 40,
            target,
            status="failed" if failed else "succeeded",
        )


def gateway() -> None:
    sys.argv = ["ghostdeploy-ssh", "--config", str(CONFIG)]
    with patch.object(workflow, "Executor", FixtureExecutor):
        workflow.ssh_main()


def key(path: Path) -> str:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    return (path.with_suffix(".pub")).read_text().strip()


def configure(root: Path) -> tuple[Path, dict[str, str]]:
    client = key(root / "client")
    host = key(root / "host")
    # The account exists only in the isolated image. Password authentication is disabled.
    subprocess.run(["chpasswd"], input=f"{USER}:{uuid4().hex}\n", text=True, check=True)
    ssh = Path(f"/home/{USER}/.ssh")
    ssh.mkdir(mode=0o700)
    auth = ssh / "authorized_keys"
    auth.touch(mode=0o600)
    auth.write_text(
        f'restrict,command="/opt/ghostdeploy/bin/python /fixture/check_forced_command.py --gateway" {client}\n'
    )
    for item in (ssh, auth):
        shutil.chown(item, user=USER, group=USER)
    config = root / "sshd_config"
    config.write_text(
        "\n".join(
            [
                "Port 22",
                "ListenAddress 127.0.0.1",
                f"HostKey {root / 'host'}",
                f"PidFile {root / 'sshd.pid'}",
                f"AllowUsers {USER}",
                "AuthorizedKeysFile .ssh/authorized_keys",
                "StrictModes yes",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "PermitRootLogin no",
                "UsePAM no",
                "",
            ]
        )
    )
    CONFIG.write_text("""
schema_version: 1
project: {name: ssh_fixture}
odoo:
  version: "19.0"
  service: ghostfixture
  service_user: ghostfixture
  executable: /opt/odoo/odoo-bin
  config_file: /etc/odoo-test.conf
  addons_path: [/srv/ghostdeploy-fixture]
repository: {branch: main, checkout_path: /srv/ghostdeploy-fixture}
deployment:
  environment: production
  databases: [fixture_db]
  health_check: {url: "http://127.0.0.1:8069/web/health"}
""")
    return config, {
        **os.environ,
        "DEPLOY_ENVIRONMENT": "production",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "PRODUCTION_CONFIRMED": "true",
        "TARGET_SHA": TARGET,
        "SSH_HOST": "127.0.0.1",
        "SSH_USER": USER,
        "SSH_PRIVATE_KEY": (root / "client").read_text(),
        "SSH_KNOWN_HOSTS": f"127.0.0.1 {host}",
        "UPGRADE_MODULES": "fixture_addon",
        "APPROVE_REVIEW": "true",
    }


def wait_for_sshd(daemon: subprocess.Popen) -> None:
    deadline = monotonic() + 10
    while daemon.poll() is None and monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 22), timeout=0.5):
                return
        except ConnectionRefusedError:
            sleep(0.1)
    raise GhostError("The disposable OpenSSH server did not start.")


def check(root: Path, env: dict[str, str]) -> None:
    generated = yaml.safe_load(workflow.generate(load(CONFIG)))
    assert set(generated["on"]) == {"workflow_dispatch"}
    assert generated["jobs"]["deploy"]["if"] == "github.ref == 'refs/heads/main'"
    assert generated["permissions"] == {}
    assert generated["concurrency"] == {
        "group": f"ghostdeploy-{UNIT}-production",
        "cancel-in-progress": False,
    }
    step = generated["jobs"]["deploy"]["steps"][0]
    assert step["env"]["TARGET_SHA"] == "${{ github.sha }}"
    assert step["env"]["PRODUCTION_CONFIRMED"] == "${{ inputs.confirm_production }}"
    script = step["run"]

    def run(overrides):
        result = subprocess.run(
            ["bash", "-c", script],
            env={**env, **overrides},
            capture_output=True,
            text=True,
            timeout=25,
        )
        assert env["SSH_PRIVATE_KEY"].strip() not in result.stdout + result.stderr
        return result

    passed = run({})
    assert passed.returncode == 0, passed.stderr
    assert json.loads(passed.stdout)["target_sha"] == TARGET
    failed = run({"UPGRADE_MODULES": "fixture_failure"})
    assert failed.returncode == 1 and json.loads(failed.stdout)["status"] == "failed"
    unauthorized = run({"TARGET_SHA": "c" * 40})
    assert unauthorized.returncode == 1 and "approved exact revision" in unauthorized.stdout
    malformed = run({"TARGET_SHA": TARGET[:12]})
    assert malformed.returncode == 1 and "full lowercase SHA" in malformed.stderr
    needs_confirmation = run({"PRODUCTION_CONFIRMED": "false"})
    assert (
        needs_confirmation.returncode == 1 and "explicit confirmation" in needs_confirmation.stderr
    )
    wrong_event = run({"GITHUB_EVENT_NAME": "push"})
    assert wrong_event.returncode == 1 and "manual dispatch" in wrong_event.stderr
    wrong_key = run({"SSH_KNOWN_HOSTS": "127.0.0.1 " + key(root / "wrong-host")})
    assert wrong_key.returncode != 0 and "Host key verification failed" in wrong_key.stderr
    key(root / "unauthorized-client")
    wrong_identity = run({"SSH_PRIVATE_KEY": (root / "unauthorized-client").read_text()})
    assert wrong_identity.returncode == 255 and "Permission denied" in wrong_identity.stderr

    known = root / "known_hosts"
    known.write_text(env["SSH_KNOWN_HOSTS"] + "\n")
    denied = subprocess.run(
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known}",
            "-i",
            str(root / "client"),
            f"{USER}@127.0.0.1",
            "whoami",
        ],
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert denied.returncode == 1
    assert "only accepts GhostDeploy deployment requests" in json.loads(denied.stdout)["error"]
    print(
        "PASS restricted SSH: real loopback OpenSSH, forced command, pinned host key, approvals, exact SHA and remote exit status"
    )


def main() -> None:
    if not MARKER.is_file():
        raise SystemExit("Use the disposable SSH container.")
    if sys.argv[1:] == ["--gateway"] and os.geteuid() != 0:
        gateway()
    elif len(sys.argv) == 1 and os.geteuid() == 0:
        with TemporaryDirectory(prefix="ghostdeploy-ssh-") as directory:
            root = Path(directory)
            config, env = configure(root)
            with (
                (root / "sshd.log").open("wb") as log,
                subprocess.Popen(
                    ["/usr/sbin/sshd", "-D", "-e", "-f", str(config)],
                    stderr=log,
                ) as daemon,
            ):
                try:
                    wait_for_sshd(daemon)
                    check(root, env)
                except Exception:
                    print((root / "sshd.log").read_text(errors="replace")[-4000:])
                    raise
                finally:
                    daemon.terminate()
                    try:
                        daemon.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
    else:
        raise SystemExit("Invalid fixture invocation.")


if __name__ == "__main__":
    main()
