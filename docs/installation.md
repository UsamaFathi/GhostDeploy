# Install on an Ubuntu Odoo host

Use a disposable Ubuntu VM first. Start with an existing, working Odoo 18 or 19 installation and a PostgreSQL server. Install Python 3.11 or later with venv support, Git, sudo, OpenSSH and the PostgreSQL client tools. Prefer client tools from the same PostgreSQL major version as the server. Verify the checkout is a full Git clone and on the configured branch. Give the Odoo service account ownership of the addons checkout and keep GhostDeploy's install, configuration, data and state paths outside it.

Prefer a wheel built from a reviewed full Git SHA. This keeps the chosen GhostDeploy source fixed and gives you an artifact you can hash before transfer. Python dependencies resolve within the declared version ranges. Use your own vetted constraints or wheelhouse if you need to pin those too. An editable install is useful on your workstation. Use a dedicated admin-owned virtual environment on the server so the forced SSH command has a stable absolute path. [Workstation build commands](quick-start.md#1-on-your-workstation)

```sh
sudo python3 -m venv /opt/ghostdeploy/venv
sudo /opt/ghostdeploy/venv/bin/python -m pip install /root/ghostdeploy-0.3.0.dev0-py3-none-any.whl
sudo install -d -m 0700 -o odoo -g odoo /var/lib/ghostdeploy /var/backups/ghostdeploy
sudo install -m 0640 -o root -g odoo ghostdeploy.yml /etc/ghostdeploy.yml
sudo -H -u odoo /opt/ghostdeploy/venv/bin/ghostdeploy doctor --config /etc/ghostdeploy.yml
```

Use a trusted install directory and an administrator-verified copy of the wheel. Keep the venv and systemd unit writable only by administrators. The Odoo account must be able to read GhostDeploy and Odoo configuration. Protect the Odoo config separately because it may contain a database password.

The Odoo `[options]` file must set `data_dir` to an existing absolute directory and include the configured custom paths in `addons_path`. Set one explicit `db_host` and `db_port` so the client tools and Odoo use the same endpoint. For a local socket, use `db_host = /var/run/postgresql` and `db_port = 5432` if those match your cluster. Use PostgreSQL peer authentication when appropriate, or keep authentication in the Odoo config or in the service account's private `.pgpass`. For TCP PostgreSQL, configure TLS appropriate to your network. The database role must own or have migration access to each allowlisted Odoo database and have `NOSUPERUSER` and `NOCREATEDB`. Avoid scheduling workers, Odoo shells, jobs or any other writers outside the managed service during the maintenance window.

The supported systemd start command launches the configured Odoo runtime directly with `-c CONFIG` and a dedicated, static Unix account. Put normal options in the Odoo config file. If using a Python venv, explicitly configure its interpreter in GhostDeploy. The running process must use the same account home and connection settings as the upgrade command. Keep `PYTHONDONTWRITEBYTECODE=1` in the unit and remove any preexisting compiled bytecode from the deployment clone before using it. List any extra Odoo addon roots as absolute paths in `addons_path`, including packaged builtin roots if they are installed away from the Odoo executable's source tree. Avoid duplicate module names across all addon paths. A unit's core settings may look like this:

```ini
[Service]
Type=simple
User=odoo
WorkingDirectory=/opt/odoo/server
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/odoo/venv/bin/python /opt/odoo/server/odoo-bin -c /etc/odoo19.conf
KillMode=control-group
SendSIGKILL=yes
```

Use `visudo` to grant the actual service user permission to stop and start only the configured unit. The command paths and unit arguments must be exact. Keep the unit and any root-run scripts under admin control.

Use the canonical unit name. The static service account needs a trusted, writable home for the persistent state-directory binding. Use the same account, home and state directory for forced SSH and local deployments. Keep PostgreSQL, Odoo and Python import overrides out of the service environment, and avoid editing the Odoo configuration or deployment checkout during a run.

```sudoers
odoo ALL=(root) NOPASSWD: /usr/bin/systemctl stop odoo19.service, /usr/bin/systemctl start odoo19.service
```

Check the unit and endpoint with `doctor`, then verify the exact branch and both plan outputs. See [first deployment](first-deployment.md) for the staging sequence.

## Upgrade GhostDeploy

Pause workflow triggers and complete or reconcile any pending deployment first. Build and verify a new wheel using the same reviewed-commit process, then install it into the existing venv. Keep the bound state directory, service identity, backup store and SSH entry point stable.

```sh
sudo /opt/ghostdeploy/venv/bin/python -m pip install --upgrade "$REVIEWED_WHEEL"
/opt/ghostdeploy/venv/bin/ghostdeploy version
sudo -H -u odoo /opt/ghostdeploy/venv/bin/ghostdeploy doctor --config /etc/ghostdeploy.yml
```

For an existing production Actions workflow, regenerate or review the dispatch step to adopt the new confirmation input. This input is enforced by the generated workflow. It does not change the host's forced-command protocol.

## Uninstall GhostDeploy

Pause deployment workflows, remove the dedicated key from the host's authorized keys, and revoke obsolete environment secrets before uninstalling. Remove only the GhostDeploy sudo rule if no other process uses it. Reconcile pending state and retain backups and journals under your retention policy.

```sh
sudo /opt/ghostdeploy/venv/bin/python -m pip uninstall ghostdeploy
```

This command uninstalls the package from its venv. Odoo, PostgreSQL, your checkouts, configuration and recovery data remain separate.
