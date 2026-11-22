# GhostDeploy 0.1.0 engineering report

GhostDeploy is an installable Python CLI for managing one self-hosted Odoo service and a checkout of its custom addons. The implementation covers configuration, exact-commit planning, database-aware upgrades, backups, service control, reporting and GitHub Actions generation. Local checks support advancing to a controlled staging exercise. Production use depends on a successful staging exercise and restore drill.

## Architecture and repository structure

```text
GhostDeploy/
├── .github/workflows/ci.yml
├── ghostdeploy/
│   ├── __init__.py, __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── core.py, files.py
│   ├── git.py, planning.py
│   ├── runtime.py, backup.py, deploy.py
│   └── workflow.py
├── tests/
│   ├── conftest.py
│   ├── test_config_cli.py
│   ├── test_deploy.py
│   ├── test_planning.py
│   └── test_workflow.py
├── examples/ghostdeploy.yml
├── docs/
│   ├── engineering-report.md
│   ├── github-actions.md
│   ├── installation.md
│   ├── operations.md
│   └── reference.md
├── .gitignore, MANIFEST.in, pyproject.toml
└── README.md, CHANGELOG.md, CONTRIBUTING.md, SECURITY.md, LICENSE
```

| Module | Responsibility |
| --- | --- |
| `cli`, `config` | Typer commands, Rich terminal output, JSON responses and strict schema validation. |
| `core`, `files` | Structured command execution with sanitized failures and atomic, durable file writes. |
| `git`, `planning` | Resolve exact commit objects, inspect committed trees and diffs, classify changes, discover addons and construct read-only plans. |
| `runtime` | Verify the Ubuntu/systemd environment and Odoo process, inspect allowlisted PostgreSQL databases, invoke Odoo and check health. |
| `backup` | Estimate storage, write and verify database and filestore archives, and record per-database metadata. |
| `deploy` | Apply approval rules, coordinate the lock and operations, and persist the deployment state and recovery guidance. |
| `workflow` | Generate GitHub Actions YAML and validate the restricted server-side SSH request. |

The command runner is the test seam for Git, PostgreSQL, sudo and Odoo subprocesses. Planning reads Git commits independently of the host, while `Host` encapsulates Ubuntu/systemd details. The executor owns lifecycle ordering, so future platform support can replace runtime behavior while retaining the same planning and journal decisions.

## Working commands

```sh
ghostdeploy --help
ghostdeploy version
ghostdeploy init --non-interactive --databases company_a,company_b
ghostdeploy validate --output json
ghostdeploy doctor --config /etc/ghostdeploy.yml
ghostdeploy plan --local-checkout ./custom-addons --base-sha "$BASE" --target-sha "$TARGET"
ghostdeploy plan --config /etc/ghostdeploy.yml --inspect-databases --target-sha "$TARGET"
ghostdeploy deploy --config /etc/ghostdeploy.yml --dry-run --target-sha "$TARGET"
ghostdeploy deploy --config /etc/ghostdeploy.yml --execute --target-sha "$TARGET" --upgrade attendance
ghostdeploy workflow generate --path .github/workflows/deploy-staging.yml
ghostdeploy-ssh --help
```

The server-side SSH entry point is intended for `authorized_keys`, with its config path fixed by the host administrator. For production local execution, add `--confirm-production`. Apply `--approve-review` only after inspecting the exact plan. See the [full command reference](reference.md).

## Deployment and database behavior

Git plans identify changed addons from both revisions, including renamed and deleted files. They classify Python changes as potential schema changes and separate standalone Git plans from database-inspected results. The executor operates only on named databases. For each database it intersects the plan's candidates with modules PostgreSQL reports as installed. Under `manual_review` the operator must approve every resulting module. The opt-in `installed` policy can apply ordinary candidates in development or staging. Pending Odoo module operations, the special `all` selector, removed addons and specified dependency changes block execution. Odoo receives one explicit module list per affected database. A restart still affects all tenants served by the instance.

The executor acquires the per-service lock, checks the host and exact commit, inspects database state and verifies permissions and storage. For runtime changes, it stops Odoo before creating and verifying backups, fast-forwards to the approved commit, upgrades databases one at a time, starts Odoo and performs health checks. It writes the phase journal before any operational change. Documentation and workflow-only updates may fast-forward while Odoo remains active. See [operations and recovery](operations.md) for the complete order and phase-by-phase recovery decisions.

A failure before synchronization can recover to the verified original service after a handled backup failure. A Git error or a migration that may have changed data leaves the relevant phase and pending journal. The journal flags completed upgrade invocations and the database whose state is uncertain. A handled start or health error triggers a stop attempt. After possible database changes, an operator must reconcile the exact code and matching database and filestore before reopening traffic or retrying. GhostDeploy never restores a database automatically.

## Security and remaining risks

The CLI uses structured argument lists and strict validation, suppresses raw child output, reads database connection settings from Odoo, and rejects Git operations outside a full, clean and compatible checkout. It rejects a root execution identity, limits the documented sudo rules to a named unit, checks the PostgreSQL role for database upgrades, and writes snapshots and reports under private directories. Production config requires a manual policy, backups and a direct loopback HTTP probe. The generated workflow has no token permissions, pins the SSH host key, restricts branch eligibility, uses deployment concurrency and defaults production to manual dispatch. GitHub environment protections and forced-command key installation are administrator prerequisites. See [SSH and Actions setup](github-actions.md).

Trusted branch authors can introduce application code that executes as Odoo. The health check verifies process status and an optional HTTP response, so it cannot establish tenant-level correctness. A backup may be structurally readable while missing data written by an independent application. Keep every writer quiescent and test a restore on synthetic data. Git repo filters, external addon paths, custom systemd units, PostgreSQL authentication and Odoo CLI behavior need verification on the operator's target. Service unavailability during maintenance and manual reconciliation are intentional design tradeoffs.

## Verification

| Check | Result |
| --- | --- |
| Pytest on Windows with Python 3.11 | 43 collected, 42 passed, 0 failed, 1 skipped. The skipped case requires Linux `flock`. |
| Ruff lint and format | Passed. |
| Editable install and CLI smoke | Passed. Both CLI entry points loaded and the example config validated. |
| GitHub Actions validation | CI plus generated staging and production YAML passed actionlint. The SSH step ran against a local shell stub in tests. |
| Distribution build and metadata | Source archive and wheel built and passed strict Twine checks. |
| Live Odoo, PostgreSQL, systemd and SSH | Require a disposable Ubuntu staging exercise. |

| Feature | Status | Coverage |
| --- | --- | --- |
| Initialization, schema, JSON and diagnostics | Implemented and tested | CLI fixtures, including missing executable and malformed YAML. |
| Git planning and exact-SHA synchronization | Implemented and tested | Temporary repositories, cross-addon renames, dirty checkout, removed addons, fast-forward and duplicate execution. |
| Database selection and migration safeguards | Implemented and tested with fixtures | Multiple databases, pending state and reserved selectors, role checks, partial upgrade and failure journals. |
| Backups, service control and health | Implemented and tested with fixtures | Real filestore archives and checksums, simulated PostgreSQL/systemd failures and local health logic. Live archive restore and systemd remain unverified. |
| Advisory lock | Implemented, platform verification pending | Linux-specific test included in the CI matrix. |
| GitHub integration | Implemented and locally tested | Verified YAML, validated request grammar and a local SSH stub. Live runner, host key pin and forced-command installation remain unverified. |
| Runtime/platform expansion | Deferred | Containers, coordinated multi-service execution, older Odoo, alternate hosts and automatic restore or rollback. |

## Staging validation and next engineering priorities

Run the [disposable Ubuntu staging plan](operations.md#staging-validation-before-production) against both Odoo versions. Use three databases and synthetic attachments. Prove that only the two installed candidates upgrade, drill a failure during the second upgrade, and restore the pre-deployment pair to an isolated environment. Validate the GitHub workflow and the Linux lock under contention. Then record version-specific install and migration behavior, the measured downtime and the restore result before considering production. CI should be run on GitHub after the repository owner approves publishing the changes.
