import errno
import json
import os
import socket
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ghostdeploy.backup import Backups
from ghostdeploy.config import MODULE, SHA, Config
from ghostdeploy.core import GhostError, Runner, deployment_signals
from ghostdeploy.files import sync_directory, write_atomic
from ghostdeploy.git import GitRepository
from ghostdeploy.planning import Plan, build_plan
from ghostdeploy.runtime import DatabaseState, HealthEvidence, Host, private_directory


@contextmanager
def deployment_lock(directory: Path, unit: str) -> Iterator[None]:
    import fcntl

    private_directory(directory)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as guard:
        try:
            guard.bind(f"\0ghostdeploy:{unit}")
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            raise GhostError("A deployment for this service is already running.") from None
        descriptor = os.open(
            directory / f"{unit}.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
            ):
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


class JournalError(GhostError):
    """Evidence could not be safely persisted."""


def bind_state_directory(unit: str, directory: Path, home: Path | None = None) -> None:
    """A stable service account records which path owns its recovery history."""
    import pwd

    anchor = (home or Path(pwd.getpwuid(os.geteuid()).pw_dir)).resolve()
    for component in (".local", "state", "ghostdeploy"):
        anchor = anchor / component
        anchor.mkdir(mode=0o700, exist_ok=True)
        metadata = anchor.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_mode & 0o022
        ):
            raise GhostError(
                "The service account's GhostDeploy state binding needs trusted directories under HOME."
            )
    private_directory(anchor)
    binding = anchor / f"{unit}.state"
    expected = str(directory.resolve()) + "\n"
    try:
        descriptor = os.open(binding, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        write_atomic(binding, expected)
        return
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
        ):
            raise GhostError(
                "The service state binding must be a private regular file without links."
            )
        if stream.read(4097) != expected.encode("utf-8"):
            raise GhostError(
                "This service is bound to another state directory. Reconcile its original journal before moving storage."
            )


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
    verified_databases: list[str] = field(default_factory=list)
    in_progress_database: str | None = None
    backup_directory: str | None = None
    backups: list[dict] = field(default_factory=list)
    upgrades: dict[str, tuple[str, ...]] = field(default_factory=dict)
    database_oids: dict[str, int] = field(default_factory=dict)
    expected_versions: dict[str, str] = field(default_factory=dict)
    health: HealthEvidence | None = None
    error: str | None = None
    recovery: str | None = None
    updated_at: str | None = None


class Journal:
    def __init__(self, root: Path, report: Report):
        self.root = root
        self.pending = root / f"{report.unit}.pending.json"
        self.report = report

    def record(self, phase: str, *, create: bool = False) -> None:
        updated_at = datetime.now(UTC).isoformat()
        snapshot = replace(self.report, phase=phase, updated_at=updated_at)
        try:
            write_atomic(self.pending, self.content(snapshot), replace=not create)
        except OSError:
            raise JournalError(
                f"Could not persist deployment phase {phase}. Inspect {self.pending}."
            ) from None
        self.report.phase = phase
        self.report.updated_at = updated_at

    def content(self, snapshot: Report | None = None) -> str:
        return json.dumps(asdict(snapshot or self.report), indent=2) + "\n"

    def archive(self) -> None:
        try:
            write_atomic(self.root / f"{self.report.id}.json", self.content())
        except OSError:
            raise JournalError(
                f"Could not archive the deployment result. Inspect {self.pending}."
            ) from None

    def complete(self) -> None:
        self.record("finalizing")
        self.report.status = (
            "unchanged" if self.report.base_sha == self.report.target_sha else "succeeded"
        )
        self.report.phase = "complete"
        self.archive()
        try:
            self.pending.unlink()
            sync_directory(self.root)
        except OSError:
            raise JournalError(
                "Deployment operations completed and the report was archived, but journal cleanup could not be confirmed."
            ) from None


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
    state = Path(config.deployment.state_path).resolve()
    backup = Path(config.deployment.backup.path).resolve()
    if (
        state.is_relative_to(backup)
        or backup.is_relative_to(state)
        or checkout.is_relative_to(state)
        or checkout.is_relative_to(backup)
    ):
        raise GhostError(
            "The checkout, state and backup locations must resolve to separate directories."
        )


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
        with deployment_signals(), deployment_lock(self.directory, unit):
            bind_state_directory(unit, self.directory)
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
            if fetch and target != tip:
                raise GhostError(
                    "The requested SHA is no longer the fetched deployment branch tip. Review the latest commit."
                )
            if not repo.ancestor(target, tip):
                raise GhostError(
                    "The exact target must be reachable from the configured origin branch."
                )
            snapshots = self.host.snapshot_databases()
            installed = {
                database: frozenset(state.versions) for database, state in snapshots.items()
            }
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
                uuid4().hex,
                unit,
                datetime.now(UTC).isoformat(),
                base,
                target,
                upgrades=upgrades,
                database_oids={database: state.oid for database, state in snapshots.items()},
                expected_versions=plan.expected_versions,
            )
            journal = Journal(self.directory, report)
            journal.record("prepared", create=True)
            self.apply(journal, repo, plan, snapshots, backups)
            return report

    def apply(
        self,
        journal: Journal,
        repo: GitRepository,
        plan: Plan,
        snapshots: dict[str, DatabaseState],
        backups: Backups,
    ) -> None:
        report = journal.report
        expected = snapshots.copy()

        def check_databases():
            if self.host.snapshot_databases() != expected:
                raise GhostError(
                    "Database identity, module state or versions differ from the deployment plan. Reconcile before continuing."
                )

        def check_revision():
            if repo.clean_head() != report.target_sha:
                raise GhostError(
                    "The checkout diverged from the approved revision during deployment."
                )

        try:
            if plan.restart_required:
                self.host.require_same_config()
                journal.record("stopping_service")
                self.host.service("stop")
                self.host.require_inactive()
                journal.record("service_stopped")
                check_databases()
            if report.upgrades:
                report.backup_directory = str(backups.root / report.id)
                journal.record("creating_backups")
                backups.check_space(list(report.upgrades))
                for record in backups.create(
                    report.id, report.base_sha, report.target_sha, list(report.upgrades)
                ):
                    report.backups.append(record)
                    journal.record("creating_backups")
            if report.base_sha != report.target_sha:
                if plan.restart_required:
                    self.host.require_inactive()
                    check_databases()
                journal.record("synchronizing_code")
                repo.sync(report.base_sha, report.target_sha)
                journal.record("code_synchronized")
            for database, modules in report.upgrades.items():
                self.host.require_inactive()
                check_databases()
                check_revision()
                report.in_progress_database = database
                journal.record("upgrading_database")
                self.host.upgrade(database, modules)
                report.upgraded_databases.append(database)
                journal.record("verifying_database")
                state = expected[database]
                expected[database] = replace(
                    state,
                    versions={
                        **state.versions,
                        **{module: report.expected_versions[module] for module in modules},
                    },
                )
                check_databases()
                check_revision()
                report.verified_databases.append(database)
                report.in_progress_database = None
                journal.record("database_upgraded")
            if plan.restart_required:
                self.host.require_inactive()
                check_databases()
                check_revision()
                journal.record("starting_service")
                self.host.service("start")
                journal.record("checking_health")
                report.health = self.host.health()
                check_databases()
                check_revision()
        except (Exception, KeyboardInterrupt) as exc:
            self.failed(journal, exc, plan.restart_required)
            return

        # Archival failures leave the pending journal and propagate as a CLI failure.
        journal.complete()

    def failed(self, journal: Journal, exc: BaseException, restart: bool) -> None:
        report = journal.report
        report.status = "failed"
        report.error = (
            str(exc)
            if isinstance(exc, GhostError)
            else (
                "Interrupted by operator."
                if isinstance(exc, KeyboardInterrupt)
                else "A filesystem operation failed."
                if isinstance(exc, OSError)
                else "An unexpected deployment error occurred."
            )
        )
        if restart and report.phase in {"starting_service", "checking_health"}:
            try:
                report.recovery = "A stop of the newly started service is being attempted. Verify the systemd job and actual service status."
                with suppress(JournalError):
                    journal.record(report.phase)
                self.host.service("stop")
                self.host.require_inactive()
            except (Exception, KeyboardInterrupt):
                report.recovery = "Could not confirm Odoo stopped. Isolate traffic and inspect the unit immediately."
            else:
                report.recovery = "Odoo is stopped after a start or health failure. Verify the code and database states."
        else:
            report.recovery = "Inspect the service, Git checkout and all reported database states before returning Odoo to service."
        report.recovery += " If a migration ran, verify shared code, every affected database and the matching filestores together. Archive the pending journal only after reconciliation."
        try:
            journal.record(report.phase)
            journal.archive()
        except (Exception, KeyboardInterrupt):
            report.recovery += f" Failure reporting was incomplete. Inspect the last durable phase in {journal.pending}."
