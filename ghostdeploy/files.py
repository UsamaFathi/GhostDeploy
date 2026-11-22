import os
import tempfile
from pathlib import Path


def sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_atomic(path: Path, content: str, *, replace: bool = False) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temp.unlink()
            raise

    try:
        if replace:
            temp.replace(path)
        else:
            os.link(temp, path)
        sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)
