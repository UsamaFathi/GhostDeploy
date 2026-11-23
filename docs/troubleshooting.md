# Troubleshooting

Start with `ghostdeploy validate --config /etc/ghostdeploy.yml` and then run `doctor --output json` as the Odoo service account. Every failure or skipped prerequisite has a remediation field.

| Symptom | What to check |
| --- | --- |
| The wizard finds no Odoo service | Run on Ubuntu as the service account. The wizard looks for systemd names containing `odoo` and recognizes direct `-c /path/to/config` commands. Use manual entry for other names, stopped services and wrapper scripts. |
| Doctor skips a database | Resolve the missing `psql`, service identity, or Odoo configuration check. GhostDeploy tests only the explicit allowlist. |
| Doctor rejects the PostgreSQL role | Check that the role has neither `SUPERUSER` nor `CREATEDB`. Review grants with your database administrator. Keep credentials in your existing Odoo authentication configuration. |
| The checkout is on another branch or is dirty | Check Git status with the service account. Review changes and reconcile them intentionally. The deployment will block if it cannot use the exact approved revision. |
| An Actions job rejects a stale SHA | A newer commit can make a queued run stale. Review the latest tip and dispatch it manually if needed. |
| The SSH host key changed | Obtain the new key through an independent administrative channel and verify it before updating `SSH_KNOWN_HOSTS`. |
| A pending journal blocks execution | Locate the journal in the state directory and follow the [recovery decisions](operations.md#recovery-decisions). Do not remove it just to retry. |
| HTTP passed but the application is wrong | Check service logs, tenant login and the affected feature. HTTP response, addon version checks and functional checks are separate. |

Use `ghostdeploy deploy --dry-run` or `ghostdeploy plan` to review an exact commit without host changes. For a suspected security issue, use [security reporting](../SECURITY.md).
