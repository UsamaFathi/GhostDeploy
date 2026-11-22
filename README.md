# GhostDeploy

GhostDeploy is an MIT-licensed deployment toolkit for developers who maintain self-hosted Odoo. It turns the usual SSH, Git, module upgrade, restart and verification steps into one auditable operation. You run it on your own host. It sends no telemetry and keeps backups on storage you control.

The first release targets Odoo 18.0 and 19.0 on Ubuntu, with one systemd service, PostgreSQL and a GitHub repository of custom addons. It supports single and multiple database installations, multiple custom addon roots within one checkout, and optional GitHub Actions over SSH. The CLI and deployment engine have local tests. Live staging validation is required before production use.

```sh
python -m pip install -e '.[dev]'
ghostdeploy init
ghostdeploy validate
ghostdeploy plan --local-checkout /path/to/addons --base-sha "$BASE" --target-sha "$TARGET"
```

Install from a reviewed checkout or a locally built wheel. The project has not been published to PyPI. `init --non-interactive --databases company_staging` creates a starter config without a running Odoo instance. See the [example configuration](examples/ghostdeploy.yml) for the schema and the [host setup guide](docs/installation.md) for permissions and Odoo settings. Run `ghostdeploy doctor` on the target host before attempting execution.

| Command | Purpose |
| --- | --- |
| `ghostdeploy validate --output json` | Check schema and report warnings without reading the host. |
| `ghostdeploy doctor` | Check paths, permissions, tools and the systemd unit. |
| `ghostdeploy plan --base-sha "$BASE" --target-sha "$TARGET"` | Compare exact Git revisions and identify candidate upgrades and review items. |
| `ghostdeploy plan --inspect-databases --target-sha "$TARGET"` | Read installed module states from the configured database allowlist on the host. |
| `ghostdeploy deploy --dry-run --target-sha "$TARGET"` | Preview without fetching, writing files, restarting or upgrading. |
| `ghostdeploy deploy --execute --target-sha "$TARGET" --upgrade attendance` | Apply the exact revision and approve the installed `attendance` candidate under the default review policy. |
| `ghostdeploy workflow generate` | Generate a branch-scoped GitHub Actions workflow with pinned SSH host verification and concurrency. |
| `ghostdeploy version` | Print the installed package version. |

The executor holds a per-service lock and accepts only a clean, fast-forwardable checkout and a full SHA reachable from the configured origin branch. Its database inspection uses the Odoo connection settings and an explicit database allowlist. A changed Python file is a potential schema change. If `attendance` is installed in only two of three allowed databases, explicit `-u attendance` runs only in those two. The service stop and start still affects every database the instance serves.

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

Contributions are welcome. See the [contributing guide](CONTRIBUTING.md) and [security reporting guidance](SECURITY.md).
