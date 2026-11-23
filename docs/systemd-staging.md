# Disposable Ubuntu systemd gate

The manual `systemd` suite exercises the real service adapter and sudo policy with a synthetic unit on hosted Ubuntu. Use the procedure below for the combined Odoo, PostgreSQL and systemd gate. [Phase 3.6 validation](phase-36-beta-validation.md)

Use an isolated Ubuntu VM booted with systemd. Reserve the VM, PostgreSQL cluster, test Unix account, Git checkout and two or three synthetic databases solely for this exercise. Follow the [installation guide](installation.md) and [first deployment tutorial](first-deployment.md). Use a dedicated canonical unit such as `ghostdeploy-e2e.service`. Keep the unit's direct Odoo command, the configured interpreter and the install guide's limited sudo permissions. Audit the VM identity before running any host-changing command.

## Observe a successful deployment

Use two reviewed Git revisions of a synthetic addon that updates an ORM record. Include an attachment and install the addon in two databases only. Record the current revision and target revision, then run `plan --inspect-databases` under the service account. Resolve every blocker. Confirm the second and third database expectations before execution.

```sh
systemctl show ghostdeploy-e2e.service -p Id -p User -p ActiveState -p MainPID
sudo -H -u odoo /opt/ghostdeploy/venv/bin/ghostdeploy plan \
  --config /etc/ghostdeploy-e2e.yml --target-sha "$TARGET" --inspect-databases --output json
```

Authorize the exact SHA and module under `deploy --execute`. Record the active PID before execution. In a second console, follow `journalctl -u ghostdeploy-e2e.service` and record the stop, inactive interval and new main PID. Cross-check the UTC interval against the GhostDeploy report and backup metadata. The old process must be gone before backup and the new one must run as the configured account. Check the application record, a login and attachment access across the relevant databases after startup.

## Rehearse failure

Use a fresh fixture for each row. Preserve the pending journal and every backup. Reconcile the expected baseline before the next run.

| Case | Test setup | Evidence to record |
| --- | --- | --- |
| Failed start | In the disposable unit only, add an admin-owned `ExecStartPre` readiness check that fails while a dedicated test marker exists. Activate the marker after the initial healthy start. | The post-upgrade start fails, CLI exits nonzero, and the journal records the attempted start. Inspect the unit and its cgroup even if systemd reports no main PID. |
| Failed health | Use a synthetic test addon whose loopback probe responds successfully on the base revision and returns `503` on the target. | The executor stops the newly launched process, records `checking_health`, and blocks another deployment. |
| Interrupted backup or migration | Interrupt the deployment process at the selected checkpoint while logged into the VM. Identify the fixture PID explicitly. | The journal remains, the phase and database are recoverable, and a second deployment is blocked. Inspect any surviving systemd transaction independently. |
| Second database upgrade fails | Follow the container fixture's approach with a migration that fails only on B. | A is verified, B remains uncertain, C was never upgraded, and the canonical service is inactive pending reconciliation. |

Finish by restoring the pre-deployment database archive and matching filestore into a new isolated Odoo instance at the recorded base SHA. Confirm the original record and attachment bytes through the application. The test should retain the exact target SHA, Odoo version, SQL and attachment outcomes, report and relevant unit log timestamps. A health response or a container-managed process alone does not validate systemd, sudo privileges or production compatibility.
