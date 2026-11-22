import json
from dataclasses import asdict
from functools import wraps
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated, Literal

import typer
import yaml
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ghostdeploy.config import load
from ghostdeploy.config import validate as validate_config
from ghostdeploy.core import GhostError, Runner, safe_error
from ghostdeploy.deploy import Executor
from ghostdeploy.files import write_atomic
from ghostdeploy.git import GitRepository
from ghostdeploy.planning import Plan, build_plan
from ghostdeploy.runtime import Host
from ghostdeploy.workflow import SECRETS, generate

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
    upgrade_policy: str = "manual_review",
    health_url: str | None = None,
):
    """Create and validate a GhostDeploy configuration."""
    if not non_interactive:
        out.print("[bold]GhostDeploy setup[/bold]")
        project = typer.prompt("Project name", default=project)
        odoo_version = typer.prompt("Odoo version (18.0 or 19.0)", default=odoo_version)
        service = typer.prompt("Systemd service name without .service", default=service)
        service_user = typer.prompt("Service user", default=service_user)
        executable = typer.prompt("Odoo executable", default=executable)
        interpreter = (
            typer.prompt(
                "Python interpreter (blank if directly executable)",
                default=interpreter or "",
                show_default=False,
            )
            or None
        )
        odoo_config = typer.prompt("Odoo configuration file", default=odoo_config)
        checkout = typer.prompt("Git checkout", default=checkout)
        addons = typer.prompt(
            "Custom addon roots, separated by commas", default=addons or checkout
        )
        branch = typer.prompt("Deployment branch", default=branch)
        environment = typer.prompt("Environment", default=environment)
        databases = typer.prompt(
            "Database names, separated by commas", default=databases or "odoo_staging"
        )
        backup_enabled = typer.confirm("Enable backups?", default=backup_enabled)
        backup_path = typer.prompt("Backup directory", default=backup_path)
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
                "backup": {"enabled": backup_enabled, "path": backup_path},
                "upgrade_policy": upgrade_policy,
                "health_check": {"url": health_url},
            },
        }
    )
    content = yaml.safe_dump(data.model_dump(exclude_none=True), sort_keys=False)
    out.print(Text(content.rstrip()))
    if not non_interactive and not typer.confirm("Write this configuration?", default=True):
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
    findings = Host(load(config), Runner()).inspect()
    ok = all(item.ok for item in findings)
    if output == "json":
        emit_json({"ok": ok, "checks": [asdict(item) for item in findings]})
    else:
        table = Table("Check", "Status", "Details")
        for item in findings:
            table.add_row(
                item.check, "[green]OK[/green]" if item.ok else "[red]Fix[/red]", Text(item.detail)
            )
        out.print(table)
    if not ok:
        raise typer.Exit(1)


def show_plan(data: Plan, output: str, *, dry_run: bool = False) -> None:
    if output == "json":
        emit_json({"ok": not data.blockers, "dry_run": dry_run, **asdict(data)})
        return
    out.print("[bold]Deployment dry run[/bold]" if dry_run else "[bold]Deployment plan[/bold]")
    out.print(Text(f"{data.base_sha} → {data.target_sha}"))
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
    out.print("Candidate upgrades", ", ".join(data.candidate_upgrades) or "none")
    out.print("Allowlisted databases", ", ".join(data.databases))
    if data.installed_by_database is None:
        out.print("Installed modules have not been inspected.")
    else:
        for database, modules in data.installed_by_database.items():
            out.print(f"  {database}: {', '.join(modules) or 'no installed candidates'}")
    for label, values, style in (
        ("Warning", data.warnings, "yellow"),
        ("Review", data.review, "yellow"),
        ("Blocked", data.blockers, "red"),
        ("Preflight", data.preflight, "dim"),
    ):
        for value in values:
            out.print(Text(f"{label}: {value}", style=style))


def compute_plan(
    config: Path,
    base_sha: str | None,
    target_sha: str | None,
    local_checkout: Path | None,
    inspect_databases: bool,
) -> Plan:
    settings = load(config)
    runner = Runner()
    repo = GitRepository(
        local_checkout or Path(settings.repository.checkout_path),
        settings.repository.branch,
        runner,
    )
    installed = None
    if inspect_databases:
        if local_checkout is not None:
            raise GhostError("Database inspection uses the configured checkout on the Odoo host.")
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
    data = compute_plan(config, base_sha, target_sha, local_checkout, inspect_databases)
    show_plan(data, output)
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
    output: Output = "text",
):
    """Preview or apply a deployment at an immutable Git commit."""
    if not execute:
        if upgrade or approve_review or confirm_production or fetch:
            raise GhostError(
                "Upgrade choices, review approval, production confirmation and fetch require --execute."
            )
        data = compute_plan(config, None, target_sha, None, False)
        show_plan(data, output, dry_run=True)
        if data.blockers:
            raise typer.Exit(1)
        return
    if dry_run or target_sha is None:
        raise GhostError(
            "Use --execute with --target-sha and omit --dry-run to apply a deployment."
        )
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
        out.print(Text(f"Deployment {report.id} {report.status}. Recorded phase: {report.phase}."))
        if report.error:
            err.print(Text(report.error, style="red"))
        if report.recovery:
            err.print(Text(report.recovery, style="yellow"))
        out.print(Text(f"Report: {Path(settings.deployment.state_path) / (report.id + '.json')}"))
    if report.status == "failed":
        raise typer.Exit(1)


@workflow.command("generate")
@command
def generate_workflow(
    config: ConfigPath = DEFAULT_CONFIG,
    path: Annotated[Path, typer.Option(help="Destination workflow path.")] = Path(
        ".github/workflows/ghostdeploy.yml"
    ),
    force: Annotated[bool, typer.Option(help="Replace an existing destination.")] = False,
):
    """Write a GitHub Actions workflow for the configured environment and branch."""
    settings = load(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, generate(settings), force=force, interactive=False)
    out.print(
        Text(f"Created {path}. Configure these GitHub environment secrets: {', '.join(SECRETS)}.")
    )
    out.print(
        "Install the restricted ghostdeploy-ssh entry point on the host before enabling the workflow."
    )
