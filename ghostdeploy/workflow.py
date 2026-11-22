import argparse
import json
import os
import re
from dataclasses import asdict
from pathlib import Path

import yaml

from ghostdeploy.config import Config, load
from ghostdeploy.core import GhostError, Runner, safe_error
from ghostdeploy.deploy import Executor
from ghostdeploy.runtime import Host

SECRETS = ("SSH_HOST", "SSH_USER", "SSH_PRIVATE_KEY", "SSH_KNOWN_HOSTS")

SSH_SCRIPT = r"""set -euo pipefail
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


def generate(config: Config) -> str:
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
    if environment != "production":
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
                            **{secret: "${{ secrets." + secret + " }}" for secret in SECRETS},
                        },
                        "run": SSH_SCRIPT,
                    }
                ],
            }
        },
    }
    return yaml.dump(workflow, Dumper=WorkflowDumper, sort_keys=False)


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
