# Changelog

## 0.2.0.dev0 (unreleased)

- Record and verify per-database identity, installed addon versions and explicit upgrade dependencies.
- Retain pending recovery journals after failed or interrupted operations. Bind the recovery directory to its service and add a Linux namespace lock.
- Reject stale fetched targets, shallow or grafted Git histories, overlapping storage and legacy or zipped addons in managed roots.
- Verify and publish private backup artifacts incrementally, including Linux filestore checks for links and concurrent changes.
- Add regression tests, a disposable PostgreSQL restore and Odoo CLI fixtures for both target versions. Live systemd and SSH verification remain pending.

Behavior changes for 0.1.0 users include stale fetched commits being refused, retained journals after pre-synchronization failures, and a service account home binding for the original state directory. See [recovery and storage migration](docs/operations.md#backups-and-lock-files).

## 0.1.0 (unreleased)

- Add an installable CLI, strict YAML schema, initialization wizard, configuration validation and host diagnostics.
- Build read-only Git plans with addon discovery, review gates and optional allowlisted database inspection.
- Add exact-SHA deployments, per-service locking, verified PostgreSQL and filestore backups, Odoo module upgrades and durable failure reports.
- Generate branch-scoped GitHub Actions workflows and provide a restricted SSH entry point.
- Document setup, backup recovery and disposable staging validation. Live staging verification is pending.
