import configparser
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from ghostdeploy.config import MODULE, Config
from ghostdeploy.core import GhostError, Runner, env_without

SYSTEMCTL = "/usr/bin/systemctl"
SUDO = "/usr/bin/sudo"
UNSET = {"", "false", "none"}


def checked_path(path: Path, *, directory: bool, access: int) -> None:
    try:
        info = path.stat()
    except OSError as exc:
        issue = (
            "permission denied"
            if exc.errno in {errno.EACCES, errno.EPERM}
            else "missing or inaccessible"
        )
        raise GhostError(f"{path}: {issue}.") from None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise GhostError(f"{path}: expected a {'directory' if directory else 'regular file'}.")
    if not os.access(path, access):
        raise GhostError(f"{path}: insufficient permissions for the service account.")


def private_directory(path: Path) -> None:
    checked_path(path, directory=True, access=os.R_OK | os.W_OK | os.X_OK)
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise GhostError(
            f"{path}: use a real directory owned by the service account with mode 0700."
        )
    if path.resolve() != path.absolute():
        raise GhostError(f"{path}: private storage must use a canonical path without symlinks.")
    for parent in path.absolute().parents:
        metadata = parent.stat()
        if metadata.st_uid not in {0, os.geteuid()} or (
            metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX
        ):
            raise GhostError(
                f"{path}: a parent directory can be replaced by an untrusted account."
            )


@dataclass(frozen=True)
class Finding:
    check: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DatabaseState:
    oid: int
    server: tuple[str, ...]
    versions: dict[str, str | None]


@dataclass(frozen=True)
class HealthEvidence:
    systemd_active: bool
    process_pid: int
    http_status: int | None
    application_verified: bool = False


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Host:
    def __init__(self, config: Config, runner: Runner):
        self.config = config
        self.runner = runner
        self.odoo = config.odoo
        self.timeout = config.deployment.command_timeout_seconds
        self.pg_settings: dict[str, str] = {}
        self.filestore: Path | None = None
        self.external_addons: set[Path] = set()
        self.skip_auto_install = False
        self.config_digest: bytes | None = None

    def systemd(self) -> dict[str, str]:
        properties = "Id,LoadState,ActiveState,MainPID,User,DynamicUser,KillMode,SendSIGKILL"
        raw = self.runner.run(
            [SYSTEMCTL, "show", f"--property={properties}", "--", self.odoo.unit],
            label="Inspect systemd service",
            timeout=10,
        )
        service = dict(line.split("=", 1) for line in raw.decode().splitlines() if "=" in line)
        if service.get("LoadState") != "loaded":
            raise GhostError(f"Systemd unit {self.odoo.unit} is missing, masked or unloaded.")
        if service.get("Id") != self.odoo.unit:
            raise GhostError("Use the canonical systemd unit name rather than an alias.")
        if service.get("User") != self.odoo.service_user or service.get("DynamicUser") != "no":
            raise GhostError(
                f"Set User={self.odoo.service_user} on {self.odoo.unit} and disable DynamicUser."
            )
        if service.get("KillMode") != "control-group" or service.get("SendSIGKILL") != "yes":
            raise GhostError(
                "The Odoo service requires KillMode=control-group and SendSIGKILL=yes."
            )
        return service

    def inspect(self) -> list[Finding]:
        findings = []
        release = platform.freedesktop_os_release() if platform.system() == "Linux" else {}
        ubuntu = release.get("ID") == "ubuntu"
        findings.append(
            Finding(
                "Operating system",
                ubuntu,
                "Ubuntu Linux is required for execution."
                if not ubuntu
                else release.get("PRETTY_NAME", "Ubuntu"),
            )
        )
        findings.append(Finding("Python", sys.version_info >= (3, 11), platform.python_version()))
        for executable in ("git", "psql", "pg_dump", "pg_restore", SYSTEMCTL, SUDO):
            installed = shutil.which(executable)
            findings.append(
                Finding(
                    executable,
                    bool(installed),
                    installed or "Install the matching system package and check PATH.",
                )
            )
        checks = [
            (
                "Odoo executable",
                self.odoo.executable,
                False,
                os.R_OK | (0 if self.odoo.interpreter else os.X_OK),
            ),
            ("Odoo configuration", self.odoo.config_file, False, os.R_OK),
            (
                "Git checkout",
                self.config.repository.checkout_path,
                True,
                os.R_OK | os.W_OK | os.X_OK,
            ),
            *(("Addon root", root, True, os.R_OK | os.X_OK) for root in self.odoo.addons_path),
        ]
        if self.odoo.interpreter:
            checks.append(("Python interpreter", self.odoo.interpreter, False, os.R_OK | os.X_OK))
        for label, location, directory, access in checks:
            try:
                checked_path(Path(location), directory=directory, access=access)
                findings.append(Finding(label, True, location))
            except GhostError as exc:
                findings.append(Finding(label, False, str(exc)))
        for label, path in (
            ("State directory", self.config.deployment.state_path),
            ("Backup directory", self.config.deployment.backup.path),
        ):
            if label == "Backup directory" and not self.config.deployment.backup.enabled:
                continue
            try:
                if not ubuntu:
                    raise GhostError(
                        "Private directory permissions are checked on the Ubuntu deployment host."
                    )
                private_directory(Path(path))
                findings.append(Finding(label, True, path))
            except GhostError as exc:
                findings.append(Finding(label, False, str(exc)))
        try:
            self.systemd()
            findings.append(Finding("Service", True, self.odoo.unit))
        except GhostError as exc:
            findings.append(Finding("Service", False, f"{exc} Check the configured systemd unit."))
        return findings

    def require_identity(self) -> None:
        if platform.system() != "Linux":
            raise GhostError("Odoo database inspection and deployment require an Ubuntu host.")
        import pwd

        if os.geteuid() == 0 or pwd.getpwuid(os.geteuid()).pw_name != self.odoo.service_user:
            raise GhostError(
                f"Run GhostDeploy as the unprivileged Odoo service user ({self.odoo.service_user})."
            )

    def read_odoo_config(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            contents = Path(self.odoo.config_file).read_bytes()
            parser.read_string(contents.decode("utf-8"))
            options = parser["options"]
        except (OSError, ValueError, KeyError, configparser.Error):
            raise GhostError("Cannot read the Odoo configuration [options] section.") from None

        def setting(key: str) -> str:
            value = options.get(key, "").strip()
            return "" if value.lower() in UNSET else value

        if any(
            setting(key)
            for key in (
                "init",
                "update",
                "reinit",
                "save",
                "test_enable",
                "test_tags",
                "db_template_overrides",
                "db_replica_host",
                "db_replica_port",
            )
        ):
            raise GhostError(
                "Remove persistent installation, upgrade, test or replica options from the Odoo configuration."
            )
        override = setting("db_name")
        if override and not set(self.config.deployment.databases) <= {
            name.strip() for name in override.split(",")
        }:
            raise GhostError(
                "Every allowlisted database must be included in Odoo's configured db_name when set."
            )
        configured = {
            Path(root.strip()) for root in setting("addons_path").split(",") if root.strip()
        }
        if any(not root.is_absolute() for root in configured):
            raise GhostError("Use absolute Odoo addons_path entries.")
        roots = {root.resolve() for root in configured}
        managed = {Path(root).resolve() for root in self.odoo.addons_path}
        if not managed <= roots:
            raise GhostError(
                "Include every GhostDeploy addon root in the Odoo configuration addons_path."
            )
        checkout = Path(self.config.repository.checkout_path).resolve()
        if any(root.is_relative_to(checkout) for root in roots - managed):
            raise GhostError(
                "Every Odoo addon root inside the checkout must be managed by GhostDeploy."
            )
        self.pg_settings = {
            variable: value
            for option, variable in {
                "db_host": "PGHOST",
                "db_port": "PGPORT",
                "db_user": "PGUSER",
                "db_password": "PGPASSWORD",
                "db_sslmode": "PGSSLMODE",
            }.items()
            if (value := setting(option))
        }
        host = self.pg_settings.get("PGHOST", "")
        port = self.pg_settings.get("PGPORT", "")
        if (
            not host
            or "," in host
            or not (host.startswith("/") or re.fullmatch(r"[A-Za-z0-9.:-]+", host))
            or not re.fullmatch(r"[0-9]{1,5}", port)
            or not 1 <= int(port) <= 65535
        ):
            raise GhostError("Set one explicit db_host and db_port in the Odoo configuration.")
        data_dir = Path(setting("data_dir"))
        if not data_dir.is_absolute():
            raise GhostError(
                "Set an absolute data_dir in Odoo's configuration to identify its filestores."
            )
        self.filestore = data_dir / "filestore"
        server = Path(self.odoo.executable).resolve().parent
        defaults = {
            server / "addons",
            server / "odoo" / "addons",
            data_dir / "addons" / self.odoo.version,
        }
        self.external_addons = (roots | {root.resolve() for root in defaults}) - managed
        self.config_digest = hashlib.sha256(contents).digest()

    def require_same_config(self) -> None:
        if (
            self.config_digest is not None
            and hashlib.sha256(Path(self.odoo.config_file).read_bytes()).digest()
            != self.config_digest
        ):
            raise GhostError(
                "Odoo configuration changed during deployment. Reconcile it before continuing."
            )

    def prepare_upgrades(self, modules: set[str]) -> None:
        for module in modules:
            if any(
                (root / module / "__manifest__.py").exists()
                or (root / module / "__openerp__.py").exists()
                or (root / f"{module}.zip").exists()
                for root in self.external_addons
            ):
                raise GhostError(f"Addon {module} also exists in an unmanaged Odoo addon root.")
        help_text = self.runner.run(
            [*self.odoo_command(), "--help"],
            label="Inspect Odoo upgrade options",
            cwd=Path(self.odoo.executable).parent,
            env=self.odoo_env(),
        )
        self.skip_auto_install = b"--skip-auto-install" in help_text

    def pg_env(self, database: str) -> dict[str, str]:
        if database not in self.config.deployment.databases:
            raise GhostError("Database is outside the configured allowlist.")
        env = env_without("PG")
        env.update(self.pg_settings)
        env.update(
            PGDATABASE=database,
            PGCONNECT_TIMEOUT="10",
            PGCLIENTENCODING="UTF8",
            PGOPTIONS="-c default_transaction_read_only=on",
        )
        return env

    def require_database_role(self, database: str) -> None:
        answer = self.runner.run(
            [
                "psql",
                "--no-psqlrc",
                "--no-password",
                "--no-align",
                "--tuples-only",
                "--set",
                "ON_ERROR_STOP=1",
                "--command",
                "SELECT NOT (rolsuper OR rolcreatedb) FROM pg_catalog.pg_roles WHERE rolname = current_user",
            ],
            label=f"Check PostgreSQL role in {database}",
            env=self.pg_env(database),
        )
        if answer.strip() != b"t":
            raise GhostError("Use a PostgreSQL role with NOSUPERUSER and NOCREATEDB for upgrades.")

    def inspect_databases(self) -> dict[str, frozenset[str]]:
        return {
            database: frozenset(state.versions)
            for database, state in self.snapshot_databases().items()
        }

    def snapshot_databases(self) -> dict[str, DatabaseState]:
        self.require_same_config()
        states = {}
        for database in self.config.deployment.databases:
            answer = self.runner.run(
                [
                    "psql",
                    "--no-psqlrc",
                    "--no-password",
                    "--no-align",
                    "--tuples-only",
                    "--set",
                    "ON_ERROR_STOP=1",
                    "--command",
                    "SELECT pg_catalog.json_build_array(d.oid::bigint, "
                    "pg_catalog.json_build_array(pg_catalog.pg_postmaster_start_time()::text, "
                    "COALESCE(pg_catalog.inet_server_addr()::text, ''), pg_catalog.current_setting('port')), "
                    "(SELECT COALESCE(pg_catalog.json_agg(pg_catalog.json_build_array(name, state, latest_version) "
                    "ORDER BY name), '[]'::json) FROM public.ir_module_module "
                    "WHERE state IN ('installed','to install','to upgrade','to remove'))) "
                    "FROM pg_catalog.pg_database d WHERE d.datname = pg_catalog.current_database()",
                ],
                label=f"Inspect installed modules in {database}",
                env=self.pg_env(database),
            )
            versions = {}
            try:
                oid, server, rows = json.loads(answer)
                if (
                    type(oid) is not int
                    or not isinstance(server, list)
                    or len(server) != 3
                    or not all(isinstance(value, str) for value in server)
                    or not isinstance(rows, list)
                ):
                    raise ValueError
                for module, state, version in rows:
                    if (
                        state != "installed"
                        or not MODULE.fullmatch(module)
                        or module in versions
                        or (version is not None and not isinstance(version, str))
                    ):
                        raise ValueError
                    versions[module] = version
            except (ValueError, TypeError):
                raise GhostError(
                    f"Database {database} has pending addon operations or invalid module records. Reconcile them before deployment."
                ) from None
            states[database] = DatabaseState(oid, tuple(server), versions)
        return states

    def service_process(self, state: dict[str, str]) -> None:
        pid = state.get("MainPID", "")
        if not pid.isdecimal() or int(pid) <= 1:
            raise GhostError("The systemd service has no stable Odoo main process.")
        try:
            proc = Path(f"/proc/{pid}")
            if proc.stat().st_uid != os.geteuid():
                raise GhostError("The Odoo main process is not owned by the service account.")
            argv = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            service_env = (proc / "environ").read_bytes().split(b"\0")
        except OSError:
            raise GhostError(
                "Cannot inspect the Odoo process. Check the configured identity and /proc access."
            ) from None
        expected = [
            os.fsencode(value) for value in [self.odoo.executable, "-c", self.odoo.config_file]
        ]
        if (
            argv[-3:] != expected
            or len(argv) not in {3, 4}
            or (self.odoo.interpreter and argv != [os.fsencode(self.odoo.interpreter), *expected])
        ):
            raise GhostError(
                "The running service must use the configured Odoo executable and '-c CONFIG' without extra CLI flags."
            )
        if state.get("ActiveState") != "active":
            raise GhostError("The configured systemd service is no longer active.")
        if any(
            item.startswith((b"PG", b"ODOO_", b"PYTHONHOME=", b"PYTHONPATH=")) and b"=" in item
            for item in service_env
        ):
            raise GhostError(
                "Use Odoo config for connection and runtime settings. Remove PostgreSQL, Odoo and Python import overrides from the service environment."
            )
        service_home = next((item[5:] for item in service_env if item.startswith(b"HOME=")), None)
        if service_home != os.fsencode(Path.home()):
            raise GhostError(
                "GhostDeploy and the Odoo service must share the service account HOME."
            )

    def preflight(self) -> None:
        self.require_identity()
        failed = [f"{item.check}: {item.detail}" for item in self.inspect() if not item.ok]
        if failed:
            raise GhostError("Host preflight failed. " + " ".join(failed))
        self.read_odoo_config()
        runtime = self.runner.run(
            [*self.odoo_command(), "--version"],
            label="Check Odoo version",
            cwd=Path(self.odoo.executable).parent,
            env=self.odoo_env(),
        )
        if not re.search(
            rb"\b" + re.escape(self.odoo.version.encode()) + rb"(?=$|\s|[+.-])", runtime
        ):
            raise GhostError("The Odoo executable does not report the configured version.")
        state = self.systemd()
        if state.get("ActiveState") != "active":
            raise GhostError(
                "The Odoo service must initially be active. Reconcile it before deployment."
            )
        self.service_process(state)
        self.health()
        self.require_service_permissions()

    def require_service_permissions(self) -> None:
        for action in ("stop", "start"):
            self.runner.run(
                [SUDO, "-n", "-l", "--", SYSTEMCTL, action, self.odoo.unit],
                label=f"Check sudo permission to {action} {self.odoo.unit}",
                timeout=10,
            )

    def odoo_command(self) -> list[str]:
        return (
            [self.odoo.interpreter, self.odoo.executable]
            if self.odoo.interpreter
            else [self.odoo.executable]
        )

    def odoo_env(self) -> dict[str, str]:
        env = {
            key: value
            for key, value in env_without("PG").items()
            if not key.upper().startswith(("ODOO_", "PYTHONHOME", "PYTHONPATH"))
        }
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def service(self, action: str) -> None:
        if action not in {"stop", "start"}:
            raise GhostError("Unsupported service operation.")
        self.runner.run(
            [SUDO, "-n", SYSTEMCTL, action, self.odoo.unit],
            label=f"{action.capitalize()} {self.odoo.unit}",
            timeout=180,
        )

    def require_inactive(self) -> None:
        state = self.systemd()
        if (state.get("ActiveState"), state.get("MainPID")) != ("inactive", "0"):
            raise GhostError("Odoo is not fully stopped. Fix the service before continuing.")

    def upgrade(self, database: str, modules: tuple[str, ...]) -> None:
        if (
            database not in self.config.deployment.databases
            or not modules
            or any(module == "all" or not MODULE.fullmatch(module) for module in modules)
        ):
            raise GhostError(
                "Upgrade requires an allowlisted database and explicit valid module names."
            )
        self.require_same_config()
        self.runner.run(
            [
                *self.odoo_command(),
                "-c",
                self.odoo.config_file,
                "-d",
                database,
                "-u",
                ",".join(modules),
                "--stop-after-init",
                "--no-http",
                "--workers=0",
                "--max-cron-threads=0",
                *(["--skip-auto-install"] if self.skip_auto_install else []),
            ],
            label=f"Upgrade modules in {database}",
            cwd=Path(self.odoo.executable).parent,
            env=self.odoo_env(),
            timeout=self.timeout,
            capture=False,
        )

    def health(self) -> HealthEvidence:
        health = self.config.deployment.health_check
        end = time.monotonic() + health.timeout_seconds
        opener = build_opener(ProxyHandler({}), NoRedirect())
        reason = "systemd did not report an active service with a main process"
        while True:
            state = self.systemd()
            pid = state.get("MainPID", "0")
            if state.get("ActiveState") == "active" and pid.isdecimal() and int(pid) > 1:
                self.service_process(state)
                if not health.enabled or not health.url:
                    return HealthEvidence(True, int(pid), None)
                reason = "the configured HTTP endpoint did not return 2xx for the active process"
                try:
                    with opener.open(
                        health.url, timeout=max(0.05, min(2, end - time.monotonic()))
                    ) as response:
                        if 200 <= response.status < 300:
                            current = self.systemd()
                            if (
                                current.get("MainPID") == pid
                                and current.get("ActiveState") == "active"
                            ):
                                self.service_process(current)
                                return HealthEvidence(True, int(pid), response.status)
                except (HTTPError, URLError, OSError):
                    pass
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise GhostError(
                    f"Odoo did not pass its health check before the timeout: {reason}."
                )
            time.sleep(min(1, remaining))
