# Quick start

This walkthrough uses an existing Odoo 19 installation on a disposable Ubuntu VM. The two synthetic databases are `training_east` and `training_west`. The [sample YAML](../examples/staging-odoo19.yml) shows a checkout with two custom addon roots. Replace the paths and database names with ones you control. All deployments run on your infrastructure.

## 1. On your workstation

Clone GhostDeploy and build the wheel from a reviewed commit. Use the full SHA of the development revision you chose to evaluate.

```sh
git clone https://github.com/UsamaFathi/GhostDeploy.git
cd GhostDeploy
REVIEWED_COMMIT='<full reviewed GhostDeploy commit SHA>'
git switch --detach "$REVIEWED_COMMIT"
test "$(git rev-parse HEAD)" = "$REVIEWED_COMMIT"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
ghostdeploy version
ghostdeploy validate --config examples/staging-odoo19.yml
python -m build
python -m twine check --strict dist/*
sha256sum dist/*.whl
```

Copy the reviewed wheel to your disposable Odoo host and place it in an administrator-owned directory. Compare the SHA-256 of that copy with your workstation's value before installing it. If you are only evaluating plans, pass `--local-checkout` to the plan command in the [first deployment tutorial](first-deployment.md#preview-the-change).

## 2. On the Odoo VPS or disposable Ubuntu VM

Use a dedicated addons checkout and install the verified wheel in the admin-owned venv. These commands assume the existing service account and group are both called `odoo`.

```sh
sudo python3 -m venv /opt/ghostdeploy/venv
sudo /opt/ghostdeploy/venv/bin/python -m pip install /root/ghostdeploy-0.3.0.dev0-py3-none-any.whl
sudo install -d -o odoo -g odoo -m 0700 /var/lib/ghostdeploy /var/backups/ghostdeploy
sudo -H -u odoo /opt/ghostdeploy/venv/bin/ghostdeploy init --config /var/lib/odoo/ghostdeploy.yml
sudo install -o root -g odoo -m 0640 /var/lib/odoo/ghostdeploy.yml /etc/ghostdeploy.yml
sudo -H -u odoo /opt/ghostdeploy/venv/bin/ghostdeploy validate --config /etc/ghostdeploy.yml
sudo -H -u odoo /opt/ghostdeploy/venv/bin/ghostdeploy doctor --config /etc/ghostdeploy.yml
```

The wizard asks you to select a unit, review suggested paths and enter `training_east,training_west` explicitly. Check every warning and failure in doctor. Configure the exact systemd permissions and PostgreSQL role described in the [installation guide](installation.md). Doctor checks your setup without authorizing a deployment. For a scripted setup, use `init --non-interactive --databases training_east,training_west` together with the host-specific flags in `init --help`.

Follow the [first deployment tutorial](first-deployment.md) to inspect the target commit and run a controlled deployment on the disposable VM. A restart may temporarily take every database on that Odoo service offline, including databases outside the upgrade allowlist.

## 3. On GitHub

This step is optional for local host CLI deployments. From the workstation, run these commands **in your addons repository** with its reviewed config or the sample adjusted to match the host.

```sh
ghostdeploy workflow setup --config /path/to/ghostdeploy.yml
ghostdeploy workflow generate --config /path/to/ghostdeploy.yml --manual-only --path .github/workflows/ghostdeploy-staging.yml
```

Commit the generated YAML to the addons repo only after reviewing it. In GitHub, configure the `staging` environment and the four secrets listed by `workflow setup`. Use a dedicated key with the restricted forced-command example, and pin the host key from a trusted source. Run the workflow manually from the configured branch for your first disposable staging test. See [GitHub Actions setup](github-actions.md) for exact secret names and review rules.
