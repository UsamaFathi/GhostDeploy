# Configuration and CLI reference

Use `--config PATH` after a subcommand to read a file other than `ghostdeploy.yml`. YAML is limited to 64 KiB and a versioned schema. Unknown fields, duplicate keys, aliases and unsafe tags are rejected. No configuration field accepts an arbitrary command or a credential. Ubuntu paths are absolute. The checkout and configured custom addon roots must be canonical and each addon root must be a distinct directory within that checkout.

| Key | Rules and defaults |
| --- | --- |
| `schema_version` | Integer `1`. |
| `project.name` | Project identifier used in workflow names. |
| `odoo.version` | String `"18.0"` or `"19.0"`. |
| `odoo.service` | Systemd unit name without `.service`. |
| `odoo.service_user` | Static, unprivileged account, default `odoo`. |
| `odoo.executable`, `odoo.config_file` | Absolute paths outside the checkout. |
| `odoo.interpreter` | Optional absolute path to the same Python interpreter used by the service. |
| `odoo.addons_path` | One or more non-overlapping custom addon roots. Each addon is an immediate child of a configured root. |
| `repository.branch`, `repository.checkout_path` | Deployment branch (default `main`) and full Git worktree root. |
| `deployment.environment` | `development`, `staging` or `production`. |
| `deployment.databases` | Non-empty ordered allowlist of distinct PostgreSQL database names. |
| `deployment.upgrade_policy` | `manual_review` by default. `installed` opts development or staging into upgrading all installed candidates. |
| `deployment.state_path` | Local service-owned directory, default `/var/lib/ghostdeploy`. It must be separate from the checkout and backup store. The service account's home records its authoritative state directory. |
| `deployment.command_timeout_seconds` | Per dump, archive verification and module upgrade timeout, default `3600`. |
| `deployment.backup` | `enabled` defaults to `true`; `path` defaults to `/var/backups/ghostdeploy`; `min_free_mib` is the free-space reserve above the estimate, default `512`. |
| `deployment.health_check` | `enabled` defaults to `true`; `timeout_seconds` defaults to `60`; `url` is optional and accepts only direct loopback HTTP with no credentials or query. |

Identifiers use letters, digits, underscores and hyphens and start with a letter or underscore. Module names use letters, digits and underscores. Production requires `manual_review`, backups and a loopback HTTP health URL. All environments require backups for database upgrades.

`ghostdeploy init` previews validated YAML before writing it. Use `--non-interactive --databases company_a,company_b` for scripts and `--force` when replacement is intentional. `validate`, `doctor`, `plan` and `deploy` accept `--output json`. Exit `0` means successful completion, `1` means a validation, preflight, blocking plan or execution failure, and `2` means invalid CLI usage.

The interactive wizard can suggest settings for a selected Ubuntu systemd service. It reads accessible installation metadata and displays names explicitly declared in Odoo's `db_name`. It never runs the discovered executable or selects a database for you. With `--verify-databases` it can check the entered allowlist against a local PostgreSQL socket or loopback address. This option does not enumerate databases. Use `init --no-discover` for manual prompts. Non-interactive initialization never discovers services and requires `--databases`. Replacing an existing destination requires confirmation or `--force`. Config validation still uses schema version 1.

Doctor's JSON `checks` retain `check`, `ok` and `detail` and add `status` and `remediation`. Status can be `PASS`, `WARN`, `FAIL` or `SKIP`. Overall `ok` is false when any check fails. Warnings and skipped checks can still matter for your setup. For example, SSH key restrictions require manual verification. Doctor never authorizes deployment.

`plan` defaults to `HEAD` and the local `origin/<branch>` tracking ref. When the latter is absent it compares `HEAD` to itself and emits a warning. Use full `--base-sha` and `--target-sha` to compare known commits. `--local-checkout PATH` lets you plan against a developer checkout, including on Windows, while keeping relative addon roots from the deploy config. `--inspect-databases` reads only the allowlist on the deployed host and intersects installed modules with candidates. Pending Odoo module operations are treated as blockers during inspection.

`deploy` defaults to a read-only plan. The preview accepts `--local-checkout` for workstation use or `--inspect-databases` on the host. Real execution requires `--execute --target-sha SHA`. `--fetch` updates the GitHub remote-tracking branch under the lock and requires the requested SHA to match its current tip. Without `--fetch`, an explicitly approved fast-forward can still target an ancestor of the cached tip. With `manual_review`, pass `--upgrade MODULE` for every installed candidate. With `installed`, providing any explicit choices still requires all installed candidates. Review items require `--approve-review` in either policy. Production additionally requires `--confirm-production` for local execution.

Plan JSON adds `project`, `environment`, `required_approvals`, `backup_policy`, `shared_service_impact`, and ordered `operations`. The operations describe the scope if approved and can include conditional upgrades. Deployment JSON adds `completed_at` and `completed_phases`. The latter records completed checkpoints. The last `phase` can represent work that was about to begin when the process stopped.

Manifests must be UTF-8 Python dictionary literals. Git-derived results include candidate upgrades and expected versions from target manifests. PostgreSQL-derived results include the installed candidates per database. Missing installed dependencies, pending operations, legacy manifests, zipped addons, removed addons and changes to dependency-related manifest fields block execution. Unknown changes and migrations require review. The execution report distinguishes command completion, version verification, systemd status and HTTP status. Application validation remains a separate check.

`workflow generate --manual-only --path .github/workflows/deploy-staging.yml` writes one manual staging workflow. It refuses to replace that path unless you pass `--force`. `workflow setup --output json` prints required operator actions without creating secrets, changing access or writing a workflow. See [GitHub Actions setup](github-actions.md) for the forced SSH entry point.
