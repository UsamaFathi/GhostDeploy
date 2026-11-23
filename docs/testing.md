# Testing and release readiness

## Support and verification

| Component | Scope | Evidence for 0.3.0.dev0 |
| --- | --- | --- |
| Ubuntu | Target platform for execution | Existing baseline Ubuntu CI and Phase 2 Linux CI configuration. New Linux-only tests still need to run. |
| Odoo 18.0 | Targeted | Upstream CLI and version metadata reviewed. Disposable Odoo CLI fixture supplied, execution pending. |
| Odoo 19.0 | Targeted | Upstream CLI and version metadata reviewed. Disposable Odoo CLI fixture supplied, execution pending. |
| PostgreSQL | Supported by native clients | PostgreSQL 17.12 on Windows: real snapshot query, dump, archive decode and restore into a separate synthetic database. |
| Systemd | Targeted | Lifecycle, process-status and failure behavior exercised with fixtures. A real Ubuntu systemd host remains pending. |
| Multi-database | Implemented | Three-database planning and partial failure exercised with simulated services and Git fixtures. Real Odoo upgrade tests pending. |
| GitHub Actions and SSH | Implemented | Workflow syntax and Bash SSH stub tested, including dispatch confirmation and SSH exit propagation. A restricted SSH key and pinned host on a real staging instance remain pending. |

Ubuntu is the deployment target. Windows supports development and offline planning. Container tests exercise the stock Odoo executable and PostgreSQL without validating host systemd, sudo, SSH or an Odoo restore.

## Hermetic suite

Install from the checkout and run:

```sh
python -m pip install -e '.[dev]'
python -m pytest
ruff check .
ruff format --check .
ghostdeploy --help
ghostdeploy version
ghostdeploy validate --config examples/ghostdeploy.yml --output json
python -m build
python -m twine check --strict dist/*
```

No test contacts a Git remote or real database unless you opt in to the disposable integration suite. Linux tests exercise `flock`, the namespace lock across separate processes, and the state-directory binding. Git and Bash fixtures use local temporary files. Run `actionlint` if installed to check workflows.

## Disposable PostgreSQL restore

Put `initdb`, `pg_ctl`, `psql`, `pg_dump` and `pg_restore` from the same PostgreSQL installation on PATH. On Linux, run the suite as an ordinary user. No existing database URI or credentials are required.

```sh
GHOSTDEPLOY_TEST_POSTGRESQL=1 python -m pytest tests/integration -v
```

On Windows, if PostgreSQL's restricted token cannot read the default user temp directory, pass a fresh path in an accessible local workspace. Use a new path each run because pytest owns the path it receives.

```powershell
$env:GHOSTDEPLOY_TEST_POSTGRESQL = '1'
New-Item -ItemType Directory -Force .venv | Out-Null
$testRoot = Join-Path (Get-Location) ('.venv/pg-fixture-' + [guid]::NewGuid().ToString('N'))
python -m pytest tests/integration -v --basetemp $testRoot
Remove-Item Env:GHOSTDEPLOY_TEST_POSTGRESQL
```

The fixture starts a fresh password-protected cluster bound to loopback. It verifies the actual data directory before issuing SQL, creates uniquely named source and destination databases, restores only into the empty destination and stops only the cluster started from its own temporary directory. It asserts database rows and module metadata survived restore, and checks archived Unicode and nested attachment bytes. This is a PostgreSQL integration test. Application recovery still requires restoring and exercising a matched Odoo database and filestore. [PostgreSQL `initdb`](https://www.postgresql.org/docs/17/app-initdb.html) · [PostgreSQL `pg_ctl`](https://www.postgresql.org/docs/17/app-pgctl.html)

## Disposable Odoo CLI integration

Use Docker Engine or Docker Desktop with the Compose plugin and a local daemon. Run each stock image fixture separately:

```sh
python tests/odoo/run.py 18.0
python tests/odoo/run.py 19.0
```

The harness uses a unique Compose project with an internal network, no published ports and its own database volume. It creates three synthetic databases, installs the small fixture addon on two, updates them with the actual GhostDeploy Odoo command, then checks the changed application data, reported versions and untouched third database. Cleanup targets only the newly created project. Initial image and package downloads can use the internet. The same suites are available individually with the manual **Disposable integration** workflow after the branch is published. [Official Odoo images](https://github.com/odoo/docker)

## Release gate

| Gate | Status |
| --- | --- |
| Hermetic tests, security regressions, Ruff and CLI smoke | Passed locally on Windows. See the [Phase 3 test counts](phase-3-report.md#verification-status). |
| First-time setup walkthrough and docs checks | Exercised through CLI fixtures for Odoo 18 and 19, including real temporary Git clones and a controlled host simulation. A fresh Ubuntu onboarding run remains pending. |
| Build and verify wheel and sdist | Passed locally. |
| PostgreSQL dump and restore into a disposable database | Passed locally with PostgreSQL 17.12. |
| Multi-database partial failure | Passed with fixtures. Actual shared-service failure exercise pending. |
| Linux file and service-level lock | Tests added. Execution pending on Linux. |
| Odoo 18 upgrade | Pending Docker or disposable Ubuntu. |
| Odoo 19 upgrade | Pending Docker or disposable Ubuntu. |
| Real systemd stop/start, sudo scope, process identity and health | Pending disposable Ubuntu host. |
| GitHub Actions SSH to a restricted staging user | Pending an authorized disposable staging host. |
| Recovery drill including functional login and attachment download from the restored Odoo instance | Pending disposable staging. |

See the [staging procedure](operations.md#staging-validation-before-production) for the remaining service and application checks.

For a public Beta, complete the pending staging gates, including a failure on the second upgrade and a functional restore with matching filestore. Verify the documented workstation, host and GitHub steps end to end on disposable infrastructure. Record both runtime versions and the recovered state before publishing anything.
