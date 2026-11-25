"""Run Odoo and PostgreSQL in an owned fixture on a fresh hosted Ubuntu VM."""

import argparse
import json
import os
import platform
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from time import monotonic
from uuid import uuid4

SYSTEMCTL = "/usr/bin/systemctl"
SUDO = "/usr/bin/sudo"
PYTHON = "/usr/bin/python3"
DOCKER = ("/usr/bin/docker", "--host", "unix:///var/run/docker.sock")
IMAGES = {
    "18.0": "sha256:0bd79cf07f59105c5697667c2241b86447e99c6adb036200df0e4e295785bdf3",
    "19.0": "sha256:03a47921f9eef2c225ded1167fe7828465d2b3177883f30d173bb1c3c792949b",
}


def run(*args, timeout: int = 30, check: bool = True, **kwargs):
    return subprocess.run(args, check=check, timeout=timeout, stdin=subprocess.DEVNULL, **kwargs)


def as_user(name: str) -> tuple[str, ...]:
    return (SUDO, "-n", "-H", "-u", name, "--")


def account(name: str, home: Path, cleanup: ExitStack) -> None:
    run("/usr/sbin/useradd", "-M", "-U", "-d", home, "-s", "/usr/sbin/nologin", name)
    cleanup.callback(run, "/usr/sbin/userdel", name)
    own(home, name)


def own(path: Path, name: str) -> None:
    identity = pwd.getpwnam(name)
    path.mkdir(mode=0o700)
    os.chown(path, identity.pw_uid, identity.pw_gid)


def stage_runtime(root: Path, dependencies: Path, series: str) -> None:
    image = f"odoo:{series}@{IMAGES[series]}"
    container = f"{root.name}-payload"
    run(*DOCKER, "pull", image, timeout=240)
    run(*DOCKER, "create", "--name", container, "--network", "none", image)
    try:
        run(*DOCKER, "cp", f"{container}:/usr/bin/odoo", root / "odoo")
        run(
            *DOCKER,
            "cp",
            f"{container}:/usr/lib/python3/dist-packages",
            root / "odoo-packages",
            timeout=90,
        )
        run(
            *DOCKER,
            "cp",
            f"{container}:/usr/lib/x86_64-linux-gnu",
            root / "image-libs",
            timeout=90,
        )
    finally:
        run(*DOCKER, "rm", container)
    # Only supply SONAMEs absent from the runner. Keep its native loader and libc.
    available = {
        line.split()[0]
        for line in run(
            "/usr/sbin/ldconfig", "-p", capture_output=True, text=True
        ).stdout.splitlines()
        if "=>" in line
    }
    image_libraries = root / "image-libs"
    library = root / "native-libs"
    library.mkdir()
    for item in image_libraries.iterdir():
        if re.fullmatch(r"lib[\w+.-]+\.so\.\d[\w.-]*", item.name) and item.name not in available:
            source = item.resolve()
            if source.is_relative_to(image_libraries) and source.is_file():
                shutil.copyfile(source, library / item.name)
    shutil.rmtree(image_libraries)
    run(PYTHON, "-m", "venv", "--without-pip", root / "venv")
    site = root / "venv" / "lib" / "python3.12" / "site-packages"
    # The runtime and test driver are root-owned. The app account only writes its fixture data.
    shutil.copytree(dependencies, site, dirs_exist_ok=True)
    site.chmod(0o755)
    (site / "odoo-fixture.pth").write_text(f"{root}/odoo-packages\n")
    for script in ("fixture.py", "check_upgrade.py", "browser_check.py"):
        shutil.copyfile(Path(__file__).with_name(script), root / script)


def postgres(root: Path, cleanup: ExitStack, bindir: Path) -> tuple[int, str]:
    name = root.name + "pg"
    home = root / "db"
    account(name, home, cleanup)
    sockets = home / "sockets"
    own(sockets, name)
    pgdata = home / "cluster"
    token = uuid4().hex
    secret = home / "secret"
    with secret.open("x") as stream:
        os.fchmod(stream.fileno(), 0o600)
        identity = pwd.getpwnam(name)
        os.fchown(stream.fileno(), identity.pw_uid, identity.pw_gid)
        stream.write(token + "\n")
    try:
        run(
            *as_user(name),
            bindir / "initdb",
            "-D",
            pgdata,
            "-U",
            "ghost_fixture_admin",
            "--auth=scram-sha-256",
            "--pwfile",
            secret,
            "--no-instructions",
        )
    finally:
        secret.unlink()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    pg_ctl = (*as_user(name), bindir / "pg_ctl", "-D", pgdata)
    cleanup.callback(run, *pg_ctl, "-m", "fast", "-w", "stop", check=False)
    run(
        *pg_ctl,
        "-l",
        home / "postgres.log",
        "-w",
        "-o",
        f"-h 127.0.0.1 -p {port} -k {sockets}",
        "start",
    )
    env = {key: val for key, val in os.environ.items() if not key.startswith("PG")}
    env.update(PGPASSWORD=token, GHOSTDEPLOY_FIXTURE_TOKEN=token, PGCONNECT_TIMEOUT="5")
    psql = (
        bindir / "psql",
        "-X",
        "-w",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        "127.0.0.1",
        "-p",
        str(port),
        "-U",
        "ghost_fixture_admin",
    )
    actual = run(
        *psql,
        "-d",
        "postgres",
        "-c",
        "SHOW data_directory",
        env=env,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if Path(actual) != pgdata:
        raise RuntimeError("PostgreSQL did not start from the fixture cluster.")
    run(*psql, "-d", "postgres", "-c", "CREATE DATABASE ghostdeploy_fixture", env=env)
    run(*psql, "-d", "ghostdeploy_fixture", "-f", Path(__file__).with_name("fixture.sql"), env=env)
    return port, token


def remove_unit(unit: str, path: Path) -> None:
    try:
        run(SYSTEMCTL, "stop", unit)
    finally:
        run(SYSTEMCTL, "reset-failed", unit, check=False, capture_output=True)
        path.unlink()


def units(root: Path, cleanup: ExitStack) -> None:
    allowed = []
    cleanup.callback(run, SYSTEMCTL, "daemon-reload")
    for scenario in ("success", "failure"):
        for runtime in ("host", "restored-runtime"):
            unit = f"{root.name}-{scenario}-{runtime}.service"
            state = run(
                SYSTEMCTL,
                "show",
                "--property=LoadState",
                "--value",
                "--",
                unit,
                capture_output=True,
                check=False,
            )
            if state.stdout.strip() != b"not-found":
                raise RuntimeError(f"The generated fixture unit is already in use: {unit}.")
            path = Path("/run/systemd/system") / unit
            with path.open("x") as definition:
                cleanup.callback(remove_unit, unit, path)
                definition.write(
                    "[Service]\nType=exec\n"
                    f"User={root.name}\nWorkingDirectory={root}/work\n"
                    f"Environment=LD_LIBRARY_PATH={root}/native-libs\n"
                    f"ExecStart={root}/venv/bin/python {root}/odoo -c {root}/work/{scenario}/{runtime}/odoo.conf\n"
                    "KillMode=control-group\nSendSIGKILL=yes\nTimeoutStopSec=20s\n"
                    "NoNewPrivileges=true\nIPAddressDeny=any\nIPAddressAllow=localhost\n"
                )
            allowed.extend(f"{SYSTEMCTL} {action} {unit}" for action in ("start", "stop"))
    sudoers = Path("/etc/sudoers.d") / root.name
    with sudoers.open("x") as rule:
        cleanup.callback(sudoers.unlink)
        os.fchmod(rule.fileno(), 0o440)
        rule.write(f"{root.name} ALL=(root) NOPASSWD: {', '.join(allowed)}\n")
    run("/usr/sbin/visudo", "-c", "-f", sudoers)
    run(SYSTEMCTL, "daemon-reload")
    denied = run(
        *as_user(root.name),
        SUDO,
        "-n",
        "-l",
        "--",
        SYSTEMCTL,
        "restart",
        f"{root.name}-success-host.service",
        check=False,
        capture_output=True,
    )
    assert denied.returncode != 0
    denied = run(
        *as_user(root.name),
        SUDO,
        "-n",
        "-l",
        "--",
        "/usr/bin/true",
        check=False,
        capture_output=True,
    )
    assert denied.returncode != 0


def main(dependencies: Path, series: str) -> int:
    if (
        sys.platform != "linux"
        or os.geteuid() != 0
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or platform.freedesktop_os_release().get("VERSION_ID") != "24.04"
        or platform.machine() != "x86_64"
        or sys.version_info[:2] != (3, 12)
        or Path("/proc/1/comm").read_text().strip() != "systemd"
    ):
        print("SKIPPED: combined staging requires a hosted Ubuntu 24.04 VM with systemd PID 1.")
        return 77
    bindir = Path(
        run("/usr/bin/pg_config", "--bindir", capture_output=True, text=True).stdout.strip()
    )
    if not (bindir / "initdb").is_file() or not shutil.which("google-chrome"):
        print("SKIPPED: the runner must supply PostgreSQL binaries and Chrome.")
        return 77
    run(SYSTEMCTL, "show", "--property=Version")
    print(platform.platform(), platform.freedesktop_os_release()["PRETTY_NAME"], flush=True)
    start = monotonic()
    root = Path("/var/lib") / f"gdst{uuid4().hex[:12]}"
    root.mkdir(mode=0o755)
    with ExitStack() as cleanup:
        cleanup.callback(shutil.rmtree, root)
        account(root.name, root / "home", cleanup)
        work = root / "work"
        own(work, root.name)
        stage_runtime(root, dependencies, series)
        port, token = postgres(root, cleanup, bindir)
        connection = work / "connection.json"
        with connection.open("x") as stream:
            os.fchmod(stream.fileno(), 0o600)
            identity = pwd.getpwnam(root.name)
            os.fchown(stream.fileno(), identity.pw_uid, identity.pw_gid)
            json.dump({"port": port, "token": token}, stream)
        units(root, cleanup)
        test_start = monotonic()
        print(f"Combined staging setup completed in {test_start - start:.1f}s", flush=True)
        result = run(
            *as_user(root.name),
            "/usr/bin/env",
            f"PATH={bindir}:/usr/bin:/bin",
            f"LD_LIBRARY_PATH={root}/native-libs",
            root / "venv/bin/python",
            root / "check_upgrade.py",
            "--native",
            root,
            series,
            cwd=root,
            check=False,
            timeout=600,
        ).returncode
        print(
            f"Combined staging assertions completed in {monotonic() - test_start:.1f}s, exit={result}",
            flush=True,
        )
    print(
        "Removed owned Odoo units, accounts, PostgreSQL cluster and fixture storage.", flush=True
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dependencies", type=Path)
    parser.add_argument("version", choices=IMAGES)
    args = parser.parse_args()

    def interrupt(_signum, _frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    raise SystemExit(main(args.dependencies, args.version))
