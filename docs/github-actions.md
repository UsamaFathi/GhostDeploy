# Deploy from GitHub Actions

Install GhostDeploy on the host and finish a local dry run using the [host setup guide](installation.md). Create a dedicated key per deployment environment for the GitHub runner to reach the unprivileged Odoo service account. Use the key solely for the generated workflow and restrict it at the server. Prefer one service account per environment. Create a separate read-only GitHub deploy key for the server if it must fetch a private repository.

```sh
ssh-keygen -t ed25519 -f ghostdeploy-staging -C ghostdeploy-staging
```

The runner uses non-interactive SSH, so install a CI-only key that does not require a passphrase. Store the public key on the host with this prefix in the service account's `authorized_keys`. Use a private `.ssh` directory and restrictive file permissions, or an administrator-managed authorized keys file. Replace the key material after `ssh-ed25519` with the generated public key.

```text
restrict,command="/opt/ghostdeploy/venv/bin/ghostdeploy-ssh --config /etc/ghostdeploy.yml" ssh-ed25519 PUBLIC_KEY ghostdeploy-staging
```

The entry point accepts only a structured deploy request for the configured environment. This key grants the ability to deploy branch commits with the permissions of the Odoo account. Restrict and rotate it accordingly. If the account has other keys, audit those independently. A restricted workflow key does not require granting a root login, a general SSH shell to that key, or port or agent forwarding. Do not let SSH clients supply arbitrary environment variables to the account.

Obtain the VPS host public key or fingerprint over a trusted administrator channel. Compare it independently to the key collected for the server's DNS name. Add the verified entry, such as `staging.example.net ssh-ed25519 …`, to the GitHub environment's known-hosts secret. A keyscan alone does not authenticate a host. Also verify GitHub's host key in the Odoo service account's known-hosts file for an SSH Git origin. See [GitHub's published fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).

| GitHub environment secret | Value |
| --- | --- |
| `SSH_HOST` | DNS name or IPv4 address used in the verified entry. SSH uses port 22. |
| `SSH_USER` | The unprivileged Odoo service user. |
| `SSH_PRIVATE_KEY` | Private half of the workflow-only key. |
| `SSH_KNOWN_HOSTS` | Independently verified host key entry. |

Before enabling the workflow, test SSH with `ssh -T -o StrictHostKeyChecking=yes -o UserKnownHostsFile=./staging_known_hosts -i ./ghostdeploy-staging odoo@staging.example.net probe`. Expect the GhostDeploy entry point to reject `probe` with a nonzero status. For the server's Git credential, verify access to the configured `origin` branch separately. There are no deployment changes during the connectivity probe.

Generate the workflow in the **addons repository**. Create a GitHub environment matching the configuration, restrict it to the intended deployment branch, and add the environment secrets. Configure required reviewers where supported, especially for production. Production templates use `workflow_dispatch` only. Development and staging templates also use pushes to the configured branch. The workflow uses the triggering SHA and evaluates branch eligibility again at the job boundary. It requests no GitHub token permissions and does not run a checkout action. [GitHub deployment environments](https://docs.github.com/en/actions/concepts/workflows-and-actions/deployment-environments) · [Workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)

Run the first deployment manually from the Actions tab and select the configured branch. Review that exact SHA, supply the approved modules, and check `approve_review` if the plan requires it. The SSH entry point re-fetches under the server-side lock and rejects queued commits that are no longer the branch tip. GitHub concurrency can skip pending work during rapid pushes. Review and dispatch the latest tip when needed. A failed host journal blocks a retry regardless of GitHub's concurrency result. Keep `manual_review` until a disposable staging exercise validates the `installed` policy.
