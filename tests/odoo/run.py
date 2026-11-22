"""Run one Odoo CLI integration on a local Docker daemon under a unique Compose project."""

import argparse
import os
import subprocess
from pathlib import Path
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", choices=["18.0", "19.0"])
    args = parser.parse_args()
    endpoint = None if os.environ.get("DOCKER_CONTEXT") else os.environ.get("DOCKER_HOST")
    endpoint = (
        endpoint
        or subprocess.check_output(
            [
                "docker",
                "context",
                "inspect",
                "--format",
                "{{ .Endpoints.docker.Host }}",
            ],
            text=True,
        ).strip()
    )
    if not endpoint.startswith(("unix://", "npipe://")):
        parser.error("An explicitly local Docker daemon is required")
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in {"DOCKER_HOST", "DOCKER_CONTEXT"}
    }
    env["ODOO_TEST_VERSION"] = args.version
    command = [
        "docker",
        "--host",
        endpoint,
        "compose",
        "-f",
        str(Path(__file__).with_name("compose.yml")),
        "-p",
        f"ghostdeploy-fixture-{uuid4().hex}",
    ]
    try:
        subprocess.run(
            [*command, "up", "--build", "--abort-on-container-exit", "--exit-code-from", "checks"],
            env=env,
            check=True,
        )
    finally:
        subprocess.run([*command, "down", "--volumes", "--rmi", "local"], env=env, check=True)


if __name__ == "__main__":
    main()
