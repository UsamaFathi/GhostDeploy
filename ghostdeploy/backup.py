import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ghostdeploy.core import GhostError
from ghostdeploy.files import sync_directory, write_atomic
from ghostdeploy.runtime import Host, private_directory


def filestore_size(source: Path) -> int | None:
    try:
        info = source.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(info.st_mode):
        return 4096 + sum(filestore_size(child) or 0 for child in source.iterdir())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise GhostError(
            "Filestore contains a symlink, hardlink or special file. Reconcile it before deployment."
        )
    return info.st_size + 4096


def open_private(path: Path):
    return open(path, "xb", opener=lambda name, flags: os.open(name, flags, 0o600))


def stat_signature(info: os.stat_result) -> tuple:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        # Windows pathname and handle queries need not report the same ctime.
        info.st_ctime_ns if os.name == "posix" else None,
    )


def add_filestore(
    archive: tarfile.TarFile, path: Path, name: str, parent_fd: int | None = None
) -> None:
    local_path = path.name if parent_fd is not None else path
    original = os.stat(local_path, dir_fd=parent_fd, follow_symlinks=False)
    is_dir = stat.S_ISDIR(original.st_mode)
    if "\\" in name or (
        not is_dir and (not stat.S_ISREG(original.st_mode) or original.st_nlink != 1)
    ):
        raise GhostError("Filestore contains a link, special file or unsupported path.")
    entry = tarfile.TarInfo(name)
    entry.mode = stat.S_IMODE(original.st_mode) & 0o777
    entry.mtime = int(original.st_mtime)
    if is_dir and os.name == "nt":
        entry.type = tarfile.DIRTYPE
        archive.addfile(entry)
        for child in sorted(path.iterdir()):
            add_filestore(archive, child, f"{name}/{child.name}")
        return
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    if is_dir:
        flags |= os.O_DIRECTORY
    descriptor = os.open(local_path, flags, dir_fd=parent_fd)
    try:
        if stat_signature(os.fstat(descriptor)) != stat_signature(original):
            raise GhostError("Filestore entry changed while opening it for backup.")
        if is_dir:
            entry.type = tarfile.DIRTYPE
            archive.addfile(entry)
            for child in sorted(os.listdir(descriptor)):
                add_filestore(archive, path / child, f"{name}/{child}", descriptor)
        else:
            entry.size = original.st_size
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                archive.addfile(entry, source)
        if stat_signature(os.fstat(descriptor)) != stat_signature(original):
            raise GhostError("Filestore entry changed during backup. Quiesce all writers.")
    finally:
        os.close(descriptor)


def verify_filestore(output: Path) -> None:
    try:
        with gzip.open(output, "rb") as stream:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                for entry in archive:
                    if not (entry.isdir() or entry.isfile()):
                        raise GhostError("Filestore archive contains an unexpected entry.")
            while stream.read(1024 * 1024):
                pass
    except (tarfile.TarError, EOFError, gzip.BadGzipFile):
        raise GhostError("Filestore archive failed integrity verification.") from None


def archive_filestore(source: Path, output: Path) -> None:
    with open_private(output) as stream, tarfile.open(fileobj=stream, mode="w:gz") as archive:
        if os.name == "posix":
            # Open each ancestor relative to a fixed descriptor, without following links.
            absolute = source.absolute()
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open(absolute.anchor, flags)
            try:
                for part in absolute.parent.parts[1:]:
                    child = os.open(part, flags, dir_fd=parent)
                    os.close(parent)
                    parent = child
                add_filestore(archive, absolute, "filestore", parent)
            finally:
                os.close(parent)
        else:
            add_filestore(archive, source, "filestore")
    verify_filestore(output)


def artifact(path: Path) -> dict:
    with open(
        path,
        "r+b",
        opener=lambda name, flags: os.open(
            name, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        ),
    ) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077))
        ):
            raise GhostError(
                "Backup artifact must be a private, owned regular file without hardlinks."
            )
        size = info.st_size
        if not size:
            raise GhostError("A backup artifact is empty.")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if stat_signature(info) != stat_signature(os.fstat(stream.fileno())):
            raise GhostError("Backup artifact changed during verification.")
        os.fsync(stream.fileno())
    return {"path": str(path), "bytes": size, "sha256": digest}


class Backups:
    def __init__(self, host: Host):
        self.host = host
        self.settings = host.config.deployment.backup
        self.root = Path(self.settings.path)

    def source(self, database: str) -> Path:
        if self.host.filestore is None or database not in self.host.config.deployment.databases:
            raise GhostError(
                "Filestore backup requires an allowlisted database and Odoo data_dir."
            )
        root = self.host.filestore
        if (
            root.resolve() != root
            or not root.parent.is_dir()
            or (root.exists() and not root.is_dir())
        ):
            raise GhostError(
                "Odoo data_dir and its filestore must use real directories without symlinks."
            )
        source = root / database
        if source.is_symlink() or (source.exists() and not source.is_dir()):
            raise GhostError(f"Filestore for {database} must be a real directory or absent.")
        return source

    def check_space(self, databases: list[str]) -> None:
        if not self.settings.enabled:
            raise GhostError("Backups must be enabled for any planned database upgrade.")
        private_directory(self.root)
        required = self.settings.min_free_mib * 1024**2
        for database in databases:
            size = self.host.runner.run(
                [
                    "psql",
                    "--no-psqlrc",
                    "--no-password",
                    "--no-align",
                    "--tuples-only",
                    "--set",
                    "ON_ERROR_STOP=1",
                    "--command",
                    "SELECT pg_database_size(current_database())",
                ],
                label=f"Estimate backup size for {database}",
                env=self.host.pg_env(database),
            )
            if not size.strip().isdigit():
                raise GhostError(f"Could not estimate database size for {database}.")
            required += int(size) + (filestore_size(self.source(database)) or 0)
        if shutil.disk_usage(self.root).free < required:
            raise GhostError(
                f"Backup volume has less than the estimated {required // 1024**2 + 1} MiB required."
            )

    def create(
        self, identifier: str, base: str, target: str, databases: list[str]
    ) -> Iterator[dict]:
        if not self.settings.enabled or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", identifier):
            raise GhostError("Backup creation requires enabled storage and a safe deployment ID.")
        private_directory(self.root)
        directory = self.root / identifier
        directory.mkdir(mode=0o700)
        sync_directory(self.root)
        for database in databases:
            source = self.source(database)
            has_filestore = filestore_size(source) is not None
            dump = directory / f"{database}.dump.partial"
            with open_private(dump):
                pass
            self.host.runner.run(
                [
                    "pg_dump",
                    "--no-password",
                    "--format=custom",
                    "--file",
                    str(dump),
                ],
                label=f"Back up {database}",
                env=self.host.pg_env(database),
                timeout=self.host.timeout,
                capture=False,
            )
            self.host.runner.run(
                [
                    "pg_restore",
                    f"--file={os.devnull}",
                    "--exit-on-error",
                    str(dump),
                ],
                label=f"Verify PostgreSQL archive for {database}",
                timeout=self.host.timeout,
                capture=False,
            )
            dump_info = self.publish(dump)
            store = {"status": "absent_at_snapshot"}
            if has_filestore:
                archive_path = directory / f"{database}.filestore.tar.gz.partial"
                archive_filestore(source, archive_path)
                store = {"status": "verified", **self.publish(archive_path)}
            record = {
                "deployment_id": identifier,
                "database": database,
                "timestamp": datetime.now(UTC).isoformat(),
                "base_sha": base,
                "target_sha": target,
                "status": "verified",
                "dump": dump_info,
                "filestore": store,
                "test_restore": "not_performed",
            }
            write_atomic(directory / f"{database}.json", json.dumps(record, indent=2) + "\n")
            yield record

    @staticmethod
    def publish(partial: Path) -> dict:
        info = artifact(partial)
        verified = partial.with_suffix("")
        os.link(partial, verified)
        partial.unlink()
        sync_directory(verified.parent)
        return {**info, "path": str(verified)}
