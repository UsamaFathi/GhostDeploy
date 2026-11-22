import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ghostdeploy.backup import Backups
from ghostdeploy.config import MODULE, SHA, Config
from ghostdeploy.core import GhostError, Runner
from ghostdeploy.files import sync_directory, write_atomic
from ghostdeploy.git import GitRepository
from ghostdeploy.planning import Plan, build_plan
from ghostdeploy.runtime import Host, private_directory


@contextmanager
def deployment_lock(directory: Path, unit: str) -> Iterator[None]:
    import fcntl

    private_directory(directory)
    descriptor = os.open(directory / f"{unit}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise GhostError(
                "Deployment lock must be a private regular file owned by the service account."
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GhostError("A deployment for this service is already running.") from None
        yield
    finally:
        os.close(descriptor)


@dataclass
class Report:
    id: str
    unit: str
    started_at: str
    base_sha: str
    target_sha: str
    phase: str = "prepared"
    status: str = "in_progress"
    upgraded_databases: list[str] = field(default_factory=list)
    in_progress_database: str | None = None
    backup_directory: str | None = None
    backups: list[dict] = field(default_factory=list)
    upgrades: dict[str, tuple[str, ...]] = field(default_factory=dict)
    error: str | None = None
    recovery: str | None = None
    updated_at: str | None = None


class Journal:
    def __init__(self, root: Path, report: Report):
        self.root = root
        self.pending = root / f"{report.unit}.pending.json"
        self.report = report

    def record(self, phase: str, *, create: bool = False) -> None:
        self.report.phase = phase
        self.report.updated_at = datetime.now(UTC).isoformat()
        write_atomic(self.pending, self.content(), replace=not create)

    def content(self) -> str:
        return json.dumps(asdict(self.report), indent=2) + "\n"

    def finish(self, *, clear: bool) -> None:
        self.record(self.report.phase)
        write_atomic(self.root / f"{self.report.id}.json", self.content())
        if clear:
            self.pending.unlink()
            sync_directory(self.root)


def select_upgrades(
    config: Config, plan: Plan, selected: list[str], approve_review: bool
) -> dict[str, tuple[str, ...]]:
    if plan.blockers:
        raise GhostError(" ".join(plan.blockers))
    if plan.review and not approve_review:
        raise GhostError(
            "Review the exact plan and pass --approve-review. " + " ".join(plan.review)
        )
    mapping = plan.installed_by_database
    if mapping is None:
        raise GhostError("Deployment requires inspected database module states.")
    eligible = {module for modules in mapping.values() for module in modules}
    if "all" in eligible:
        raise GhostError("Odoo reserves 'all' in its update option. Handle that addon manually.")
    approved = set(selected)
    if any(not MODULE.fullmatch(module) for module in approved) or approved - eligible:
        raise GhostError(
            "--upgrade may only name candidates installed on an allowlisted database."
        )
    if (config.deployment.upgrade_policy == "manual_review" or selected) and eligible - approved:
        raise GhostError(
            "Approve each installed candidate with --upgrade MODULE. Missing: "
            + ", ".join(sorted(eligible - approved))
        )
    return {database: modules for database, modules in mapping.items() if modules}


def verify_paths(config: Config) -> None:
    checkout = Path(config.repository.checkout_path)
    if checkout.resolve() != checkout:
        raise GhostError("The Linux Git checkout must be a canonical path without symlinks.")
    for root in map(Path, config.odoo.addons_path):
        if root.resolve() != root:
            raise GhostError("Custom addon roots must be canonical paths without symlinks.")
    outside = [
        config.odoo.executable,
        config.odoo.config_file,
        config.deployment.backup.path,
        config.deployment.state_path,
    ]
    if config.odoo.interpreter:
        outside.append(config.odoo.interpreter)
    if any(Path(path).resolve().is_relative_to(checkout) for path in outside):
        raise GhostError("Runtime files and storage must resolve outside the deployment checkout.")


class Executor:
    def __init__(self, config: Config, runner: Runner, host: Host):
        self.config = config
        self.runner = runner
        self.host = host
        self.directory = Path(config.deployment.state_path)

    def execute(
        self,
        target: str,
        *,
        selected: list[str],
        approve_review: bool,
        confirm_production: bool,
        fetch: bool,
    ) -> Report:
        settings = self.config.deployment
        if settings.environment == "production" and not confirm_production:
            raise GhostError("Production deployment requires --confirm-production.")
        if not SHA.fullmatch(target):
            raise GhostError("Execution requires --target-sha with a full lowercase commit SHA.")
        self.host.require_identity()
        verify_paths(self.config)
        unit = self.config.odoo.unit
        with deployment_lock(self.directory, unit):
            pending = self.directory / f"{unit}.pending.json"
            if pending.exists() or pending.is_symlink():
                raise GhostError(
                    f"An unfinished deployment blocks execution. Reconcile the host and archive {pending}."
                )
            self.host.preflight()
            if self.host.filestore:
                data = self.host.filestore.parent.resolve()
                for location in (
                    self.config.repository.checkout_path,
                    settings.backup.path,
                    settings.state_path,
                ):
                    path = Path(location).resolve()
                    if data.is_relative_to(path) or path.is_relative_to(data):
                        raise GhostError(
                            "Odoo data must be separate from the Git checkout, backup and state directories."
                        )
            repo = GitRepository(
                Path(self.config.repository.checkout_path),
                self.config.repository.branch,
                self.runner,
            )
            base = repo.clean_head()
            if fetch:
                repo.fetch()
            tip = repo.resolve(repo.remote_ref)
            target = repo.commit(target)
            if not repo.ancestor(target, tip):
                raise GhostError(
                    "The exact target must be reachable from the configured origin branch."
                )
            installed = self.host.inspect_databases()
            plan = build_plan(
                self.config, repo, base_sha=base, target_sha=target, installed=installed
            )
            upgrades = select_upgrades(self.config, plan, selected, approve_review)
            backups = Backups(self.host)
            if upgrades:
                self.host.prepare_upgrades(
                    {module for modules in upgrades.values() for module in modules}
                )
                for database in upgrades:
                    self.host.require_database_role(database)
                backups.check_space(list(upgrades))
            report = Report(
                uuid4().hex, unit, datetime.now(UTC).isoformat(), base, target, upgrades=upgrades
            )
            journal = Journal(self.directory, report)
            journal.record("prepared", create=True)
            self.apply(journal, repo, plan, installed, backups)
            return report

    def apply(
        self, journal: Journal, repo: GitRepository, plan: Plan, installed: dict, backups: Backups
    ) -> None:
        report = journal.report
        try:
            if plan.restart_required:
                journal.record("stopping_service")
                self.host.service("stop")
                self.host.require_inactive()
                journal.record("service_stopped")
                if self.host.inspect_databases() != installed:
                    raise GhostError(
                        "Database module state changed after planning. Replan after reconciling writers."
                    )
            if report.upgrades:
                report.backup_directory = str(backups.root / report.id)
                journal.record("creating_backups")
                backups.check_space(list(report.upgrades))
                report.backups = backups.create(
                    report.id, report.base_sha, report.target_sha, list(report.upgrades)
                )
            if report.base_sha != report.target_sha:
                if plan.restart_required:
                    self.host.require_inactive()
                journal.record("synchronizing_code")
                repo.sync(report.base_sha, report.target_sha)
                journal.record("code_synchronized")
            for database, modules in report.upgrades.items():
                self.host.require_inactive()
                report.in_progress_database = database
                journal.record("upgrading_database")
                self.host.upgrade(database, modules)
                report.upgraded_databases.append(database)
                journal.record("verifying_database")
                if self.host.inspect_databases() != installed:
                    raise GhostError(
                        "Installed modules or pending module states changed unexpectedly during an upgrade."
                    )
                report.in_progress_database = None
                journal.record("database_upgraded")
            if plan.restart_required:
                self.host.require_inactive()
                journal.record("starting_service")
                self.host.service("start")
                journal.record("checking_health")
                self.host.health()
                self.host.service_process(self.host.systemd())
        except (GhostError, OSError, KeyboardInterrupt) as exc:
            self.failed(journal, repo, exc, plan.restart_required)
            return

        report.status = "unchanged" if report.base_sha == report.target_sha else "succeeded"
        journal.record("complete")
        journal.finish(clear=True)

    def failed(
        self, journal: Journal, repo: GitRepository, exc: BaseException, restart: bool
    ) -> None:
        report = journal.report
        report.status = "failed"
        report.error = (
            str(exc)
            if isinstance(exc, GhostError)
            else (
                "Interrupted by operator."
                if isinstance(exc, KeyboardInterrupt)
                else "A filesystem operation failed."
            )
        )
        clear = False
        if report.phase in {"service_stopped", "creating_backups"}:
            try:
                if repo.clean_head() != report.base_sha:
                    raise GhostError("Original checkout is no longer verified.")
                self.host.require_inactive()
                self.host.service("start")
                self.host.health()
                self.host.service_process(self.host.systemd())
            except (GhostError, OSError):
                report.recovery = "Could not verify the original service after the failure. Inspect the unit and checkout."
            else:
                report.recovery = "Original checkout and service verified. Inspect the failure and retry after correction."
                clear = True
        elif restart and report.phase in {"starting_service", "checking_health"}:
            try:
                self.host.service("stop")
                self.host.require_inactive()
            except (GhostError, OSError):
                report.recovery = "Could not confirm Odoo stopped. Isolate traffic and inspect the unit immediately."
            else:
                report.recovery = "Odoo is stopped after a start or health failure. Verify the code and database states."
        else:
            report.recovery = "Inspect the service, Git checkout and all reported database states before returning Odoo to service."
        if not clear:
            report.recovery += " If a migration ran, verify database and filestore compatibility together. Archive the pending journal only after reconciliation."
        journal.finish(clear=clear)
