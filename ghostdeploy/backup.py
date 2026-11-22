import gzip
import hashlib
import json
import os
import shutil
import stat
import tarfile
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
    if not stat.S_ISREG(info.st_mode):
        raise GhostError(
            "Filestore contains a symlink or special file. Reconcile it before deployment."
        )
    return info.st_size + 4096


def archive_filestore(source: Path, output: Path) -> None:
    def regular_only(entry: tarfile.TarInfo) -> tarfile.TarInfo:
        if not (entry.isfile() or entry.isdir()):
            raise GhostError("Filestore contains an unsupported archive entry.")
        return entry

    with tarfile.open(output, "x:gz", dereference=False) as archive:
        archive.add(source, arcname="filestore", filter=regular_only)
    with gzip.open(output, "rb") as stream:
        while stream.read(1024 * 1024):
            pass


def artifact(path: Path) -> dict:
    path.chmod(0o600)
    with path.open("r+b") as stream:
        size = os.fstat(stream.fileno()).st_size
        if not size:
            raise GhostError("A backup artifact is empty.")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
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

    def create(self, identifier: str, base: str, target: str, databases: list[str]) -> list[dict]:
        directory = self.root / identifier
        directory.mkdir(mode=0o700)
        sync_directory(self.root)
        records = []
        for database in databases:
            source = self.source(database)
            has_filestore = filestore_size(source) is not None
            dump = directory / f"{database}.dump"
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
            )
            store = {"status": "absent_at_snapshot"}
            if has_filestore:
                archive_path = directory / f"{database}.filestore.tar.gz"
                archive_filestore(source, archive_path)
                store = {"status": "verified", **artifact(archive_path)}
            record = {
                "deployment_id": identifier,
                "database": database,
                "timestamp": datetime.now(UTC).isoformat(),
                "base_sha": base,
                "target_sha": target,
                "status": "verified",
                "dump": artifact(dump),
                "filestore": store,
            }
            write_atomic(directory / f"{database}.json", json.dumps(record, indent=2) + "\n")
            records.append(record)
        return records
