# Independent Alpha staging validation

Use this kit if you administer an explicitly authorized disposable Ubuntu VM, can reach it from an Actions runner over SSH, and can dispose of the entire environment. Use Odoo 18 or 19, a dedicated systemd unit and unprivileged account, a fresh local PostgreSQL cluster and a dedicated repository of synthetic addons. You need authority to configure the repository environment, SSH policy and service permissions. Finish the success and failure cases on separate fresh VM states or reset a dedicated VM to its seeded base snapshot between cases.

## Prepare the fixture

These steps change the disposable environment. Build a wheel from a reviewed commit and install it in the root-managed venv described in the [installation guide](../../docs/installation.md). Copy `staging.py` to the root-owned `/opt/ghostdeploy/staging.py` and the reviewed [example configuration](configuration.example.yml) to `/etc/ghostdeploy-alpha/ghostdeploy.yml`. Use the actual Odoo series in the config. Keep GhostDeploy's YAML, Odoo's separate config and the kit readable by the service user and writable only by root, with root-controlled parent directories. Create the state and backup directories as the service account with mode 0700.

Initialize only `ghostdeploy_alpha_a`, `ghostdeploy_alpha_b`, and `ghostdeploy_alpha_c` on the new local cluster. In Odoo's config set the exact comma-separated `db_name` list, `list_db = False`, `http_interface = 127.0.0.1`, and `db_host = 127.0.0.1` with an explicit port. Keep PostgreSQL bound to loopback. Run Odoo using the documented direct systemd command, the dedicated user and `PYTHONDONTWRITEBYTECODE=1`. Give the account sudo permission for only start and stop of the canonical test unit. A service restart interrupts all three databases during the test.

Reuse the fixture addon that passed on both Odoo series. From a reviewed GhostDeploy source checkout with GhostDeploy installed, run this on the operator workstation. `$ADDONS` is the separate test repository worktree whose root will be the configured addon path. Set `$SERIES` to `18.0` or `19.0`.

```sh
PYTHONPATH=tests/odoo python -c 'from pathlib import Path; import sys; from check_upgrade import write_addon; write_addon(Path(sys.argv[1]), 0, sys.argv[2], fail_second=False)' "$ADDONS" "$SERIES"
```

Commit the baseline addon together with the generated workflow described below. Clone that baseline on the staging host. Install `ghostdeploy_test_addon` in A and B only. Initialize C with the web module. On both A and B, create a private synthetic attachment linked to the fixture record using Odoo's shell as the Odoo service account with its reviewed executable and config. Save the original content hash locally.

```python
import base64

record = env.ref("ghostdeploy_test_addon.sample")
data = b"GhostDeploy synthetic alpha attachment"
env["ir.attachment"].create(
    {
        "name": "ghostdeploy-recovery.bin",
        "res_model": record._name,
        "res_id": record.id,
        "datas": base64.b64encode(data),
    }
)
env.cr.commit()
```

For the success candidate, repeat the fixture generation command with `minor=1` (change the second argument from `0` to `1`) and `fail_second=False`. For the intentional failure, start again from the seeded baseline on a separate fresh fixture and generate minor 1 with `fail_second=True`. That migration raises only on the synthetic database whose name ends in `_b`. Review both commits. Publish only the selected candidate as the tip of the configured dispatch branch for its own scenario. Retain the base and target SHAs and a local snapshot of C's installed module names and versions.

## Authorize the host and configure SSH

These are administrator changes. Confirm that the VM and repository are dedicated and that all data is synthetic. The command below prints a two-hour, host-bound authorization for root to install. Its hashes bind the machine and both configs. Treat this file as private validation material. Use a fresh authorization if it expires or a reviewed config changes.

```sh
sudo -i
umask 077
set -C
/opt/ghostdeploy/venv/bin/python -I /opt/ghostdeploy/staging.py \
  --config /etc/ghostdeploy-alpha/ghostdeploy.yml \
  --authorization /etc/ghostdeploy-alpha/authorization.json \
  authorize --confirm-disposable > /etc/ghostdeploy-alpha/authorization.json
chgrp ghostdeploy_alpha /etc/ghostdeploy-alpha/authorization.json
chmod 0640 /etc/ghostdeploy-alpha/authorization.json
exit
```

For renewal, archive the expired file under root control before creating its replacement. Use one dedicated Ed25519 keypair. As administrator, install its public key as the only entry in root-owned `/etc/ssh/authorized_keys/ghostdeploy_alpha`. Set the file to 0644. The forced command must match the preflight check.

```text
restrict,command="/opt/ghostdeploy/venv/bin/python -I /opt/ghostdeploy/staging.py --config /etc/ghostdeploy-alpha/ghostdeploy.yml --authorization /etc/ghostdeploy-alpha/authorization.json ssh-gate" ssh-ed25519 <PUBLIC_KEY_BODY> ghostdeploy-alpha
```

Configure the effective OpenSSH policy for that dedicated account. Set its `AuthorizedKeysFile` to `/etc/ssh/authorized_keys/%u`, enable public-key authentication and disable password, keyboard-interactive authentication, forwarding and PTY allocation for it. Ensure there are no other accepted key sources or force commands for the account and disable user-provided environment and shell startup files. Keep a usable, root-managed shell for OpenSSH's forced command. Validate the SSH configuration and inspect it as administrator with `sshd -T -C user=ghostdeploy_alpha,host=<client-host>,addr=<client-ip>` before reloading the disposable SSH service. Run the kit preflight to inspect the dedicated key file structure and sudo scope. Confirm the effective sshd policy separately since the preflight runs without root.

Use a trusted console to obtain the SSH host public key and fingerprint. Pin the key in the GitHub environment. Make only the SSH listener reachable from the selected runner under your approved network policy. Leave Odoo and PostgreSQL on loopback.

## Generate and run the real workflow

From the test addons worktree on your workstation, generate the manual workflow from a reviewed copy of the staging YAML using the regular CLI. Set `$REVIEWED_CONFIG` to the path of that copy. Commit the workflow to the **test addons repository** and ensure it exists on the default branch so it can be dispatched. Keep repository and environment secrets separate from the code.

```sh
ghostdeploy workflow generate --config "$REVIEWED_CONFIG" \
  --manual-only --path .github/workflows/ghostdeploy-staging.yml
```

Configure environment `staging` before dispatch and add its SSH endpoint, identity, dedicated private key and pinned known-host entry under the names in [workflow setup](../../docs/github-actions.md). Restrict deployment branches to the reviewed test branch. Configure an independent required reviewer when available, prevent self-review, and record whether approval was actually required. Environment branch controls and secrets depend on visibility and plan. On GitHub Free environments are available for public repositories. Private and internal repositories need an eligible plan for environment secrets and branch controls, and required reviewers on Free, Pro and Team are limited to public repositories. Check the repository's actual settings. [GitHub environment availability](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)

### Read-only checks before dispatch

Run the kit and the database-aware plan as the service account. Confirm the plan selects only the intended addon in A and B. If you refresh Git refs first, review it as a separate checkout metadata change. Verify the default branch and selected dispatch branch and record the exact Actions event SHA. The deploy gateway fetches the branch and checks the requested commit against the current tip. Arrange for a denied command, incorrect pin and disallowed branch test while the synthetic environment is isolated. None should cause a deployment.

```sh
kit() {
  sudo -H -u ghostdeploy_alpha /opt/ghostdeploy/venv/bin/python -I \
    /opt/ghostdeploy/staging.py --config /etc/ghostdeploy-alpha/ghostdeploy.yml \
    --authorization /etc/ghostdeploy-alpha/authorization.json "$@"
}
kit preflight
sudo -H -u ghostdeploy_alpha /opt/ghostdeploy/venv/bin/ghostdeploy plan \
  --config /etc/ghostdeploy-alpha/ghostdeploy.yml --target-sha "$TARGET" --inspect-databases --output json
```

### State-changing dispatch

Manually dispatch on the permitted branch. Supply `ghostdeploy_test_addon` and approve the reviewed plan items if required. Set `$TEST_REPOSITORY` and `$BRANCH` to the reviewed disposable repository and branch. Keep the branch tip fixed while the run is queued. Have the designated reviewer inspect the waiting job and record the approval and release of the job. Check that a second authorized dispatch on the same ref and environment waits while the first is active. Observe job timestamps and check the server journal for a single active deployment. The second dispatch may be a no-change completion once the first finishes. Treat canceled or skipped jobs as separate outcomes.

```sh
gh workflow run ghostdeploy-staging.yml --repo "$TEST_REPOSITORY" --ref "$BRANCH" \
  -f upgrade_modules=ghostdeploy_test_addon -f approve_review=true
```

Copy the deployment ID from the real remote JSON, keeping the original log private. On the success fixture, run `kit verify --deployment-id "$ID" --target-sha "$TARGET" --module ghostdeploy_test_addon --expect succeeded`. Expect a completed journal, a healthy service and two verified database and filestore backup sets. Confirm C's module state matches the pre-run snapshot.

On the failure fixture, use the same checks with `--expect failed`. Expect a failed Actions job, A verified, B uncertain, C unchanged, an inactive unit and a preserved pending journal. Re-dispatch the same approved SHA while the authorization is still valid. The retry must fail at the pending-journal gate. Record both run IDs separately. The verifier reads evidence and keeps the external gate pending. Only a completed independent report with actual run links and host evidence can close it.

## Restore, report and clean up

Recovery modifies a separate disposable environment. Follow [backup and recovery](../../docs/operations.md) to restore the matching dump and filestore to a new database and a new data directory at the recorded base commit. Verify the destination is empty before restore. Keep it local to the recovery environment. Sign in with a synthetic test account, check the original record, and compare the attachment's downloaded bytes to the saved hash. Recheck the failed source journal and service after the drill.

Submit the [sanitized report](report-template.md) through the repository's Alpha validation issue form. Public repositories can expose Actions logs and the remote report contains paths and database names. Use only synthetic names and paths in a public test repository. Follow [security reporting](../../SECURITY.md) for sensitive findings. Include workflow links only when their logs are suitable for the audience. Copy minimal, redacted error excerpts. Keep private raw logs, keys, authorization files, application configs, dumps, customer data and full hostnames out of the issue.

To clean up, disable the disposable repository's dispatches, revoke its dedicated environment secrets and remove the exact restricted public key. End active runs and verify the owned unit is inactive. Remove the authorization from the disposable host. Retain only the evidence allowed by your retention policy, then dispose of the dedicated VM and restore environment through your VM manager. The kit has no destructive cleanup command. The [uninstall procedure](../../docs/installation.md#uninstall-ghostdeploy) covers removing only the installed package.

A separate local VM with an isolated self-hosted runner is useful for rehearsing the workflow. Use a trusted, restricted test repository and a dedicated runner VM with no access to personal credentials, workstation files or production networks. Limit runner registration to the reviewed workflow, exclude untrusted pull requests, and remove runner registration and temporary credentials after testing. Record this as local rehearsal evidence. The independently reachable hosted-runner gate remains pending until tested. [GitHub guidance on self-hosted runner isolation](https://docs.github.com/en/actions/reference/security/secure-use)
