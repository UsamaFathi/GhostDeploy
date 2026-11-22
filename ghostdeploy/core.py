import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path


class GhostError(Exception):
    """An actionable error whose message is safe to display."""


class CommandError(GhostError):
    def __init__(self, label: str, reason: str, returncode: int | None = None):
        self.returncode = returncode
        super().__init__(f"{label}: {reason}")


class Runner:
    def run(
        self,
        args: Sequence[str],
        *,
        label: str,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = 60,
    ) -> bytes:
        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise CommandError(label, f"timed out after {timeout} seconds") from None
        except OSError as exc:
            raise CommandError(label, exc.strerror or "could not start process") from None
        if result.returncode:
            raise CommandError(label, f"exited with status {result.returncode}", result.returncode)
        return result.stdout


def env_without(prefix: str) -> dict[str, str]:
    return {
        name: value for name, value in os.environ.items() if not name.upper().startswith(prefix)
    }


def git_environment() -> dict[str, str]:
    env = env_without("GIT_")
    env.update(
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_SSH_COMMAND=shlex.join(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        ),
    )
    return env
