import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

from ghostdeploy.core import GhostError, safe_error
from ghostdeploy.git import GitRepository
from ghostdeploy.runtime import SUDO, SYSTEMCTL, Host

Status = Literal["PASS", "WARN", "FAIL", "SKIP"]


@dataclass(frozen=True)
class Check:
    check: str
    status: Status
    detail: str
    remediation: str = ""

    @property
    def ok(self) -> bool:
        return self.status != "FAIL"


REMEDIATION = {
    "Operating system": "Run host diagnostics on the Ubuntu Odoo server.",
    "Python": "Install Python 3.11 or newer and recreate the GhostDeploy virtual environment.",
    "git": "Install Git and make it available on the service account's PATH.",
    "psql": "Install the PostgreSQL client tools and check PATH.",
    "pg_dump": "Install the PostgreSQL client tools for the server version and check PATH.",
    "pg_restore": "Install pg_restore with the PostgreSQL client tools and check PATH.",
    SYSTEMCTL: "Use an Ubuntu host running systemd.",
    SUDO: "Install sudo and use the narrow unit permissions from the installation guide.",
    "Odoo executable": "Set odoo.executable to the Odoo server entry point.",
    "Python interpreter": "Set odoo.interpreter to the interpreter used by the service.",
    "Odoo configuration": "Set odoo.config_file to the service config and grant the Odoo account read access.",
    "Git checkout": "Use a dedicated writable addons checkout owned by the service account.",
    "Addon root": "Use existing custom addons directories inside the configured checkout.",
    "Service": "Use the canonical systemd unit name and the static Odoo service user.",
    "State directory": "Use a real mode 0700 directory owned by the Odoo account. Preserve existing journals.",
    "Backup directory": "Provision a separate mode 0700 directory owned by the Odoo account.",
}


def check_action(label: str, action: Callable[[], object], success: str, remedy: str) -> Check:
    try:
        action()
    except (GhostError, OSError) as exc:
        return Check(label, "FAIL", safe_error(exc), remedy)
    return Check(label, "PASS", success)


def diagnose(host: Host) -> list[Check]:
    config = host.config
    initial = host.inspect()
    checks = [
        Check(
            item.check,
            "PASS" if item.ok else "FAIL",
            item.detail,
            "" if item.ok else REMEDIATION.get(item.check, "Review the installation guide."),
        )
        for item in initial
    ]
    passed = {item.check for item in initial if item.ok}
    if "Operating system" in passed:
        identity = check_action(
            "Service account",
            host.require_identity,
            f"Running as {config.odoo.service_user}.",
            "Run doctor from the configured unprivileged Odoo account with its normal HOME.",
        )
    else:
        identity = Check(
            "Service account",
            "SKIP",
            "Ubuntu service identity cannot be checked here.",
            "Run doctor as the Odoo account on the target host.",
        )
    checks.append(identity)

    if identity.status == "PASS" and "Service" in passed and SUDO in passed:
        checks.append(
            check_action(
                "Systemd stop/start permissions",
                host.require_service_permissions,
                "sudo lists permission to stop and start the named unit.",
                "Grant NOPASSWD for only the configured systemctl stop and start commands.",
            )
        )
    else:
        checks.append(
            Check(
                "Systemd stop/start permissions",
                "SKIP",
                "Service, account or sudo is unavailable.",
                "Resolve the systemd and service account checks first.",
            )
        )

    git_checks(host, passed, checks)
    database_checks(host, passed, identity.status == "PASS", checks)
    storage_checks(host, passed, checks)
    ssh_checks(passed, checks)
    return checks


def git_checks(host: Host, passed: set[str], checks: list[Check]) -> None:
    config = host.config.repository
    remedy = "Use a full local Git clone and set repository.checkout_path to its worktree root."
    if not {"git", "Git checkout"} <= passed:
        checks.append(
            Check("Git repository", "SKIP", "Git or the checkout path is unavailable.", remedy)
        )
        return
    try:
        repo = GitRepository(Path(config.checkout_path), config.branch, host.runner)
    except (GhostError, OSError) as exc:
        checks.append(Check("Git repository", "FAIL", safe_error(exc), remedy))
        return
    checks.append(Check("Git repository", "PASS", "Found a full Git worktree."))
    checks.append(
        check_action(
            "Git branch and cleanliness",
            repo.clean_head,
            f"Clean checkout on {config.branch}.",
            "Check out the configured branch and review local or ignored changes before removing anything.",
        )
    )
    target_check = check_action(
        "Git tracking ref",
        lambda: repo.resolve(repo.remote_ref),
        f"The cached origin/{config.branch} ref resolves locally.",
        "Fetch the reviewed branch separately before planning or use an explicitly approved fetch during deployment.",
    )
    if not target_check.ok:
        target_check = Check(
            target_check.check,
            "WARN",
            "Could not resolve the cached deployment branch.",
            target_check.remediation,
        )
    checks.append(target_check)


def database_checks(
    host: Host, passed: set[str], identity_verified: bool, checks: list[Check]
) -> None:
    def runtime() -> None:
        host.read_odoo_config()
        host.service_process(host.systemd())

    if identity_verified and {"Odoo configuration", "Service"} <= passed:
        settings = check_action(
            "Odoo connection settings",
            runtime,
            "The active process matches the Odoo configuration.",
            "Check the service command, PostgreSQL endpoint and Odoo configuration on the target host.",
        )
    else:
        settings = Check(
            "Odoo connection settings",
            "SKIP",
            "The service account, config or unit is unavailable.",
            "Fix those checks before testing database connections.",
        )
    checks.append(settings)
    for database in host.config.deployment.databases:
        label = f"Database {database}"
        remedy = "Check the exact allowlisted name, the Odoo role and PostgreSQL authentication. Never add credentials to GhostDeploy YAML."
        if "psql" not in passed or settings.status != "PASS":
            checks.append(
                Check(
                    label,
                    "SKIP",
                    "Verified Odoo connection settings and psql are required.",
                    remedy,
                )
            )
            continue

        connection = check_action(
            label,
            partial(probe_database, host, database),
            "Read-only connection confirmed.",
            remedy,
        )
        checks.append(connection)
        if connection.ok:
            checks.append(
                check_action(
                    f"Upgrade role for {database}",
                    lambda target=database: host.require_database_role(target),
                    "Role has neither superuser nor database creation privileges.",
                    "Use an Odoo role with NOSUPERUSER and NOCREATEDB before upgrading.",
                )
            )


def probe_database(host: Host, database: str) -> None:
    host.require_same_config()
    response = host.runner.run(
        [
            "psql",
            "--no-psqlrc",
            "--no-password",
            "--no-align",
            "--tuples-only",
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            "SELECT pg_catalog.current_database()",
        ],
        label=f"Connect to allowlisted database {database}",
        env=host.pg_env(database),
        timeout=15,
    )
    if response.strip() != database.encode("ascii"):
        raise GhostError("PostgreSQL did not confirm the configured database name.")


def storage_checks(host: Host, passed: set[str], checks: list[Check]) -> None:
    backup = host.config.deployment.backup
    label = "Available backup storage"
    if not backup.enabled:
        checks.append(
            Check(
                label,
                "WARN",
                "Backups are disabled. Database upgrades remain blocked.",
                "Enable backups and provision separate private storage before upgrading.",
            )
        )
    elif "Backup directory" not in passed:
        checks.append(
            Check(
                label,
                "SKIP",
                "The backup directory did not pass its permissions check.",
                "Fix the backup directory check first.",
            )
        )
    else:
        try:
            free_mib = shutil.disk_usage(backup.path).free // 1024**2
        except OSError as exc:
            checks.append(
                Check(
                    label,
                    "FAIL",
                    safe_error(exc),
                    "Verify the backup volume is mounted and accessible.",
                )
            )
            return
        enough = free_mib >= backup.min_free_mib
        checks.append(
            Check(
                label,
                "PASS" if enough else "FAIL",
                f"{free_mib} MiB free. Reserve is {backup.min_free_mib} MiB before archive estimates.",
                ""
                if enough
                else "Free or provision space on the backup volume. Preserve existing backups.",
            )
        )


def ssh_checks(passed: set[str], checks: list[Check]) -> None:
    if "Operating system" not in passed:
        checks.append(
            Check(
                "Restricted SSH prerequisites",
                "SKIP",
                "Run this check on the Ubuntu host.",
                "Use ghostdeploy workflow setup to see the operator steps.",
            )
        )
        return
    sshd = shutil.which("sshd") or shutil.which("/usr/sbin/sshd")
    entrypoint = shutil.which("ghostdeploy-ssh") or shutil.which(
        str(Path(sys.executable).parent / "ghostdeploy-ssh")
    )
    checks.append(
        Check(
            "Restricted SSH prerequisites",
            "PASS" if sshd and entrypoint else "WARN",
            "sshd and ghostdeploy-ssh are available."
            if sshd and entrypoint
            else "sshd or ghostdeploy-ssh was not found.",
            ""
            if sshd and entrypoint
            else "Install OpenSSH Server and GhostDeploy on the host for Actions use.",
        )
    )
    checks.append(
        Check(
            "SSH restrictions and host key pin",
            "SKIP",
            "No inbound SSH or GitHub verification was attempted.",
            "Verify the restricted key, pinned host key and environment protection using workflow setup.",
        )
    )
