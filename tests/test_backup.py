import os
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from ghostdeploy.backup import Backups, archive_filestore, filestore_size, verify_filestore
from ghostdeploy.config import validate
from ghostdeploy.core import GhostError
from ghostdeploy.runtime import Host, private_directory


def test_empty_filestore_has_valid_archive_and_truncation_is_rejected(tmp_path):
    directory = tmp_path / "store"
    directory.mkdir()
    output = tmp_path / "store.tar.gz"
    archive_filestore(directory, output)
    with tarfile.open(output) as archive:
        assert [(entry.name, entry.isdir()) for entry in archive] == [("filestore", True)]
    output.write_bytes(output.read_bytes()[:-6])
    with pytest.raises(GhostError, match="integrity verification"):
        verify_filestore(output)


def test_interrupted_archive_is_left_under_partial_name(tmp_path, monkeypatch):
    source = tmp_path / "filestore"
    source.mkdir()
    partial = tmp_path / "store.tar.gz.partial"

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr("ghostdeploy.backup.add_filestore", interrupt)
    with pytest.raises(KeyboardInterrupt):
        archive_filestore(source, partial)
    assert partial.is_file()
    assert not partial.with_suffix("").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_filestore_rejects_links_and_special_files(tmp_path, kind):
    directory = tmp_path / "store"
    directory.mkdir()
    outside = tmp_path / "unrelated.txt"
    outside.write_text("separate fixture", encoding="utf-8")
    entry = directory / "object"
    if kind == "symlink":
        try:
            entry.symlink_to(outside)
        except OSError:
            pytest.skip("Host user cannot create symlinks")
    elif kind == "hardlink":
        os.link(outside, entry)
    elif hasattr(os, "mkfifo"):
        os.mkfifo(entry)
    else:
        pytest.skip("Host has no named FIFOs")
    with pytest.raises(GhostError, match="link|special file"):
        filestore_size(directory)
    output = tmp_path / "store.partial"
    with pytest.raises(GhostError, match="link|special file"):
        archive_filestore(directory, output)
    assert outside.read_text(encoding="utf-8") == "separate fixture"


def test_no_space_refuses_backup_before_creating_any_artifact(tmp_path, config_data, monkeypatch):
    class EstimateRunner:
        def run(self, args, **kwargs):
            assert "pg_database_size" in args[-1]
            return b"10000000"

    host = Host(validate(config_data), EstimateRunner())
    host.filestore = tmp_path / "filestore"
    backups = Backups(host)
    backups.root = tmp_path / "backups"
    backups.root.mkdir(mode=0o700)
    if os.name == "nt":
        monkeypatch.setattr("ghostdeploy.backup.private_directory", lambda path: None)
    with patch("ghostdeploy.backup.shutil.disk_usage") as disk:
        disk.return_value.free = 1
        with pytest.raises(GhostError, match="Backup volume"):
            backups.check_space(["company_a"])
    assert not list(backups.root.iterdir())


@pytest.mark.skipif(os.name != "posix", reason="POSIX storage permissions required")
def test_private_storage_rejects_world_access(tmp_path: Path):
    directory = tmp_path / "open"
    directory.mkdir(mode=0o755)
    with pytest.raises(GhostError, match="mode 0700"):
        private_directory(directory)
