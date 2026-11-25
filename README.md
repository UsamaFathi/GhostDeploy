# GhostDeploy

GhostDeploy automates reviewed custom addon deployments to self-hosted Odoo. It verifies the target Git revision, backs up the affected databases and filestores, upgrades selected modules, restarts Odoo and records the outcome. GhostDeploy is [MIT-licensed](LICENSE) and runs on infrastructure you control, without a GhostDeploy server or subscription.

**0.3.0a1 · Public Alpha · Experimental, staging evaluation only.** Use a dedicated disposable host and synthetic data. Do not use this Alpha in production. Independent external SSH deployment remains **PENDING**. [Release notes](docs/releases/0.3.0a1.md)

## Supported setup

GhostDeploy targets Ubuntu with Python 3.11+, Odoo 18.0 or 19.0, PostgreSQL, a dedicated systemd service and a full Git checkout of custom addons. Deploy locally from the host or use the generated GitHub Actions workflow with a pinned SSH host key and a forced command for the dedicated deployment identity. [Architecture and trust boundaries](docs/architecture.md) · [GitHub Actions setup](docs/github-actions.md)

## Evaluate it on disposable staging

Install the verified wheel in an isolated environment. Obtain it from the reviewed candidate or build it from a reviewed commit as shown in the Quick Start. Compare its SHA-256 against the maintainer's manifest before installing. Use the administrator-owned environment in the installation guide for systemd and SSH setup.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./ghostdeploy-0.3.0a1-py3-none-any.whl
ghostdeploy version
ghostdeploy validate --config examples/staging-odoo19.yml
```

This staging configuration uses two explicitly selected synthetic databases. Set the paths to match your disposable VM. Keep the host copy under administrator control. [Quick Start](docs/quick-start.md) · [Install, upgrade and uninstall](docs/installation.md) · [Configuration reference](docs/reference.md)

```yaml
schema_version: 1
project:
  name: odoo-training
odoo:
  version: "19.0"
  service: odoo19
  service_user: odoo
  executable: /opt/odoo/server/odoo-bin
  interpreter: /opt/odoo/venv/bin/python
  config_file: /etc/odoo19.conf
  addons_path: [/srv/odoo/custom-addons/addons]
repository:
  branch: main
  checkout_path: /srv/odoo/custom-addons
deployment:
  environment: staging
  databases: [training_east, training_west]
  state_path: /var/lib/ghostdeploy
  backup:
    enabled: true
    path: /var/backups/ghostdeploy
```

1. Run `ghostdeploy init` to create a configuration, then `validate` and `doctor` to check it. Run host checks as the dedicated Odoo account.
2. Run `ghostdeploy plan --inspect-databases --target-sha "$TARGET"` with the full reviewed commit SHA. Review candidate modules and the explicit allowlist before scheduling the restart.
3. Run `ghostdeploy workflow setup` for restricted SSH and environment approval, and `ghostdeploy workflow generate --manual-only` to generate a manual workflow. Follow the staging tutorial for an authorized execution and for interpreting the journal. [First deployment tutorial](docs/first-deployment.md)

## Deployment safeguards

- **Commit and upgrade scope.** GhostDeploy verifies the exact commit and a clean checkout, refuses unsafe Git changes and restricts upgrades to approved candidates on the configured database allowlist.
- **Shared service.** Stopping or restarting Odoo interrupts every database served by that unit, including databases outside the allowlist. Shared addon code can affect those databases. Review compatibility across the instance before deploying. [Multi-database guide](docs/multi-database.md)
- **Backup and recovery.** GhostDeploy checks storage, stops Odoo, verifies PostgreSQL and filestore backup artifacts before upgrading, and records progress in a durable journal. A partial failure blocks retries until operator reconciliation. Restoration and rollback are manual. [Recovery guide](docs/operations.md)
- **Access and trust.** Use a reviewed, protected branch, administrator-controlled configuration, scoped sudo and restricted SSH. Approved addon code executes as the Odoo service account. [Security guidance](SECURITY.md)

## Verification and limits

Hosted Ubuntu integration exercised Odoo 18 and 19 with real PostgreSQL, systemd, multi-database upgrades, intentional partial failure and authenticated browser recovery. A separate disposable OpenSSH test exercised the restricted gateway over loopback. The [verification matrix](docs/testing.md#support-and-verification) and [release notes](docs/releases/0.3.0a1.md) link to exact tested commits and runs.

The Alpha targets one direct systemd service and a full Git checkout. Symlinks, submodules and risky addon removals or dependency changes can block execution. The optional HTTP probe checks reachability. Verify Odoo and your application after deployment. [Operations and limits](docs/operations.md)

## Contribute and test

Use the [contributor guide](CONTRIBUTING.md) for local tests and reviews. Independent Odoo developers can use the [Alpha staging kit](validation/staging/README.md) and submit sanitized evidence with the [Alpha validation issue form](https://github.com/UsamaFathi/GhostDeploy/issues/new?template=alpha-validation.yml). Report suspected vulnerabilities through GitHub's private reporting feature if enabled, or contact the maintainers privately. [Security reporting](SECURITY.md)
