# First deployment on disposable staging

Start with the [Quick start](quick-start.md) and a working Odoo 19 installation on an isolated Ubuntu VM. Use only synthetic databases and attachments. In this example, `attendance` is installed on `training_east`. `training_west` shares the same service but has no `attendance` module.

## Preview the change

On your workstation, use a full addons clone with both commits available. Set `BASE` to the host checkout's current full SHA and `TARGET` to the reviewed full SHA.

```sh
ghostdeploy plan --config /path/to/ghostdeploy.yml --local-checkout /path/to/addons \
  --base-sha "$BASE" --target-sha "$TARGET" --output json
```

On the Ubuntu host, fetch the reviewed branch using the normal Git credentials for the addons checkout. Confirm your target from the output on the host, then keep it fixed while reviewing the plan.

```sh
sudo -H -u odoo git -C /srv/odoo/custom-addons fetch origin main
sudo -H -u odoo /opt/ghostdeploy/venv/bin/ghostdeploy plan \
  --config /etc/ghostdeploy.yml --target-sha "$TARGET" --inspect-databases
sudo -H -u odoo /opt/ghostdeploy/venv/bin/ghostdeploy deploy \
  --config /etc/ghostdeploy.yml --dry-run --target-sha "$TARGET" --inspect-databases
```

Review the changed addons, installed candidates, required approvals, backup path and operation order. Resolve every blocker. Code changes that require a restart affect both databases even though the example upgrades only `training_east`. Arrange a maintenance window and pause any independent writers.

## Execute and inspect

Only on disposable staging, use the exact SHA you reviewed. Approve every installed upgrade candidate reported by the plan. Add `--approve-review` only if you reviewed the displayed review items.

```sh
sudo -H -u odoo /opt/ghostdeploy/venv/bin/ghostdeploy deploy \
  --config /etc/ghostdeploy.yml --execute --target-sha "$TARGET" --upgrade attendance --output json
```

The report identifies the deployment, reached checkpoints, verified database upgrades, artifacts and any uncertain database. Availability checks cover the systemd process and optional HTTP response. Use the browser or application tools to check login, the changed feature and an attachment after startup. Consult the service log through your normal administrator access:

```sh
sudo journalctl -u odoo19.service -n 100 --no-pager
```

On failure, preserve the pending journal and matching backups. Use the [recovery decisions](operations.md#recovery-decisions) before retrying or starting the service. Complete the [release readiness gates](testing.md#release-gate) before relying on GhostDeploy for a real installation.

If you are evaluating the deployment path, use the [disposable fixture](testing.md#disposable-odoo-and-ssh-integration) to rehearse a failed second-database upgrade and application restore first. The [systemd procedure](systemd-staging.md) then checks privileges and lifecycle on actual Ubuntu systemd.
