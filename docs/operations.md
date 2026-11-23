# Deployment, recovery and staging validation

Run `doctor`, review the plan for an exact SHA, and rerun the plan with `--inspect-databases` on the staging host. If the approved change affects `attendance` and only `company_a` and `company_b` have it installed, those databases get `-u attendance` and their own backups. A third allowlisted database without the module is inspected but not upgraded. Every tenant using the managed Odoo service experiences downtime if the revision requires a restart. Keep other application and integration writers quiescent while the service is down.

1. Verify authorization and configuration, acquire the per-service lock, enforce the service's state directory binding and check for an unresolved journal.
2. Verify the canonical systemd unit, Odoo process, configuration, permissions and the clean configured branch. If fetching, require the current tip. Check that the target can fast-forward the checkout.
3. Snapshot database identities and module versions for the allowlist. Resolve approvals, verify dependencies and the upgrade role, and check backup space. Write the pending journal before touching the service.
4. Stop the service for runtime changes, then confirm the original database snapshots. For upgrades, create and verify PostgreSQL dumps and filestore archives with the service stopped. Journal each verified backup as it completes.
5. Fast-forward to the approved SHA. Before each upgrade, recheck the checkout and database snapshots, then record the database being upgraded. Verify the expected module versions and the rest of the snapshot before continuing to the next database.
6. Start Odoo. Verify the active systemd process and optional HTTP response, then recheck the revision and database snapshots. Record `finalizing`, persist the final report, and clear the pending file only on success. Documentation and workflow-only changes can fast-forward while Odoo runs. An already applied SHA produces an `unchanged` report.

## Backups and lock files

Keep separate backup and state directories owned by the service account with mode `0700`, on local storage with trusted parents. Linux releases both the per-service namespace lock and the advisory file lock when the process exits. Leave the lock file in place. The binding in `$HOME/.local/state/ghostdeploy/<unit>.state` records the authoritative recovery directory. Upgrade from 0.1.0 with the existing `state_path` and its pending journal intact. To move storage, disable all deploy triggers, reconcile the host, retain the old reports, move the private state under administrator control, and update the binding and config together before re-enabling deployments.

The pending journal blocks execution after any failure past preparation, including a backup error. Check for running processes and queued systemd jobs after interruption. Session termination and command timeouts terminate the Linux child process group when GhostDeploy can handle the signal. An abrupt kill or host outage can leave earlier phases or running external jobs, which require inspection. Archive the pending file only after confirming recovery.

Each verified backup records the database, revisions, creation time, file sizes, SHA-256 checksums, and `test_restore: not_performed`. A successful dump is only the first step. GhostDeploy checks the archive by decoding it with `pg_restore` and reading the compressed filestore, then publishes verified paths and per-database metadata. Interrupted outputs may remain as `.partial` files. Use only artifacts referenced in verified metadata. A missing filestore is recorded as `absent_at_snapshot`, which is distinct from an empty directory. A test restore into a new database and validation of a restored Odoo instance are separate checks. Quotas, external writers and late storage failures can still invalidate a backup. [PostgreSQL `pg_dump`](https://www.postgresql.org/docs/17/app-pgdump.html) · [PostgreSQL `pg_restore`](https://www.postgresql.org/docs/17/app-pgrestore.html)

## Recovery decisions

| Last recorded phase | Operator action |
| --- | --- |
| `prepared` or `stopping_service` | Check that the journaled base is still present, verify the unit and any queued systemd jobs, then reconcile the original service. |
| `service_stopped` or `creating_backups` | Expect the original checkout and no GhostDeploy migrations. The service stays stopped. Inspect the database snapshots, disk and backup metadata, then start the original service only after confirming compatibility. |
| `synchronizing_code` or `code_synchronized` | Check exact HEAD and any incomplete Git operation. Confirm no migration began before selecting the compatible revision. |
| `upgrading_database`, `verifying_database` or `database_upgraded` | Keep the service isolated. `upgraded_databases` contains commands that returned zero. `verified_databases` lists databases that passed snapshot checks. `in_progress_database` flags a database whose state is uncertain. Later databases were not upgraded. A failed command can have committed data, and version equality alone cannot verify application behavior. |
| `starting_service` or `checking_health` | All requested upgrade commands finished. GhostDeploy attempts to stop Odoo again on a handled start or health error. Check the actual service status, journal and addon compatibility before opening traffic. |
| `finalizing` | Operational checks completed. Compare the pending journal and final archive. If archival failed, establish the actual revision and database state before clearing the pending file. |

For recovery after a migration, pair the matching pre-deployment Git commit, database dump and corresponding filestore. Check the recorded hashes and practice restoring to a fresh disposable PostgreSQL database with `pg_restore --exit-on-error`. Inspect archive members before extracting a trusted filestore to a fresh directory. Match the original ownership and point an isolated Odoo instance at the restored pair. Exercise login and an attachment download before deciding on any real recovery. Restore every affected database if returning the shared service to the old revision requires it. GhostDeploy provides no automatic rollback or database restore.

Use `journalctl -u odoo19.service` or the administrator-only Odoo log to diagnose startup and module errors. GhostDeploy's CLI and Actions output show phase and status without forwarding raw child output. For a read-only diagnosis, run `ghostdeploy doctor --output json`. For a blocked plan, inspect `blockers`, `review`, `installed_by_database` and the exact revisions in `ghostdeploy plan --output json`. Fix dirty or ignored files by reconciling the deployment checkout without discarding changes. See the [supported Odoo commands and options](https://www.odoo.com/documentation/19.0/developer/reference/cli.html).

## Staging validation before production

Run the [disposable integration fixtures](testing.md#disposable-odoo-and-ssh-integration) first. The Odoo images check the application and restore, and the hosted Ubuntu job checks the service adapter with a synthetic process. Use the [systemd staging procedure](systemd-staging.md) to exercise stop, restart, failure and interruption with Odoo on the same isolated Ubuntu VM.

1. On a disposable Ubuntu VM, configure Odoo 18, PostgreSQL and systemd using a test service user and synthetic data. Repeat separately for Odoo 19.
2. Use three databases. Install a small custom addon on two, and seed attachments in one. Create a commit with a deliberately small model or view change. Compare offline and database-aware plans, then run the dry run and verify that it makes no state changes.
3. Execute the exact SHA with explicit module approval. Verify the installed module behavior in both target databases and confirm the third receives no explicit upgrade. Restore a backup into a fresh isolated environment and open an attachment there.
4. Repeat with a deliberately failed backup and with a migration that fails only on the second database. Verify downtime behavior, the in-progress field, the persistent journal and the refusal to retry until an operator reconciles state. Also test service start errors, HTTP probe failure and simultaneous deployment attempts.
5. Configure the staging Actions environment and run the workflow manually through the forced SSH account. Verify wrong-branch requests and unverified host keys are rejected. Measure maintenance and restore time under representative load before deciding on production use.
