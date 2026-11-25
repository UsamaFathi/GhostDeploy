"""Authorize, inspect, or verify a dedicated disposable GhostDeploy staging host."""

import argparse
import configparser
import hashlib
import json
import os
import platform
import re
import stat
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ghostdeploy.backup import Backups
from ghostdeploy.config import MODULE, SHA, load
from ghostdeploy.core import CommandError, GhostError, Runner, safe_error
from ghostdeploy.git import GitRepository
from ghostdeploy.runtime import SUDO, SYSTEMCTL, Host, private_directory

DATABASES = ["ghostdeploy_alpha_a", "ghostdeploy_alpha_b", "ghostdeploy_alpha_c"]
DECLARATION = "dedicated disposable staging with synthetic data"
MACHINE_ID = Path("/etc/machine-id")


def trusted_file(path: Path) -> Path:
    if not path.is_absolute() or path.resolve() != path:
        raise GhostError(
            "Use canonical absolute paths for staging authorization and configuration."
        )
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
        raise GhostError("Staging authorization, configuration and SSH policy must be root-owned.")
    for parent in path.parents:
        info = parent.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise GhostError(
                "Staging authorization files must have administrator-controlled parents."
            )
    return path


def authorized_facts(config_path: Path) -> tuple[Host, dict]:
    if sys.platform != "linux" or platform.freedesktop_os_release().get("ID") != "ubuntu":
        raise GhostError("Run the staging kit on a dedicated Ubuntu host.")
    machine_id = MACHINE_ID.read_bytes().strip()
    if not re.fullmatch(rb"[a-f0-9]{32}", machine_id) or machine_id == b"0" * 32:
        raise GhostError("Initialize the disposable VM with its own machine ID.")
    config = load(trusted_file(config_path))
    deploy = config.deployment
    if (
        deploy.environment != "staging"
        or deploy.databases != DATABASES
        or deploy.upgrade_policy != "manual_review"
        or not deploy.backup.enabled
        or not deploy.health_check.enabled
        or not deploy.health_check.url
    ):
        raise GhostError(
            "Use staging, the three kit database names, manual review, backups and HTTP health."
        )
    host = Host(config, Runner())
    odoo_file = trusted_file(Path(config.odoo.config_file))
    settings = configparser.ConfigParser(interpolation=None)
    settings.read_string(odoo_file.read_text(encoding="utf-8"))
    options = settings["options"]
    if (
        options.get("http_interface", "").strip() not in {"127.0.0.1", "::1"}
        or options.getboolean("list_db", fallback=True)
        or {name.strip() for name in options.get("db_name", "").split(",")} != set(DATABASES)
    ):
        raise GhostError(
            "Bind Odoo to loopback, disable database listing and set its exact synthetic database names."
        )
    host.read_odoo_config()
    if host.pg_settings.get("PGHOST") not in {"127.0.0.1", "::1"}:
        raise GhostError("The kit requires a dedicated PostgreSQL cluster on TCP loopback.")
    return host, {
        "declaration": DECLARATION,
        "machine_sha256": hashlib.sha256(machine_id).hexdigest(),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "odoo_config_sha256": host.config_digest.hex(),
    }


def check_authorization(record: dict, facts: dict, now: datetime) -> None:
    if (
        not isinstance(record, dict)
        or record.keys() != facts.keys() | {"expires_at"}
        or any(record[key] != value for key, value in facts.items())
    ):
        raise GhostError("The staging authorization does not match this host and configuration.")
    try:
        expires = datetime.fromisoformat(record["expires_at"])
    except (TypeError, ValueError):
        raise GhostError("Staging authorization requires an explicit expiry timestamp.") from None
    if expires.utcoffset() is None or not now < expires <= now + timedelta(hours=24):
        raise GhostError("Staging authorization is expired or exceeds its 24-hour window.")


def gateway_argv(config_path: Path, authorization: Path) -> list[str]:
    paths = [sys.executable, str(Path(__file__).resolve()), str(config_path), str(authorization)]
    if any(not re.fullmatch(r"/[A-Za-z0-9_./-]+", path) for path in paths):
        raise GhostError(
            "The installed gateway paths must be absolute and contain no shell metacharacters."
        )
    return [
        paths[0],
        "-I",
        paths[1],
        "--config",
        paths[2],
        "--authorization",
        paths[3],
        "ssh-gate",
    ]


def preflight(host: Host, config_path: Path, authorization: Path) -> dict:
    host.preflight()
    for database in DATABASES:
        host.require_database_role(database)
    host.snapshot_databases()
    Backups(host).check_space(DATABASES)
    key_file = trusted_file(Path("/etc/ssh/authorized_keys") / host.odoo.service_user)
    active = [
        line.strip()
        for line in key_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    prefix = 'restrict,command="' + " ".join(gateway_argv(config_path, authorization)) + '" '
    if len(active) != 1 or not re.fullmatch(
        re.escape(prefix) + r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}(?: [^\r\n]*)?", active[0]
    ):
        raise GhostError(
            "The admin-controlled key file must contain exactly one restricted staging key."
        )

    for command in (
        (SYSTEMCTL, "restart", host.odoo.unit),
        (SYSTEMCTL, "start", "ghostdeploy-alpha-unrelated.service"),
        ("/usr/bin/true",),
    ):
        try:
            host.runner.run(
                [SUDO, "-n", "-l", "--", *command], label="Check sudo scope", timeout=10
            )
        except CommandError as exc:
            if exc.returncode != 1:
                raise
        else:
            raise GhostError("The staging service account has broader sudo access than expected.")
    return {"status": "PASS", "check": "local staging preflight", "external_validation": "PENDING"}


@contextmanager
def private_read(path: Path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise GhostError("Expected an owned private regular evidence file without links.")
        yield stream


def read_report(path: Path) -> dict:
    with private_read(path) as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576:
        raise GhostError("The deployment report exceeds the evidence reader's size limit.")
    return json.loads(raw)


def check_backup(record: dict, directory: Path, report: dict, database: str) -> None:
    for key, value in {
        "deployment_id": report["id"],
        "base_sha": report["base_sha"],
        "target_sha": report["target_sha"],
        "database": database,
        "status": "verified",
    }.items():
        if record.get(key) != value:
            raise GhostError("Backup metadata does not match the deployment.")
    if record["filestore"].get("status") != "verified":
        raise GhostError(
            "Seed a stored attachment in both upgraded databases before the staging test."
        )
    for artifact, filename in (
        (record["dump"], f"{database}.dump"),
        (record["filestore"], f"{database}.filestore.tar.gz"),
    ):
        path = directory / filename
        if artifact["path"] != str(path):
            raise GhostError("The artifact path does not match the owned deployment directory.")
        with private_read(path) as stream:
            length = os.fstat(stream.fileno()).st_size
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if not length or length != artifact["bytes"] or digest != artifact["sha256"]:
            raise GhostError(
                "A staging backup artifact does not match its recorded size and SHA-256."
            )


def verify(host: Host, identifier: str, target: str, expected: str, module: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", identifier) or not SHA.fullmatch(target):
        raise GhostError("Use the full deployment ID and the dispatch's exact lowercase Git SHA.")
    if not MODULE.fullmatch(module) or module == "all":
        raise GhostError("Supply the explicit staging addon name.")
    host.require_identity()
    config = host.config
    state = Path(config.deployment.state_path)
    private_directory(state)
    report = read_report(state / f"{identifier}.json")
    if (
        report["id"] != identifier
        or report["unit"] != host.odoo.unit
        or report["environment"] != "staging"
        or report["target_sha"] != target
        or not SHA.fullmatch(report["base_sha"])
        or report["base_sha"] == target
        or set(report["database_oids"]) != set(DATABASES)
        or report["upgrades"] != {database: [module] for database in DATABASES[:2]}
        or report["status"] != expected
    ):
        raise GhostError("The deployment result does not match the reviewed staging scenario.")
    pending = state / f"{host.odoo.unit}.pending.json"
    completed = DATABASES[:2] if expected == "succeeded" else DATABASES[:1]
    if report["upgraded_databases"] != completed or report["verified_databases"] != completed:
        raise GhostError("The report does not match the expected verified database set.")
    if expected == "succeeded":
        if (
            report["phase"] != "complete"
            or report["in_progress_database"] is not None
            or os.path.lexists(pending)
        ):
            raise GhostError(
                "The successful deployment has incomplete or conflicting journal state."
            )
        host.health()
    else:
        if (
            report["phase"] != "upgrading_database"
            or report["in_progress_database"] != DATABASES[1]
            or read_report(pending) != report
        ):
            raise GhostError(
                "The partial failure must preserve database B's uncertain pending journal."
            )
        host.require_inactive()
    repo = GitRepository(
        Path(config.repository.checkout_path), config.repository.branch, host.runner
    )
    if repo.clean_head() != target:
        raise GhostError("The staging checkout does not match the reviewed target.")
    directory = Path(config.deployment.backup.path) / identifier
    private_directory(directory)
    for record, database in zip(report["backups"], DATABASES[:2], strict=True):
        check_backup(record, directory, report, database)
    return {
        "status": "PASS",
        "check": "local deployment evidence",
        "deployment_status": expected,
        "target_sha": target,
        "verified_databases": len(completed),
        "uncertain_databases": int(expected == "failed"),
        "verified_backup_sets": 2,
        "external_validation": "PENDING",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    authorize = commands.add_parser(
        "authorize", help="Print an authorization for the administrator to install."
    )
    authorize.add_argument("--confirm-disposable", action="store_true")
    commands.add_parser("preflight", help="Read local runtime, database and SSH policy evidence.")
    commands.add_parser(
        "ssh-gate", help="Validate authorization and enter the existing restricted deploy gateway."
    )
    verification = commands.add_parser("verify", help="Read and verify deployment evidence.")
    verification.add_argument("--deployment-id", required=True)
    verification.add_argument("--target-sha", required=True)
    verification.add_argument("--module", required=True)
    verification.add_argument("--expect", choices=["succeeded", "failed"], required=True)
    args = parser.parse_args()
    try:
        host, facts = authorized_facts(args.config)
        now = datetime.now(UTC)
        if args.command == "authorize":
            if os.geteuid() != 0 or not args.confirm_disposable:
                raise GhostError("A root operator must attest to the dedicated disposable host.")
            print(json.dumps({**facts, "expires_at": (now + timedelta(hours=2)).isoformat()}))
            return 0
        check_authorization(json.loads(trusted_file(args.authorization).read_bytes()), facts, now)
        host.require_identity()
        if args.command == "ssh-gate":
            trusted_file(Path(__file__).resolve())
            gateway = trusted_file(Path(sys.executable).parent / "ghostdeploy-ssh")
            os.execv(
                sys.executable, [sys.executable, "-I", str(gateway), "--config", str(args.config)]
            )
        if args.command == "preflight":
            result = preflight(host, args.config, args.authorization)
        else:
            result = verify(host, args.deployment_id, args.target_sha, args.expect, args.module)
    except (GhostError, OSError, ValueError, KeyError, TypeError, configparser.Error) as error:
        print(json.dumps({"status": "FAIL", "error": safe_error(error)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
