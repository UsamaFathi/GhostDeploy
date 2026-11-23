from typing import TypedDict

from ghostdeploy.config import Config
from ghostdeploy.planning import Plan


class PlannedOperation(TypedDict):
    action: str
    detail: str


def approvals(config: Config, plan: Plan) -> tuple[str, ...]:
    required = ["Authorize deployment with --execute and the full --target-sha."]
    if config.deployment.environment == "production":
        required.append("Confirm production with --confirm-production for host CLI execution.")
    if plan.review:
        required.append("Review the flagged changes and pass --approve-review.")
    if config.deployment.upgrade_policy == "manual_review":
        if plan.installed_by_database is None and plan.candidate_upgrades:
            required.append("Inspect installed candidates and approve each with --upgrade MODULE.")
        elif plan.installed_by_database:
            modules = sorted(
                {module for items in plan.installed_by_database.values() for module in items}
            )
            if modules:
                required.append(
                    "Explicit module approvals: "
                    + ", ".join(f"--upgrade {module}" for module in modules)
                )
    return tuple(required)


def operations(plan: Plan) -> list[PlannedOperation]:
    steps: list[PlannedOperation] = [
        {
            "action": "preflight",
            "detail": "Acquire the service lock and verify host, revision and database state.",
        },
    ]
    if plan.restart_required:
        steps.append(
            {
                "action": "stop_service",
                "detail": "Pause Odoo before code changes. Every tenant on this service can experience downtime.",
            }
        )
    if plan.installed_by_database is None:
        upgrades = "If approved candidates are installed on an allowlisted database"
        should_upgrade = bool(plan.candidate_upgrades)
    else:
        targets = [
            f"{db} ({', '.join(modules)})"
            for db, modules in plan.installed_by_database.items()
            if modules
        ]
        upgrades = ", ".join(targets)
        should_upgrade = bool(targets)
    if should_upgrade:
        steps.append(
            {
                "action": "verify_backups",
                "detail": f"Verify PostgreSQL and filestore backups before upgrades. Scope: {upgrades}.",
            }
        )
    if plan.base_sha != plan.target_sha:
        steps.append(
            {
                "action": "fast_forward",
                "detail": "Move the clean checkout to the exact approved commit.",
            }
        )
    if should_upgrade:
        steps.append(
            {
                "action": "upgrade_and_verify",
                "detail": f"Upgrade and verify each allowed database separately. Scope: {upgrades}.",
            }
        )
    if plan.restart_required:
        steps.append(
            {
                "action": "start_service",
                "detail": "Start Odoo and check systemd and the configured HTTP probe, if present.",
            }
        )
    steps.append(
        {
            "action": "record_result",
            "detail": "Archive the result and retain a pending journal for unresolved failures.",
        }
    )
    return steps
