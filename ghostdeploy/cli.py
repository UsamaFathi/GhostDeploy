import json
import os
from dataclasses import asdict
from functools import wraps
from importlib.metadata import version as package_version
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import typer
import yaml
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ghostdeploy.config import Config, load
from ghostdeploy.config import validate as validate_config
from ghostdeploy.core import GhostError, Runner, safe_error
from ghostdeploy.deploy import Executor, Report
from ghostdeploy.diagnostics import diagnose, probe_database
from ghostdeploy.discovery import InstallHints, inspect_service, is_ubuntu, list_services
from ghostdeploy.feedback import approvals, operations
from ghostdeploy.files import write_atomic
from ghostdeploy.git import GitRepository
from ghostdeploy.planning import Plan, build_plan
from ghostdeploy.runtime import Host
from ghostdeploy.workflow import SECRETS, generate, setup_instructions

app = typer.Typer(no_args_is_help=True, help="Manage deployments for self-hosted Odoo.")
workflow = typer.Typer(no_args_is_help=True, help="Generate GitHub Actions workflows.")
app.add_typer(workflow, name="workflow")
out = Console()
err = Console(stderr=True)
ConfigPath = Annotated[Path, typer.Option("--config", help="Path to the GhostDeploy YAML file.")]
Output = Annotated[
    Literal["text", "json"], typer.Option(help="Output format.", case_sensitive=False)
]
DEFAULT_CONFIG = Path("ghostdeploy.yml")


def command(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (typer.Exit, typer.Abort):
            raise
        except (Exception, KeyboardInterrupt) as exc:
            message = safe_error(exc)
            if kwargs.get("output") == "json":
                emit_json({"ok": False, "error": message})
            else:
                err.print(Text(message, style="red"))
            raise typer.Exit(1) from None

    return guarded


def emit_json(data) -> None:
    typer.echo(json.dumps(data, ensure_ascii=True, indent=2))


def save(path: Path, content: str, *, force: bool, interactive: bool) -> None:
    overwrite = force
    if path.exists() or path.is_symlink():
        if not force and not interactive:
            raise GhostError("Destination already exists. Pass --force to replace it.")
        if not force and not typer.confirm("Replace the existing file?", default=False):
            raise typer.Abort()
        overwrite = True
    write_atomic(path, content, replace=overwrite)


def config_destination(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise GhostError(
            "Choose a regular configuration file. Symlinks and special files are not supported."
        )
    if not path.parent.is_dir() or not os.access(path.parent, os.W_OK):
        raise GhostError("Choose a configuration destination with an existing writable parent.")


def setup_discovery() -> tuple[str | None, InstallHints]:
    if not is_ubuntu():
        out.print("Ubuntu was not detected. Enter the deployment host settings manually.")
        return None, InstallHints()
    out.print("Ubuntu detected. Looking for local Odoo systemd units.")
    runner = Runner()
    try:
        candidates = list_services(runner)
    except (GhostError, OSError):
        candidates = ()
    if not candidates:
        out.print("No named Odoo service was discovered. Enter its settings manually.")
        return None, InstallHints()
    table = Table("Choice", "Systemd unit", "State")
    table.add_row("0", "Enter settings manually", "")
    for number, candidate in enumerate(candidates, 1):
        table.add_row(str(number), f"{candidate.name}.service", candidate.state)
    out.print(table)
    choice = typer.prompt("Choose an Odoo service", type=int, default=0)
    if not 0 <= choice <= len(candidates):
        raise GhostError("Choose a service number from the table or 0 for manual setup.")
    if choice == 0:
        return None, InstallHints()
    candidate = candidates[choice - 1]
    try:
        hints = inspect_service(candidate, runner)
    except (GhostError, OSError):
        hints = InstallHints()
    if hints.executable is None:
        out.print(
            "Runtime paths were not available. Run as the service account for local suggestions, or enter paths manually."
        )
    if hints.version:
        out.print(
            f"Found Odoo {hints.version} in the installation metadata. Confirm the version below."
        )
    if hints.addon_roots:
        out.print(Text("Addon roots declared in Odoo: " + ", ".join(hints.addon_roots)))
    return candidate.name, hints


def suggested_checkout(hints: InstallHints, default: str) -> str:
    runtime_paths = [
        PurePosixPath(path)
        for path in (hints.executable, hints.interpreter, hints.config_file)
        if path
    ]
    checkouts = [
        checkout
        for checkout in hints.checkouts
        if not any(path.is_relative_to(checkout) for path in runtime_paths)
    ]
    if len(checkouts) == 1:
        return checkouts[0]
    if checkouts:
        out.print(Text("Candidate custom addon checkouts: " + ", ".join(checkouts)))
    return default


def preview_database_connections(settings: Config) -> None:
    host = Host(settings, Runner())
    try:
        host.require_identity()
        host.read_odoo_config()
        host.service_process(host.systemd())
        endpoint = host.pg_settings["PGHOST"]
        if not endpoint.startswith("/") and endpoint not in {"127.0.0.1", "::1"}:
            raise GhostError(
                "Wizard probes require a local PostgreSQL socket or loopback address."
            )
    except (GhostError, OSError) as exc:
        out.print(Text(f"Skipped database probes: {safe_error(exc)}", style="yellow"))
        return
    for database in settings.deployment.databases:
        try:
            probe_database(host, database)
        except (GhostError, OSError) as exc:
            out.print(
                Text(
                    f"{database}: {safe_error(exc)}. Check the name and authentication with doctor.",
                    style="yellow",
                )
            )
        else:
            out.print(Text(f"{database}: read-only connection confirmed."))


@app.command()
@command
def init(
    config: ConfigPath = DEFAULT_CONFIG,
    non_interactive: Annotated[
        bool, typer.Option(help="Write a starter configuration using the supplied options.")
    ] = False,
    force: Annotated[bool, typer.Option(help="Replace the destination if it exists.")] = False,
    project: str = "my-odoo-project",
    odoo_version: str = "19.0",
    service: str = "odoo",
    service_user: str = "odoo",
    executable: str = "/opt/odoo/odoo-bin",
    interpreter: str | None = None,
    odoo_config: str = "/etc/odoo.conf",
    checkout: str = "/opt/odoo/custom_addons",
    addons: Annotated[
        str | None, typer.Option(help="Comma-separated absolute addon roots.")
    ] = None,
    branch: str = "main",
    environment: str = "staging",
    databases: Annotated[
        str | None, typer.Option(help="Comma-separated database allowlist.")
    ] = None,
    backup_enabled: bool = True,
    backup_path: str = "/var/backups/ghostdeploy",
    backup_min_free_mib: int = 512,
    state_path: str = "/var/lib/ghostdeploy",
    upgrade_policy: str = "manual_review",
    health_url: str | None = None,
    no_discover: Annotated[
        bool, typer.Option(help="Enter settings manually in the interactive wizard.")
    ] = False,
    verify_databases: Annotated[
        bool,
        typer.Option(
            help="Probe only explicitly entered databases on local PostgreSQL in the wizard."
        ),
    ] = False,
):
    """Create and validate a GhostDeploy configuration."""
    if verify_databases and non_interactive:
        raise GhostError(
            "--verify-databases is an interactive wizard option. Use doctor for scripts."
        )
    config_destination(config)
    if not non_interactive:
        out.print("[bold]GhostDeploy setup[/bold]")
        detected, hints = setup_discovery() if not no_discover else (None, InstallHints())
        project = typer.prompt("Project name", default=project)
        odoo_version = typer.prompt(
            "Odoo version (18.0 or 19.0)", default=hints.version or odoo_version
        )
        service = typer.prompt(
            "Systemd service name without .service", default=detected or service
        )
        service_user = typer.prompt("Service user", default=hints.user or service_user)
        executable = typer.prompt("Odoo executable", default=hints.executable or executable)
        interpreter = (
            typer.prompt(
                "Python interpreter (blank if directly executable)",
                default=hints.interpreter or interpreter or "",
                show_default=bool(hints.interpreter or interpreter),
            )
            or None
        )
        odoo_config = typer.prompt(
            "Odoo configuration file", default=hints.config_file or odoo_config
        )
        checkout = typer.prompt("Git checkout", default=suggested_checkout(hints, checkout))
        detected_roots = ",".join(
            root for root in hints.addon_roots if PurePosixPath(root).is_relative_to(checkout)
        )
        addons = typer.prompt(
            "Custom addon roots, separated by commas", default=addons or detected_roots or checkout
        )
        branch = typer.prompt("Deployment branch", default=branch)
        environment = typer.prompt("Environment", default=environment)
        if environment not in {"development", "staging", "production"}:
            raise GhostError("Choose development, staging or production.")
        out.print(
            Text(
                f"Environment: {environment}. A restart affects every database served by this unit."
            )
        )
        if hints.declared_databases:
            out.print(
                Text(
                    "Odoo config declares these database names (connectivity unverified): "
                    + ", ".join(hints.declared_databases)
                )
            )
        databases = typer.prompt(
            "Explicit database allowlist, separated by commas", default=databases
        )
        out.print(
            "GhostDeploy requires verified PostgreSQL and filestore backups before upgrading modules."
        )
        if environment == "production":
            out.print("Production requires backups, manual review and a loopback HTTP probe.")
            backup_enabled = True
            upgrade_policy = "manual_review"
        else:
            backup_enabled = typer.confirm("Enable backups?", default=backup_enabled)
        backup_path = typer.prompt("Backup directory", default=backup_path)
        state_path = typer.prompt("Private state directory", default=state_path)
        if environment != "production":
            upgrade_policy = typer.prompt(
                "Upgrade policy (manual_review or installed)", default=upgrade_policy
            )
        health_url = (
            typer.prompt(
                "Loopback HTTP health URL (blank for service check only)",
                default=health_url or "",
                show_default=False,
            )
            or None
        )
    if databases is None:
        raise GhostError("Supply --databases when initializing non-interactively.")

    data = validate_config(
        {
            "schema_version": 1,
            "project": {"name": project},
            "odoo": {
                "version": odoo_version,
                "service": service,
                "service_user": service_user,
                "executable": executable,
                "interpreter": interpreter,
                "config_file": odoo_config,
                "addons_path": [value.strip() for value in (addons or checkout).split(",")],
            },
            "repository": {"branch": branch, "checkout_path": checkout},
            "deployment": {
                "environment": environment,
                "databases": [value.strip() for value in databases.split(",")],
                "backup": {
                    "enabled": backup_enabled,
                    "path": backup_path,
                    "min_free_mib": backup_min_free_mib,
                },
                "state_path": state_path,
                "upgrade_policy": upgrade_policy,
                "health_check": {"url": health_url},
            },
        }
    )
    if verify_databases:
        preview_database_connections(data)
    content = yaml.safe_dump(data.model_dump(exclude_none=True), sort_keys=False)
    out.print(Text(f"Configuration preview for {config}"))
    out.print(Text(content.rstrip()))
    if not non_interactive and not typer.confirm("Write this configuration?", default=False):
        raise typer.Abort()
    save(config, content, force=force, interactive=not non_interactive)
    out.print(
        Text(f"Created {config}. Run ghostdeploy validate, then run doctor on the Odoo host.")
    )


@app.command()
@command
def validate(config: ConfigPath = DEFAULT_CONFIG, output: Output = "text"):
    """Validate the configuration without inspecting the host."""
    data = load(config)
    if output == "json":
        emit_json({"ok": True, "schema_version": data.schema_version, "warnings": data.warnings()})
        return
    out.print("[green]Configuration is valid.[/green]")
    for warning in data.warnings():
        out.print(Text(warning, style="yellow"))


@app.command()
def version():
    """Print the installed GhostDeploy version."""
    typer.echo(package_version("ghostdeploy"))


@app.command()
@command
def doctor(config: ConfigPath = DEFAULT_CONFIG, output: Output = "text"):
    """Inspect prerequisites on the Odoo host without making changes."""
    findings = diagnose(Host(load(config), Runner()))
    ok = all(item.ok for item in findings)
    if output == "json":
        emit_json({"ok": ok, "checks": [{**asdict(item), "ok": item.ok} for item in findings]})
    else:
        table = Table("Check", "Status", "Details", "Remediation")
        for item in findings:
            table.add_row(
                Text(item.check),
                Text(
                    item.status,
                    style={"PASS": "green", "WARN": "yellow", "FAIL": "red", "SKIP": "dim"}[
                        item.status
                    ],
                ),
                Text(item.detail),
                Text(item.remediation or "—"),
            )
        out.print(table)
        out.print(
            "Doctor checks prerequisites. Review an exact deployment plan before authorizing work."
        )
    if not ok:
        raise typer.Exit(1)


def show_plan(settings: Config, data: Plan, output: str, *, dry_run: bool = False) -> None:
    backup = settings.deployment.backup
    required_approvals = approvals(settings, data)
    planned_operations = operations(data)
    if output == "json":
        emit_json(
            {
                "ok": not data.blockers,
                "dry_run": dry_run,
                **asdict(data),
                "project": settings.project.name,
                "environment": settings.deployment.environment,
                "required_approvals": required_approvals,
                "backup_policy": backup.model_dump(),
                "shared_service_impact": data.restart_required,
                "operations": planned_operations,
            }
        )
        return
    out.print("[bold]Deployment dry run[/bold]" if dry_run else "[bold]Deployment plan[/bold]")
    out.print(Text(f"{settings.project.name} · {settings.deployment.environment}"))
    out.print(Text(f"Current: {data.base_sha}\nTarget:  {data.target_sha}"))
    if data.changes:
        table = Table("Status", "Path", "Addons", "Classification")
        for item in data.changes:
            table.add_row(
                item.status,
                Text(" → ".join(ascii(path) for path in item.paths)),
                ", ".join(item.addons),
                ", ".join(item.classifications),
            )
        out.print(table)
    out.print("Changed addons", ", ".join(data.affected_addons) or "none")
    out.print("Candidate upgrades", ", ".join(data.candidate_upgrades) or "none")
    out.print("Allowlisted databases", ", ".join(data.databases))
    out.print(
        Text(
            f"Backups: required for upgrades · {backup.path} · {backup.min_free_mib} MiB reserve"
            if backup.enabled
            else "Backups: disabled. Module upgrades cannot proceed."
        )
    )
    if data.installed_by_database is None:
        out.print("Installed modules have not been inspected.")
    else:
        for database, modules in data.installed_by_database.items():
            out.print(f"  {database}: {', '.join(modules) or 'no installed candidates'}")
    for label, values, style in (
        ("Warning", data.warnings, "yellow"),
        ("Review", data.review, "yellow"),
        ("Blocked", data.blockers, "red"),
        ("Approval", required_approvals, "yellow"),
        ("Preflight", data.preflight, "dim"),
    ):
        for value in values:
            out.print(Text(f"{label}: {value}", style=style))
    out.print(
        "[bold]Operations if approved[/bold]"
        if not data.blockers
        else "[bold]Planned operations are blocked[/bold]"
    )
    for index, operation in enumerate(planned_operations, 1):
        out.print(Text(f"{index}. {operation['detail']}"))


def compute_plan(
    settings: Config,
    base_sha: str | None,
    target_sha: str | None,
    local_checkout: Path | None,
    inspect_databases: bool,
) -> Plan:
    if inspect_databases and local_checkout is not None:
        raise GhostError("Database inspection uses the configured checkout on the Odoo host.")
    runner = Runner()
    repo = GitRepository(
        local_checkout or Path(settings.repository.checkout_path),
        settings.repository.branch,
        runner,
    )
    installed = None
    if inspect_databases:
        host = Host(settings, runner)
        host.require_identity()
        host.read_odoo_config()
        host.service_process(host.systemd())
        installed = host.inspect_databases()
    return build_plan(
        settings, repo, base_sha=base_sha, target_sha=target_sha, installed=installed
    )


@app.command()
@command
def plan(
    config: ConfigPath = DEFAULT_CONFIG,
    base_sha: str | None = None,
    target_sha: str | None = None,
    local_checkout: Annotated[
        Path | None, typer.Option(help="Inspect a local clone without changing deployment paths.")
    ] = None,
    inspect_databases: Annotated[
        bool, typer.Option(help="Read installed modules from the allowlisted databases.")
    ] = False,
    output: Output = "text",
):
    """Compare committed revisions and report deployment candidates."""
    settings = load(config)
    data = compute_plan(settings, base_sha, target_sha, local_checkout, inspect_databases)
    show_plan(settings, data, output)
    if data.blockers:
        raise typer.Exit(1)


@app.command()
@command
def deploy(
    config: ConfigPath = DEFAULT_CONFIG,
    target_sha: str | None = None,
    execute: Annotated[bool, typer.Option(help="Authorize real deployment on this host.")] = False,
    dry_run: Annotated[bool, typer.Option(help="Show a read-only plan, the default.")] = False,
    upgrade: Annotated[
        list[str] | None,
        typer.Option(help="Approve an installed candidate. Repeat for each module."),
    ] = None,
    approve_review: Annotated[
        bool, typer.Option(help="Approve the review items for the exact target revision.")
    ] = False,
    confirm_production: Annotated[
        bool, typer.Option(help="Authorize production when used with --execute.")
    ] = False,
    fetch: Annotated[
        bool, typer.Option(help="Fetch the configured GitHub branch under the execution lock.")
    ] = False,
    local_checkout: Annotated[
        Path | None, typer.Option(help="Use a local clone for preview only.")
    ] = None,
    inspect_databases: Annotated[
        bool, typer.Option(help="In preview, read installed candidates on the deployment host.")
    ] = False,
    output: Output = "text",
):
    """Preview or apply a deployment at an immutable Git commit."""
    if not execute:
        if upgrade or approve_review or confirm_production or fetch:
            raise GhostError(
                "Upgrade choices, review approval, production confirmation and fetch require --execute."
            )
        settings = load(config)
        data = compute_plan(settings, None, target_sha, local_checkout, inspect_databases)
        show_plan(settings, data, output, dry_run=True)
        if data.blockers:
            raise typer.Exit(1)
        return
    if dry_run or target_sha is None:
        raise GhostError(
            "Use --execute with --target-sha and omit --dry-run to apply a deployment."
        )
    if local_checkout is not None or inspect_databases:
        raise GhostError("--local-checkout and --inspect-databases are preview options.")
    settings = load(config)
    runner = Runner()
    report = Executor(settings, runner, Host(settings, runner)).execute(
        target_sha,
        selected=upgrade or [],
        approve_review=approve_review,
        confirm_production=confirm_production,
        fetch=fetch,
    )
    if output == "json":
        emit_json({"ok": report.status != "failed", **asdict(report)})
    else:
        show_report(settings, report)
    if report.status == "failed":
        raise typer.Exit(1)


def show_report(settings: Config, report: Report) -> None:
    out.print(
        Text(
            f"Deployment {report.id} · {report.status} · {settings.project.name} ({settings.deployment.environment})"
        )
    )
    out.print(
        Text(f"Started: {report.started_at}\nCompleted: {report.completed_at or 'not recorded'}")
    )
    out.print(Text(f"Last phase: {report.phase}"))
    out.print(Text("Completed checkpoints: " + (", ".join(report.completed_phases) or "none")))
    out.print(
        Text("Upgrade command succeeded: " + (", ".join(report.upgraded_databases) or "none"))
    )
    out.print(Text("Verified addon versions: " + (", ".join(report.verified_databases) or "none")))
    if report.in_progress_database:
        out.print(Text(f"Uncertain database: {report.in_progress_database}", style="yellow"))
    if report.backup_directory:
        out.print(Text(f"Backup directory: {report.backup_directory}"))
    for backup in report.backups:
        for artifact in ("dump", "filestore"):
            if path := backup.get(artifact, {}).get("path"):
                out.print(Text(f"  {backup['database']} {artifact}: {path}"))
    if health := report.health:
        out.print(
            Text(
                f"Systemd PID {health.process_pid}, HTTP {health.http_status or 'not checked'}. Application behavior requires separate validation."
            )
        )
    archive = Path(settings.deployment.state_path) / f"{report.id}.json"
    if archive.is_file():
        out.print(Text(f"Report: {archive}"))
    if report.status == "failed":
        out.print(Text(f"Pending journal: {archive.parent / (report.unit + '.pending.json')}"))
        if report.error:
            err.print(Text(report.error, style="red"))
        if report.recovery:
            err.print(Text(report.recovery, style="yellow"))


@workflow.command("generate")
@command
def generate_workflow(
    config: ConfigPath = DEFAULT_CONFIG,
    path: Annotated[Path, typer.Option(help="Destination workflow path.")] = Path(
        ".github/workflows/ghostdeploy.yml"
    ),
    force: Annotated[bool, typer.Option(help="Replace an existing destination.")] = False,
    manual_only: Annotated[
        bool, typer.Option(help="Generate only a manual workflow, including for staging.")
    ] = False,
):
    """Write a GitHub Actions workflow for the configured environment and branch."""
    settings = load(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, generate(settings, manual_only=manual_only), force=force, interactive=False)
    out.print(
        Text(f"Created {path}. Configure these GitHub environment secrets: {', '.join(SECRETS)}.")
    )
    out.print("Run ghostdeploy workflow setup for the host, SSH and GitHub environment checklist.")


@workflow.command("setup")
@command
def workflow_setup(config: ConfigPath = DEFAULT_CONFIG, output: Output = "text") -> None:
    """Show the manual SSH and GitHub setup checklist. Make no changes."""
    guide = setup_instructions(load(config))
    if output == "json":
        emit_json(guide)
        return
    out.print(Text(f"GitHub Actions setup · {guide['environment']} · {guide['branch']}"))
    for index, step in enumerate(guide["steps"], 1):
        out.print(Text(f"{index}. {step}"))
    out.print(Text(guide["authorized_key_example"]))
