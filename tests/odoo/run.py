"""Run one Odoo deployment fixture on a local Docker daemon and a unique Compose project."""

import argparse
import os
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path
from time import monotonic
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", choices=["18.0", "19.0"])
    args = parser.parse_args()
    if not shutil.which("docker"):
        parser.error("The local Docker CLI and Compose plugin are required.")
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
    if not endpoint.startswith(("unix:///", "npipe:////./pipe/")):
        parser.error("An explicitly local Docker daemon is required")
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in {"DOCKER_HOST", "DOCKER_CONTEXT"} and not name.startswith("COMPOSE_")
    }
    env["ODOO_TEST_VERSION"] = args.version
    env["GHOSTDEPLOY_FIXTURE_TOKEN"] = uuid4().hex
    project = f"ghostdeploy-fixture-{uuid4().hex}"
    command = [
        "docker",
        "--host",
        endpoint,
        "compose",
        "--env-file",
        os.devnull,
        "-f",
        str(Path(__file__).with_name("compose.yml")),
        "-p",
        project,
    ]
    started = monotonic()
    result = 1
    try:
        result = subprocess.run(
            [*command, "up", "--build", "--abort-on-container-exit", "--exit-code-from", "checks"],
            env=env,
            timeout=1080,
        ).returncode
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        print(f"Disposable Odoo {args.version} run interrupted or timed out.", flush=True)
    finally:
        try:
            with suppress(subprocess.TimeoutExpired):
                subprocess.run([*command, "images"], env=env, timeout=15, check=False)
        finally:
            cleanup = subprocess.run(
                [*command, "down", "--volumes", "--rmi", "local"], env=env, timeout=90, check=False
            )
        print(
            f"Fixture {project}: Odoo {args.version}, {monotonic() - started:.1f}s, test exit={result}, cleanup exit={cleanup.returncode}",
            flush=True,
        )
    raise SystemExit(result or cleanup.returncode)


if __name__ == "__main__":
    main()
