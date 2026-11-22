import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ghostdeploy.config import SHA
from ghostdeploy.core import CommandError, GhostError, Runner, git_environment


@dataclass(frozen=True)
class Change:
    status: str
    paths: tuple[str, ...]


class GitRepository:
    def __init__(self, path: Path, branch: str, runner: Runner):
        self.path = path.resolve()
        self.branch = branch
        self.runner = runner
        self.remote_ref = f"refs/remotes/origin/{branch}"
        root = self.git("rev-parse", "--show-toplevel", label="Locate Git checkout")
        if Path(os.fsdecode(root.rstrip(b"\r\n"))).resolve() != self.path:
            raise GhostError("The configured Git checkout must be the repository root.")
        grafts = self.git(
            "rev-parse", "--git-path", "info/grafts", label="Locate Git graft metadata"
        )
        if os.path.lexists(self.path / os.fsdecode(grafts.rstrip(b"\r\n"))):
            raise GhostError("Use an unmodified Git history without graft metadata.")
        shallow = self.git(
            "rev-parse", "--is-shallow-repository", label="Check history completeness"
        )
        if shallow.strip() != b"false":
            raise GhostError("Use a full Git clone. Shallow history is unsupported.")
        try:
            self.git(
                "config",
                "--get-regexp",
                r"^(extensions\.partialclone|remote\..+\.promisor)$",
                label="Check whether the Git clone is partial",
            )
        except CommandError as exc:
            if exc.returncode != 1:
                raise
        else:
            raise GhostError("Use a full Git clone. Partial and promisor clones are unsupported.")

    def git(self, *args: str, label: str, timeout: int = 60) -> bytes:
        return self.runner.run(
            [
                "git",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-c",
                "core.fsmonitor=false",
                "-c",
                "submodule.recurse=false",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "protocol.file.allow=never",
                "-c",
                "http.sslVerify=true",
                "-c",
                "http.followRedirects=false",
                *args,
            ],
            label=label,
            cwd=self.path,
            env=git_environment(),
            timeout=timeout,
        )

    def resolve(self, ref: str) -> str:
        value = (
            self.git(
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{ref}^{{commit}}",
                label=f"Resolve {ref}",
            )
            .decode("ascii")
            .strip()
        )
        if not SHA.fullmatch(value):
            raise GhostError("Git did not return a full commit SHA.")
        return value

    def commit(self, value: str) -> str:
        if not SHA.fullmatch(value):
            raise GhostError(
                "Supply a full lowercase Git commit SHA (40 or 64 hexadecimal characters)."
            )
        if self.resolve(value) != value:
            raise GhostError("The supplied SHA must identify a commit object.")
        return value

    def default_target(self, base: str) -> tuple[str, str | None]:
        try:
            self.git(
                "show-ref",
                "--verify",
                "--quiet",
                self.remote_ref,
                label="Check local deployment branch ref",
            )
        except CommandError as exc:
            if exc.returncode != 1:
                raise
            return (
                base,
                "No local remote-tracking branch found. Pass --target-sha to compare a different commit.",
            )
        return self.resolve(self.remote_ref), None

    def diff(self, base: str, target: str) -> list[Change]:
        fields = iter(
            self.git(
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                "--no-ext-diff",
                "--no-textconv",
                base,
                target,
                "--",
                label="Compare Git commits",
            ).split(b"\0")[:-1]
        )
        return [
            Change(
                status,
                tuple(os.fsdecode(next(fields)) for _ in range(2 if status[0] in "RC" else 1)),
            )
            for field in fields
            if (status := field.decode("ascii"))
        ]

    def tree(self, commit: str) -> dict[PurePosixPath, tuple[bytes, str]]:
        entries = self.git("ls-tree", "-r", "-z", commit, label="Read commit tree").split(b"\0")[
            :-1
        ]
        tree = {}
        for entry in entries:
            metadata, path = entry.split(b"\t", 1)
            mode, _, blob = metadata.split(b" ")
            tree[PurePosixPath(os.fsdecode(path))] = mode, blob.decode("ascii")
        return tree

    def manifest(self, blob: str, path: PurePosixPath) -> bytes:
        size = self.git("cat-file", "-s", blob, label="Read manifest size")
        if int(size) > 256 * 1024:
            raise GhostError(f"Odoo manifest exceeds 256 KiB: {str(path)!r}.")
        return self.git("cat-file", "blob", blob, label="Read Odoo manifest")

    def ancestor(self, older: str, newer: str) -> bool:
        try:
            self.git("merge-base", "--is-ancestor", older, newer, label="Verify Git ancestry")
        except CommandError as exc:
            if exc.returncode == 1:
                return False
            raise
        return True

    def clean_head(self) -> str:
        current = self.git("symbolic-ref", "--quiet", "HEAD", label="Check active Git branch")
        if current.decode("utf-8").strip() != f"refs/heads/{self.branch}":
            raise GhostError("The checkout must be on the configured branch.")
        if self.git(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored",
            label="Check checkout cleanliness",
        ):
            raise GhostError(
                "The checkout has tracked, untracked or ignored changes. Reconcile it before deploying."
            )
        index = self.git("ls-files", "-v", "-z", label="Check Git index flags")
        if any(entry[:1] != b"H" for entry in index.split(b"\0")[:-1]):
            raise GhostError(
                "Deployments require a full index with no skip-worktree or assume-unchanged entries."
            )
        for marker in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-apply",
            "rebase-merge",
            "sequencer",
            "BISECT_START",
        ):
            location = self.git(
                "rev-parse", "--git-path", marker, label="Check for an unfinished Git operation"
            )
            if (self.path / os.fsdecode(location.rstrip(b"\r\n"))).exists():
                raise GhostError("Complete or abort the unfinished Git operation first.")
        return self.resolve("HEAD")

    def fetch(self) -> None:
        remote = (
            self.git("remote", "get-url", "origin", label="Inspect the origin URL")
            .decode("utf-8")
            .strip()
        )
        if not re.fullmatch(
            r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            remote,
        ):
            raise GhostError(
                "Fetch requires a credential-free GitHub origin URL using HTTPS or GitHub SSH."
            )
        self.git(
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "origin",
            f"refs/heads/{self.branch}:{self.remote_ref}",
            label="Fetch the configured branch",
            timeout=180,
        )

    def sync(self, base: str, target: str) -> None:
        if self.clean_head() != base:
            raise GhostError("The checkout changed after planning.")
        self.git(
            "merge",
            "--ff-only",
            "--no-edit",
            "--no-autostash",
            target,
            label="Fast-forward the checkout",
            timeout=180,
        )
        if self.clean_head() != target:
            raise GhostError("Git did not leave a clean checkout at the exact requested commit.")
