"""Read-only suggestions. No candidate executable or database is probed."""

import configparser
import os
import platform
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ghostdeploy.config import NAME, linux_path
from ghostdeploy.core import GhostError, Runner
from ghostdeploy.runtime import SYSTEMCTL


@dataclass(frozen=True)
class ServiceCandidate:
    name: str
    state: str


@dataclass(frozen=True)
class InstallHints:
    user: str | None = None
    executable: str | None = None
    interpreter: str | None = None
    config_file: str | None = None
    version: Literal["18.0", "19.0"] | None = None
    addon_roots: tuple[str, ...] = ()
    checkouts: tuple[str, ...] = ()
    declared_databases: tuple[str, ...] = ()


def is_ubuntu() -> bool:
    if platform.system() != "Linux":
        return False
    try:
        return platform.freedesktop_os_release().get("ID") == "ubuntu"
    except OSError:
        return False


def read_regular(path: Path, limit: int = 65_536) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Expected a regular file")
        content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError("File exceeds the discovery read limit")
        return content


def list_services(runner: Runner) -> tuple[ServiceCandidate, ...]:
    raw = runner.run(
        [
            SYSTEMCTL,
            "list-units",
            "--all",
            "--type=service",
            "--no-legend",
            "--plain",
            "--no-pager",
            "*odoo*.service",
        ],
        label="List local Odoo systemd units",
        timeout=8,
    )
    candidates = {}
    for line in raw.decode(errors="replace").splitlines():
        values = line.split(maxsplit=4)
        if len(values) < 4 or not values[0].endswith(".service"):
            continue
        name = values[0].removesuffix(".service")
        if NAME.fullmatch(name) and "odoo" in name.lower():
            candidates[name] = ServiceCandidate(
                name, values[2] if values[2].isascii() and values[2].isalpha() else "unknown"
            )
    return tuple(candidates[name] for name in sorted(candidates))


def safe_paths(values: list[str]) -> tuple[str, ...]:
    paths = {}
    for item in values:
        try:
            paths[linux_path(item.strip())] = None
        except ValueError:
            continue
    return tuple(paths)


def hints_from_config(path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(read_regular(path).decode("utf-8"))
        options = parser["options"]
        roots = safe_paths(options.get("addons_path", "").split(","))
        databases = tuple(
            dict.fromkeys(
                name
                for raw in options.get("db_name", "").split(",")
                if (name := raw.strip())
                and name.lower() not in {"false", "none"}
                and NAME.fullmatch(name)
            )
        )
        return roots, databases
    except (OSError, ValueError, KeyError, configparser.Error):
        return (), ()


def find_checkouts(roots: tuple[str, ...]) -> tuple[str, ...]:
    checkouts = {}
    for root in roots:
        try:
            path = Path(root).resolve()
            for directory in (path, *path.parents):
                if directory.parent != directory and (directory / ".git").exists():
                    checkouts[linux_path(directory.as_posix())] = None
                    break
        except (OSError, ValueError, RuntimeError):
            continue
    return tuple(checkouts)


def detected_series(executable: str) -> Literal["18.0", "19.0"] | None:
    entry = Path(executable)
    releases = [entry.parent / "odoo" / "release.py"]
    if executable == "/usr/bin/odoo":
        releases.append(Path("/usr/lib/python3/dist-packages/odoo/release.py"))
    for path in releases:
        try:
            content = read_regular(path)
        except (OSError, ValueError):
            continue
        match = re.search(rb"(?m)^version_info\s*=\s*\(\s*(18|19)\s*,\s*0\s*,", content)
        if match:
            return "18.0" if match.group(1) == b"18" else "19.0"
    return None


def inspect_service(candidate: ServiceCandidate, runner: Runner) -> InstallHints:
    if not NAME.fullmatch(candidate.name):
        raise GhostError("Invalid service candidate name.")
    unit = f"{candidate.name}.service"
    output = runner.run(
        [SYSTEMCTL, "show", "--property=Id,User,MainPID", "--", unit],
        label="Read selected systemd unit",
        timeout=8,
    )
    info = dict(
        line.split("=", 1) for line in output.decode(errors="replace").splitlines() if "=" in line
    )
    if info.get("Id") != unit:
        return InstallHints()
    user = info.get("User", "")
    user = user if user != "root" and NAME.fullmatch(user) else None
    pid = info.get("MainPID", "")
    if not re.fullmatch(r"[0-9]{1,10}", pid) or int(pid) < 2:
        return InstallHints(user=user)
    try:
        proc = Path(f"/proc/{pid}")
        uid = os.geteuid()
        if uid == 0 or proc.stat().st_uid != uid:
            return InstallHints(user=user)
        argv = read_regular(proc / "cmdline").decode("utf-8").rstrip("\0").split("\0")
        if (
            len(argv) not in {3, 4}
            or argv[-2] != "-c"
            or any(value != linux_path(value) for value in [*argv[:-2], argv[-1]])
        ):
            return InstallHints(user=user)
    except (OSError, ValueError):
        return InstallHints(user=user)
    executable, config_file = argv[-3], argv[-1]
    roots, databases = hints_from_config(Path(config_file))
    return InstallHints(
        user=user,
        executable=executable,
        interpreter=argv[0] if len(argv) == 4 else None,
        config_file=config_file,
        version=detected_series(executable),
        addon_roots=roots,
        checkouts=find_checkouts(roots),
        declared_databases=databases,
    )
