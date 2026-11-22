# Deployment, recovery and staging validation

Run `doctor`, review the plan for an exact SHA, and rerun the plan with `--inspect-databases` on the staging host. If the approved change affects `attendance` and only `company_a` and `company_b` have it installed, those databases get `-u attendance` and their own backups. A third allowlisted database without the module is inspected but not upgraded. Every tenant using the managed Odoo service experiences downtime if the revision requires a restart. Keep other application and integration writers quiescent while the service is down.

1. Verify authorization and configuration, then acquire the local per-service lock. Check for an unresolved journal.
2. Verify the runtime, active service identity and health, permissions, and the clean configured branch. Optionally fetch the configured branch, verify target ancestry and build an exact plan.
3. Inspect the database allowlist, resolve required module approvals, verify the upgrade role and check backup space. Write a pending journal.
4. For runtime changes, stop the service and recheck the database module states. For a migration, recheck storage and create and verify the PostgreSQL and filestore archives while Odoo is stopped.
5. Fast-forward to the target SHA. For each affected database, record the in-progress name, upgrade the explicit module list and recheck installed module states. Process databases sequentially.
6. Start Odoo and verify its systemd process and optional local HTTP endpoint. Write the final report and release the lock. Documentation and workflow-only revisions can synchronize with the service running. An already applied SHA produces an `unchanged` report.

## Backups and lock files

Keep backup and state directories service-owned, mode `0700`, on local storage, with a single stable state location for the service. Do not delete the per-service `.lock` file. Linux releases its advisory lock when the process exits. A `.pending.json` file persists independently and blocks subsequent executions. Check for still-running commands and systemd jobs after interruption. Archive the pending file only after an operator has confirmed recovery. Individual reports and backup metadata are written atomically. Inspect both the report and backup directory when a backup was interrupted.

Each verified backup records the database, exact revisions, creation time, deployment ID, file paths, sizes and SHA-256 checksums. GhostDeploy decodes the dump to a discarded SQL stream with `pg_restore`, and reads the compressed filestore archive to validate its integrity. The metadata records `absent_at_snapshot` when the database has no filestore directory. This verification does not apply SQL to a database or test that attachments work after a restore. The estimate uses database size plus a filestore estimate and configurable free space reserve. Quotas, unusual PostgreSQL objects and late storage failures can still invalidate a backup. Keep retention and off-host copies under your own control. [PostgreSQL `pg_dump`](https://www.postgresql.org/docs/current/app-pgdump.html) · [PostgreSQL `pg_restore`](https://www.postgresql.org/docs/current/app-pgrestore.html)

## Recovery decisions

| Last recorded phase | Operator action |
| --- | --- |
| `prepared` or `stopping_service` | Check that the journaled base is still present, verify the unit and any queued systemd jobs, then reconcile the original service. |
| `service_stopped` or `creating_backups` | Expect no Git or migration changes. GhostDeploy attempts to bring back the verified original service on a handled failure. Investigate filesystem, role or backup issues and check per-database backup metadata. |
| `synchronizing_code` or `code_synchronized` | Check exact HEAD and any incomplete Git operation. Confirm no migration began before selecting the compatible revision. |
| `upgrading_database`, `verifying_database` or `database_upgraded` | Keep the service isolated. `upgraded_databases` contains invocations that returned success. `in_progress_database` flags the name whose state is uncertain or is still being verified. Later databases were not upgraded. A failed command may already have written data. |
| `starting_service` or `checking_health` | All requested upgrade commands finished. GhostDeploy attempts to stop Odoo again on a handled start or health error. Check the actual service status, journal and addon compatibility before opening traffic. |

For recovery after a migration, pair the matching pre-deployment Git commit, database dump and corresponding filestore. Check the recorded hashes and practice restoring to a fresh disposable PostgreSQL database with `pg_restore --exit-on-error`. Inspect archive members before extracting a trusted filestore to a fresh directory. Match the original ownership and point an isolated Odoo instance at the restored pair. Exercise login and an attachment download before deciding on any real recovery. Restore every affected database if returning the shared service to the old revision requires it. GhostDeploy provides no automatic rollback or database restore.

Use `journalctl -u odoo19.service` or the administrator-only Odoo log to diagnose startup and module errors. GhostDeploy's CLI and Actions output show phase and status without forwarding raw child output. For a read-only diagnosis, run `ghostdeploy doctor --output json`. For a blocked plan, inspect `blockers`, `review`, `installed_by_database` and the exact revisions in `ghostdeploy plan --output json`. Fix dirty or ignored files by reconciling the deployment checkout without discarding changes. See the [supported Odoo commands and options](https://www.odoo.com/documentation/19.0/developer/reference/cli.html).

## Staging validation before production

1. On a disposable Ubuntu VM, configure Odoo 18, PostgreSQL and systemd using a test service user and synthetic data. Repeat separately for Odoo 19.
2. Use three databases. Install a small custom addon on two, and seed attachments in one. Create a commit with a deliberately small model or view change. Compare offline and database-aware plans, then run the dry run and verify that it makes no state changes.
3. Execute the exact SHA with explicit module approval. Verify the installed module behavior in both target databases and confirm the third receives no explicit upgrade. Restore a backup into a fresh isolated environment and open an attachment there.
4. Repeat with a deliberately failed backup and with a migration that fails only on the second database. Verify downtime behavior, the in-progress field, the persistent journal and the refusal to retry until an operator reconciles state. Also test service start errors, HTTP probe failure and simultaneous deployment attempts.
5. Configure the staging Actions environment and run the workflow manually through the forced SSH account. Verify wrong-branch requests and unverified host keys are rejected. Measure maintenance and restore time under representative load before deciding on production use.
