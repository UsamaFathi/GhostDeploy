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
import stat
import subprocess
import sys
import tarfile
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
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
FIXTURE_PARENTS = ("/srv", "/var/lib", "/var/tmp")


def fixture_parent() -> Path | None:
    for parent in map(Path, FIXTURE_PARENTS):
        if not parent.is_dir() or parent.resolve() != parent:
            continue
        if os.statvfs(parent).f_flag & os.ST_NOEXEC:
            continue
        for directory in (parent, *parent.parents):
            info = directory.stat()
            if info.st_uid != 0 or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
                print(f"Fixture storage rejected because of ownership or mode on {directory}.")
                break
        else:
            return parent
    return None


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
    run(*DOCKER, "pull", image, timeout=240)
    with TemporaryDirectory(dir=root) as directory:
        payload = Path(directory)
        export = payload / "runtime.tar"
        container = run(
            *DOCKER,
            "create",
            "--name",
            f"{root.name}-payload",
            "--network",
            "none",
            "--read-only",
            "--user",
            "odoo",
            "--entrypoint",
            "/usr/bin/tar",
            image,
            "-C",
            "/usr",
            "--dereference",
            "--hard-dereference",
            # These optional PDF font links are dangling in both pinned images.
            "--exclude=lib/python3/dist-packages/reportlab/fonts/DejaVu*.ttf",
            "-cf",
            "-",
            "bin/odoo",
            "lib/python3/dist-packages",
            "lib/x86_64-linux-gnu",
            capture_output=True,
            text=True,
        ).stdout.strip()
        try:
            # Resolve packaged data symlinks in the image's namespace, where their targets live.
            with export.open("wb") as archive:
                run(*DOCKER, "start", "--attach", container, stdout=archive, timeout=120)
        finally:
            run(*DOCKER, "rm", "-f", container)
        with tarfile.open(export) as archive:
            archive.extractall(payload / "files", filter="data")
        (payload / "files/bin/odoo").rename(root / "odoo")
        (payload / "files/lib/python3/dist-packages").rename(root / "odoo-packages")
        (payload / "files/lib/x86_64-linux-gnu").rename(root / "image-libs")
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


def stop_postgres(pg_ctl: tuple, data: Path) -> None:
    if (data / "postmaster.pid").is_file():
        run(*pg_ctl, "-m", "fast", "-w", "stop")


def postgres(home: Path, name: str, cleanup: ExitStack, bindir: Path) -> dict[str, str | int]:
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
    cleanup.callback(stop_postgres, pg_ctl, pgdata)
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
    return {"port": port, "token": token}


def unit_load_state(unit: str) -> bytes:
    return run(
        SYSTEMCTL,
        "show",
        "--property=LoadState",
        "--value",
        "--",
        unit,
        capture_output=True,
        check=False,
    ).stdout.strip()


def remove_unit(unit: str, path: Path) -> None:
    try:
        run(SYSTEMCTL, "stop", unit)
    finally:
        run(SYSTEMCTL, "reset-failed", unit, check=False, capture_output=True)
        path.unlink()


def units(root: Path, cleanup: ExitStack) -> None:
    allowed = []
    cleanup.callback(run, SYSTEMCTL, "daemon-reload")
    for scenario, runtime in (
        ("success", "host"),
        ("failure", "host"),
        ("failure", "restored-runtime"),
    ):
        unit = f"{root.name}-{scenario}-{runtime}.service"
        if unit_load_state(unit) != b"not-found":
            raise RuntimeError(f"The generated fixture unit is already in use: {unit}.")
        path = Path("/run/systemd/system") / unit
        with path.open("x") as definition:
            cleanup.callback(remove_unit, unit, path)
            definition.write(
                "[Service]\nType=exec\n"
                f"User={root.name}\nWorkingDirectory={root}/work\n"
                f"Environment=LD_LIBRARY_PATH={root}/native-libs\n"
                "Environment=PYTHONDONTWRITEBYTECODE=1\nUMask=0077\n"
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


def run_assertions(root: Path, bindir: Path, series: str) -> int:
    unit = f"{root.name}-checks.service"
    if unit_load_state(unit) != b"not-found":
        raise RuntimeError("The generated fixture driver unit is already in use.")
    try:
        return run(
            "/usr/bin/systemd-run",
            "--wait",
            "--pipe",
            "--collect",
            "--quiet",
            "--unit",
            unit,
            "--property=Type=exec",
            "--property=TimeoutStopSec=5s",
            f"--uid={root.name}",
            f"--working-directory={root}",
            f"--setenv=HOME={root}/home",
            f"--setenv=PATH={bindir}:/usr/bin:/bin",
            f"--setenv=LD_LIBRARY_PATH={root}/native-libs",
            f"--setenv=PLAYWRIGHT_BROWSERS_PATH={root}/browser-binaries",
            root / "venv/bin/python",
            root / "check_upgrade.py",
            "--native",
            root,
            series,
            cwd=root,
            check=False,
            timeout=600,
        ).returncode
    finally:
        if unit_load_state(unit) == b"loaded":
            run(SYSTEMCTL, "stop", unit)


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
    if not all(shutil.which(tool) for tool in ("pg_config", "google-chrome", DOCKER[0])):
        print("SKIPPED: the runner must supply PostgreSQL, Chrome and a local Docker client.")
        return 77
    bindir = Path(
        run("/usr/bin/pg_config", "--bindir", capture_output=True, text=True).stdout.strip()
    )
    if not (bindir / "initdb").is_file():
        print("SKIPPED: the runner must supply PostgreSQL server binaries.")
        return 77
    run(SYSTEMCTL, "show", "--property=Version")
    print(platform.platform(), platform.freedesktop_os_release()["PRETTY_NAME"], flush=True)
    start = monotonic()
    parent = fixture_parent()
    if parent is None:
        print("SKIPPED: no trusted, executable parent directory for fixture storage.")
        return 77
    root = parent / f"gdst{uuid4().hex[:12]}"
    print(f"Fixture storage: {root}", flush=True)
    root.mkdir(mode=0o755)
    with ExitStack() as cleanup:
        cleanup.callback(shutil.rmtree, root)
        account(root.name, root / "home", cleanup)
        for directory in (".local", ".local/state", ".local/state/ghostdeploy"):
            own(root / "home" / directory, root.name)
        work = root / "work"
        own(work, root.name)
        stage_runtime(root, dependencies, series)
        # Download the pinned headless executable only. The runner supplies the shared libraries.
        run(
            "/usr/bin/env",
            f"PLAYWRIGHT_BROWSERS_PATH={root}/browser-binaries",
            root / "venv/bin/python",
            "-m",
            "playwright",
            "install",
            "--only-shell",
            "chromium",
            timeout=150,
        )
        databases = {
            "live": postgres(root / "db", root.name + "pg", cleanup, bindir),
            "recovery": postgres(root / "recovery-db", root.name + "rs", cleanup, bindir),
        }
        connection = work / "connection.json"
        with connection.open("x") as stream:
            os.fchmod(stream.fileno(), 0o600)
            identity = pwd.getpwnam(root.name)
            os.fchown(stream.fileno(), identity.pw_uid, identity.pw_gid)
            json.dump(databases, stream)
        units(root, cleanup)
        test_start = monotonic()
        print(f"Combined staging setup completed in {test_start - start:.1f}s", flush=True)
        result = run_assertions(root, bindir, series)
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
