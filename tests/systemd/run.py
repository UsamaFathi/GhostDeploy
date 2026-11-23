"""Provision one synthetic service on a fresh GitHub-hosted Ubuntu runner."""

import os
import platform
import pwd
import shutil
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

SYSTEMCTL = "/usr/bin/systemctl"
SUDO = "/usr/bin/sudo"
PYTHON = "/usr/bin/python3"


def command(*args: str, check: bool = True, timeout: int = 25) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, timeout=timeout, stdin=subprocess.DEVNULL)


def remove_unit(unit: str, definition: Path) -> None:
    with ExitStack() as cleanup:
        cleanup.callback(command, SYSTEMCTL, "daemon-reload")
        cleanup.callback(definition.unlink)
        cleanup.callback(command, SYSTEMCTL, "reset-failed", unit, check=False)
        command(SYSTEMCTL, "stop", unit)


def main(dependencies: Path) -> int:
    if (
        not sys.platform.startswith("linux")
        or os.geteuid() != 0
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or platform.freedesktop_os_release().get("ID") != "ubuntu"
        or Path("/proc/1/comm").read_text().strip() != "systemd"
        or command(SYSTEMCTL, "show", "--property=Version", check=False).returncode
    ):
        print("SKIP: a fresh GitHub-hosted Ubuntu VM with root and systemd PID 1 is required.")
        return 77

    name = f"gdfx{uuid4().hex[:16]}"
    unit = name + ".service"
    root = Path("/run") / name
    unit_path = Path("/run/systemd/system") / unit
    sudoers = Path("/etc/sudoers.d") / name
    state = subprocess.run(
        [SYSTEMCTL, "show", "--property=LoadState", "--value", "--", unit],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if state.stdout.strip() != b"not-found" or any(map(os.path.lexists, (unit_path, sudoers))):
        raise RuntimeError("The generated fixture name is already in use.")
    root.mkdir(mode=0o755)
    with ExitStack() as cleanup:
        cleanup.callback(shutil.rmtree, root)
        command(
            "/usr/sbin/useradd",
            "-M",
            "-U",
            "-d",
            str(root / "home"),
            "-s",
            "/usr/sbin/nologin",
            name,
        )
        cleanup.callback(command, "/usr/sbin/userdel", name)
        identity = pwd.getpwnam(name)
        for directory in ("home", "work"):
            path = root / directory
            path.mkdir(mode=0o700)
            os.chown(path, identity.pw_uid, identity.pw_gid)
        for filename in ("service.py", "check.py"):
            shutil.copyfile(Path(__file__).with_name(filename), root / filename)
        library = root / "lib"
        shutil.copytree(dependencies, library)
        library.chmod(0o755)
        (root / "fixture.conf").write_text(
            f"[options]\naddons_path = {root}/work/addons\ndata_dir = {root}/work/data\n"
            "db_host = 127.0.0.1\ndb_port = 1\ndb_name = fixture_db\n"
            f"[fixture]\nwork = {root}/work\n"
        )
        with sudoers.open("x") as rule:
            cleanup.callback(sudoers.unlink)
            os.fchmod(rule.fileno(), 0o440)
            rule.write(
                f"{name} ALL=(root) NOPASSWD: {SYSTEMCTL} start {unit}, {SYSTEMCTL} stop {unit}\n"
            )
        command("/usr/sbin/visudo", "-c", "-f", str(sudoers))
        with unit_path.open("x") as definition:
            cleanup.callback(remove_unit, unit, unit_path)
            definition.write(
                "[Service]\nType=notify\nNotifyAccess=main\n"
                f"User={name}\n"
                f"ExecStart={PYTHON} {root}/service.py -c {root}/fixture.conf\n"
                "KillMode=control-group\nSendSIGKILL=yes\n"
                "TimeoutStartSec=3s\nTimeoutStopSec=3s\n"
            )
        command(SYSTEMCTL, "daemon-reload")
        print(platform.platform(), platform.freedesktop_os_release()["PRETTY_NAME"], flush=True)
        result = command(
            SUDO,
            "-n",
            "-H",
            "-u",
            name,
            "--",
            PYTHON,
            str(root / "check.py"),
            str(root),
            check=False,
            timeout=90,
        ).returncode
        if result:
            command("/usr/bin/journalctl", "--no-pager", "-n", "24", "-u", unit, check=False)
    print("Removed owned systemd fixture resources.", flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
