# GhostDeploy

GhostDeploy is an MIT-licensed deployment toolkit for developers who maintain self-hosted Odoo. It turns the usual SSH, Git, module upgrade, restart and verification steps into one auditable operation. You run it on your own host. It sends no telemetry and keeps backups on storage you control.

GhostDeploy 0.3.0.dev0 is **alpha**. It targets Odoo 18.0 and 19.0 on Ubuntu with one systemd service, PostgreSQL and a GitHub repository of custom addons. Start with the [Quick start](docs/quick-start.md) for workstation, Odoo host and GitHub steps. The [support and verification matrix](docs/testing.md#support-and-verification) lists the checks that have run and the staging gates that remain.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
ghostdeploy validate --config examples/staging-odoo19.yml
```

Install a wheel built from a reviewed Git commit on the server. The project has not been published to PyPI. On Ubuntu, the wizard can suggest a selected service's paths, addon roots and declared database names. Enter the database allowlist explicitly. `init --non-interactive --databases training_east,training_west` works without an Odoo installation. Run `doctor` on the target host as the Odoo account.

| Command | Purpose |
| --- | --- |
| `ghostdeploy validate --output json` | Check schema and report warnings without reading the host. |
| `ghostdeploy doctor` | Report PASS, WARN, FAIL or SKIP for host, checkout, database and SSH prerequisites. |
| `ghostdeploy plan --base-sha "$BASE" --target-sha "$TARGET"` | Compare exact Git revisions and identify candidate upgrades and review items. |
| `ghostdeploy plan --inspect-databases --target-sha "$TARGET"` | Read installed module states from the configured database allowlist on the host. |
| `ghostdeploy deploy --dry-run --target-sha "$TARGET"` | Preview without fetching, writing files, restarting or upgrading. |
| `ghostdeploy deploy --execute --target-sha "$TARGET" --upgrade attendance` | Apply the exact revision and approve the installed `attendance` candidate under the default review policy. |
| `ghostdeploy workflow setup` | Show the SSH, pinned host key and GitHub environment checklist. |
| `ghostdeploy workflow generate --manual-only` | Generate a branch-scoped manual workflow for the first staging run. |
| `ghostdeploy version` | Print the installed package version. |

The executor holds a per-service lock and accepts only a clean checkout and a full commit SHA that can be fast-forwarded. A deployment that fetches, including GitHub Actions, requires the current branch tip. Database inspection records the identity and installed addon versions of every allowlisted database. If `attendance` is installed in only two of three allowed databases, GhostDeploy requests its upgrade only in those two and verifies the reported version afterward. A service stop and start still affects every database the instance serves.

For upgrades, GhostDeploy checks storage, stops Odoo, and verifies PostgreSQL dumps and matching filestore archives before switching the Git revision. It writes a durable phase journal before code or database changes. When an upgrade fails, GhostDeploy records completed databases and the uncertain database, stops further upgrades and requires operator reconciliation. Restore and rollback are manual. See [operations](docs/operations.md) for the sequence, failure states and a restore drill.

Trust the configured Git branch and the Odoo service account. Addon upgrades execute trusted application code with that account. Use protected branches and GitHub environments, tightly scoped systemd permissions, a PostgreSQL role without database creation privileges, and the [SSH setup guide](docs/github-actions.md). Root SSH, untrusted pull requests and automatic production pushes are outside the design.

The first release expects a dedicated, direct systemd launch and a regular Git checkout. It blocks addon removal, changes to dependency metadata and target trees with symlinks or submodules. Availability checks are limited to systemd status and an optional local HTTP probe. Containers, additional platforms, coordinated multi-service deployments and automatic rollback are deferred. The [CLI and configuration reference](docs/reference.md) explains the boundaries and review rules.

```sh
pytest
ruff check .
ruff format --check .
python -m build
python -m twine check --strict dist/*
```

See [installation and upgrades](docs/installation.md), [first deployment](docs/first-deployment.md), [multiple databases](docs/multi-database.md), [backup and recovery](docs/operations.md), [troubleshooting](docs/troubleshooting.md) and [security](SECURITY.md). The [test and release guide](docs/testing.md) explains how to run the PostgreSQL and Odoo suites without a VPS. [Phase 3 report](docs/phase-3-report.md) · [Contributing](CONTRIBUTING.md)
