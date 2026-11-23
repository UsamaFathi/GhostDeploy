import argparse
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import TypedDict

import yaml

from ghostdeploy.config import Config, load
from ghostdeploy.core import GhostError, Runner, safe_error
from ghostdeploy.deploy import Executor
from ghostdeploy.runtime import Host

SECRETS = ("SSH_HOST", "SSH_USER", "SSH_PRIVATE_KEY", "SSH_KNOWN_HOSTS")

SSH_SCRIPT = r"""set +x
set -euo pipefail
if [[ "$DEPLOY_ENVIRONMENT" == production ]] &&
   [[ "${GITHUB_EVENT_NAME:-}" != workflow_dispatch || "${PRODUCTION_CONFIRMED:-}" != true ]]; then
  echo 'Production requires a manual dispatch with explicit confirmation.' >&2
  exit 1
fi
if [[ -z "$SSH_PRIVATE_KEY" || -z "$SSH_KNOWN_HOSTS" ]]; then
  echo 'Configure the SSH_PRIVATE_KEY and SSH_KNOWN_HOSTS environment secrets.' >&2
  exit 1
fi
if [[ ! "$SSH_HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ || \
      ! "$SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ || "$SSH_USER" == root ]]; then
  echo 'Configure a DNS name or IPv4 address and a non-root SSH user.' >&2
  exit 1
fi
if [[ ! "$TARGET_SHA" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]]; then
  echo 'The triggering commit must be a full lowercase SHA.' >&2
  exit 1
fi
modules=${UPGRADE_MODULES:--}
if [[ ${#modules} -gt 4096 || ( "$modules" != '-' && \
      ! "$modules" =~ ^[a-zA-Z_][a-zA-Z0-9_]{0,62}(,[a-zA-Z_][a-zA-Z0-9_]{0,62})*$ ) ]]; then
  echo 'Pass a comma-separated list of module names without spaces.' >&2
  exit 1
fi
review=no
[[ "$APPROVE_REVIEW" == true ]] && review=yes
umask 077
ssh_dir=$(mktemp -d)
trap 'rm -rf -- "$ssh_dir"' EXIT
printf '%s\n' "$SSH_PRIVATE_KEY" > "$ssh_dir/key"
printf '%s\n' "$SSH_KNOWN_HOSTS" > "$ssh_dir/known_hosts"
unset SSH_PRIVATE_KEY SSH_KNOWN_HOSTS
ssh -T -F /dev/null -o BatchMode=yes -o ConnectTimeout=15 \
  -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o GlobalKnownHostsFile=/dev/null -o "UserKnownHostsFile=$ssh_dir/known_hosts" \
  -i "$ssh_dir/key" "$SSH_USER@$SSH_HOST" \
  "deploy $DEPLOY_ENVIRONMENT $TARGET_SHA $modules $review"
"""


class WorkflowDumper(yaml.SafeDumper):
    def represent_str(self, data):
        return self.represent_scalar(
            "tag:yaml.org,2002:str", data, style="|" if "\n" in data else None
        )


WorkflowDumper.add_representer(str, WorkflowDumper.represent_str)


def generate(config: Config, *, manual_only: bool = False) -> str:
    environment = config.deployment.environment
    branch = config.repository.branch
    triggers = {
        "workflow_dispatch": {
            "inputs": {
                "upgrade_modules": {
                    "description": "Reviewed installed candidates, comma-separated",
                    "type": "string",
                    "required": False,
                },
                "approve_review": {
                    "description": "Approve review items in this exact commit's plan",
                    "type": "boolean",
                    "default": False,
                },
            }
        }
    }
    if environment == "production":
        triggers["workflow_dispatch"]["inputs"]["confirm_production"] = {
            "description": "Confirm this production deployment",
            "type": "boolean",
            "required": True,
            "default": False,
        }
    elif not manual_only:
        triggers["push"] = {"branches": [branch]}
    workflow = {
        "name": f"Deploy {config.project.name} ({environment})",
        "on": triggers,
        "permissions": {},
        "concurrency": {
            "group": f"ghostdeploy-{config.odoo.unit}-{environment}",
            "cancel-in-progress": False,
        },
        "jobs": {
            "deploy": {
                "if": f"github.ref == 'refs/heads/{branch}'",
                "runs-on": "ubuntu-latest",
                "environment": environment,
                "timeout-minutes": 360,
                "steps": [
                    {
                        "name": "Deploy the triggering commit over SSH",
                        "shell": "bash",
                        "env": {
                            "TARGET_SHA": "${{ github.sha }}",
                            "DEPLOY_ENVIRONMENT": environment,
                            "UPGRADE_MODULES": "${{ inputs.upgrade_modules }}",
                            "APPROVE_REVIEW": "${{ inputs.approve_review }}",
                            **(
                                {"PRODUCTION_CONFIRMED": "${{ inputs.confirm_production }}"}
                                if environment == "production"
                                else {}
                            ),
                            **{secret: "${{ secrets." + secret + " }}" for secret in SECRETS},
                        },
                        "run": SSH_SCRIPT,
                    }
                ],
            }
        },
    }
    return yaml.dump(workflow, Dumper=WorkflowDumper, sort_keys=False)


class SetupGuide(TypedDict):
    environment: str
    branch: str
    secrets: tuple[str, ...]
    authorized_key_example: str
    steps: list[str]


def setup_instructions(config: Config) -> SetupGuide:
    environment = config.deployment.environment
    return {
        "environment": environment,
        "branch": config.repository.branch,
        "secrets": SECRETS,
        "authorized_key_example": (
            'restrict,command="/opt/ghostdeploy/venv/bin/ghostdeploy-ssh --config /etc/ghostdeploy.yml" '
            f"ssh-ed25519 <PUBLIC_KEY_BODY> ghostdeploy-{environment}"
        ),
        "steps": [
            f"On the VPS, install the reviewed wheel in /opt/ghostdeploy/venv and run doctor as {config.odoo.service_user}. Use an admin-controlled config and the canonical systemd unit.",
            "On your workstation, create a dedicated Ed25519 SSH key for this environment. Keep the private key out of the repository.",
            f"On the VPS, install only its public key with the restriction below for {config.odoo.service_user}. Adjust the two absolute paths if needed.",
            "Pin the VPS host key from a trusted channel. Verify GitHub's host key separately if the VPS fetches over SSH.",
            f"On GitHub, configure the {environment} environment for the branch {config.repository.branch}. Add these environment secrets: {', '.join(SECRETS)}. Use protected branches and production reviewers where available.",
            "Generate the workflow in the addons repo with --manual-only for the first staging run. Review the exact commit and complete the disposable staging checklist before authorizing execution.",
        ],
    }


def ssh_request(request: str, config: Config) -> tuple[str, list[str], bool]:
    fields = request.split(" ")
    if len(request) > 4250 or len(fields) != 5:
        raise GhostError("SSH key only accepts GhostDeploy deployment requests.")
    action, environment, target, modules, review = fields
    if (
        action != "deploy"
        or environment != config.deployment.environment
        or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", target)
        or (
            modules != "-"
            and not re.fullmatch(
                r"[a-zA-Z_][a-zA-Z0-9_]{0,62}(?:,[a-zA-Z_][a-zA-Z0-9_]{0,62})*", modules
            )
        )
        or review not in {"yes", "no"}
    ):
        raise GhostError("Invalid GhostDeploy SSH request for this environment.")
    return target, [] if modules == "-" else modules.split(","), review == "yes"


def ssh_main() -> None:
    parser = argparse.ArgumentParser(description="Restricted GitHub Actions SSH entry point")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    try:
        config = load(args.config)
        target, modules, review = ssh_request(os.environ.get("SSH_ORIGINAL_COMMAND", ""), config)
        runner = Runner()
        report = Executor(config, runner, Host(config, runner)).execute(
            target,
            selected=modules,
            approve_review=review,
            confirm_production=True,
            fetch=True,
        )
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({"ok": False, "error": safe_error(exc)}))
        raise SystemExit(1) from None
    print(json.dumps(asdict(report)))
    raise SystemExit(1 if report.status == "failed" else 0)
