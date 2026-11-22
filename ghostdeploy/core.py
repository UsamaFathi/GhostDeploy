import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import current_thread, main_thread


class GhostError(Exception):
    """An actionable error whose message is safe to display."""


def safe_error(exc: BaseException) -> str:
    if isinstance(exc, GhostError):
        return str(exc)
    if isinstance(exc, KeyboardInterrupt):
        return "Interrupted by operator. Check the pending journal before retrying."
    if isinstance(exc, OSError):
        return (
            f"Filesystem operation failed: {os.strerror(exc.errno)}."
            if exc.errno
            else "Filesystem operation failed."
        )
    return "An unexpected GhostDeploy error occurred."


class CommandError(GhostError):
    def __init__(self, label: str, reason: str, returncode: int | None = None):
        self.returncode = returncode
        super().__init__(f"{label}: {reason}")


@contextmanager
def deployment_signals() -> Iterator[None]:
    """Handle runner disconnect and termination like an operator interrupt."""
    if sys.platform != "linux" or current_thread() is not main_thread():
        yield
        return

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


class Runner:
    def run(
        self,
        args: Sequence[str],
        *,
        label: str,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = 60,
        capture: bool = True,
    ) -> bytes:
        try:
            with subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            ) as process:
                try:
                    stdout, _ = process.communicate(timeout=timeout)
                except BaseException:
                    with suppress(ProcessLookupError):
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    process.wait()
                    raise
        except subprocess.TimeoutExpired:
            raise CommandError(label, f"timed out after {timeout} seconds") from None
        except OSError as exc:
            raise CommandError(label, exc.strerror or "could not start process") from None
        if process.returncode:
            raise CommandError(
                label, f"exited with status {process.returncode}", process.returncode
            )
        return stdout or b""


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
