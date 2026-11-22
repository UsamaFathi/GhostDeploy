# Install on an Ubuntu Odoo host

Use a disposable Ubuntu VM first. Start with an existing, working Odoo 18 or 19 installation and a PostgreSQL server. Install Python 3.11 or later with venv support, Git, sudo, OpenSSH and the PostgreSQL client tools. Prefer client tools from the same PostgreSQL major version as the server. Verify the checkout is a full Git clone and on the configured branch. Give the Odoo service account ownership of the addons checkout and keep GhostDeploy's install, configuration, data and state paths outside it.

Build and review a wheel using `python -m build`. On the host, install that wheel and its dependencies in an admin-owned venv such as `/opt/ghostdeploy/venv`. Keep the venv and systemd unit writable only by administrators. Config read permissions must allow the Odoo account to read both files. Restrict Odoo configuration as it may contain the database password.

```sh
sudo install -d -m 0700 -o odoo -g odoo /var/lib/ghostdeploy /var/backups/ghostdeploy
sudo install -m 0640 -o root -g odoo ghostdeploy.yml /etc/ghostdeploy.yml
sudo -H -u odoo /opt/ghostdeploy/venv/bin/ghostdeploy doctor --config /etc/ghostdeploy.yml
```

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

```sudoers
odoo ALL=(root) NOPASSWD: /usr/bin/systemctl stop odoo19.service, /usr/bin/systemctl start odoo19.service
```

Check the unit and endpoint with `doctor`, then verify the exact branch and both plan outputs before compiling an execution command. See [operations](operations.md) for the staging validation sequence.
