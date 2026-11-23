# Testing and release readiness

## Support and verification

| Component | Scope | Evidence for 0.3.0.dev0 |
| --- | --- | --- |
| Ubuntu | Target platform for execution | Linux lock and journal binding regressions passed on Ubuntu CI. |
| Odoo 18.0 | Targeted | Real executor upgrade, intentional failure and functional restore passed against `18.0-20261112` in Docker. |
| Odoo 19.0 | Targeted | Real executor upgrade, intentional failure and functional restore passed against `19.0-20261112` in Docker. |
| PostgreSQL | Supported by native clients | PostgreSQL 17.12 on Windows: real snapshot query, dump, archive decode and restore into a separate synthetic database. |
| Systemd | Targeted | Lifecycle, process-status and failure behavior exercised with fixtures. A real Ubuntu systemd host remains pending. |
| Multi-database | Implemented | Verified with Git fixtures and real Odoo on three explicitly allowlisted databases, including intentional failure of the second upgrade. |
| GitHub Actions and SSH | Implemented | Workflow syntax, Bash stub and disposable OpenSSH passed, including forced-command and host-key checks. Deployment from GitHub to a real staging instance remains pending. |

Ubuntu is the deployment target. Windows supports development and offline planning. See the [Phase 3.5 integration report](phase-35-integration-report.md) for the verified commit, independent gates and exact evidence.

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

## Disposable Odoo and SSH integration

Use Docker Engine or Docker Desktop with the Compose plugin and a local daemon. Run each stock image fixture separately:

```sh
python tests/odoo/run.py 18.0
python tests/odoo/run.py 19.0
```

The harness verifies a fresh PostgreSQL cluster by its unique marker before making changes. A unique Compose project owns its internal network, databases and volumes. The two scenarios each use three databases and a local two-commit Git checkout. The real executor stops and starts a private Odoo child process, backs up and upgrades only the first two databases, and exercises an intentional failure on the second. A separate Odoo instance checks a restored ORM record and attachment at the base revision. Real systemd and sudo are outside this container test. Initial builds require image and package downloads. [Official Odoo images](https://github.com/odoo/docker)

The SSH suite uses a separate image and loopback OpenSSH with ephemeral keys. It exercises the generated command, forced key restrictions, host-key rejection and exit propagation. The gateway substitutes a fixture executor, and GitHub reviewer rules and live Ubuntu integration remain separate.

```sh
image="ghostdeploy-ssh-fixture:$(python -c 'import uuid; print(uuid.uuid4().hex)')"
docker context inspect
docker build --file tests/ssh/Dockerfile --tag "$image" .
docker run --rm --network none "$image"
docker image rm "$image"
```

Use a verified local Docker daemon. These image builds and database installs may take several minutes and require local image and volume storage. You can also dispatch the existing **Disposable integration** workflow with `postgresql`, `odoo18`, `odoo19` or `ssh`. Hosted runs use standard Ubuntu runners. Check the repository's eligibility and current account limits before dispatching. [GitHub Actions billing and usage](https://docs.github.com/en/actions/concepts/billing-and-usage)

## Release gate

| Gate | Status |
| --- | --- |
| Hermetic tests, security regressions, Ruff and CLI smoke | Passed locally on Windows and the ordinary test suite passed on hosted Ubuntu. See [recorded evidence](phase-35-integration-report.md#recorded-evidence). |
| First-time setup walkthrough and docs checks | Exercised through CLI fixtures for Odoo 18 and 19, including real temporary Git clones and a controlled host simulation. A fresh Ubuntu onboarding run remains pending. |
| Build and verify wheel and sdist | Passed locally. |
| PostgreSQL dump and restore into a disposable database | Passed locally with PostgreSQL 17.12. |
| Multi-database partial failure | Passed in both Odoo images. Real systemd recovery after failure remains pending. |
| Linux file and service-level lock | Passed on Ubuntu CI. |
| Odoo 18 upgrade | Passed with real Odoo and the executor in Docker. |
| Odoo 19 upgrade | Passed with real Odoo and the executor in Docker. |
| Real systemd stop/start, sudo scope, process identity and health | Pending disposable Ubuntu host. |
| Restricted command on a real OpenSSH server | Passed against disposable loopback OpenSSH. |
| GitHub Actions SSH to a restricted staging user | Pending an authorized disposable staging host. |
| Functional Odoo restore | ORM read and attachment bytes passed on both Odoo series. Authenticated browser login and download remain pending in disposable staging. |

See the [staging procedure](operations.md#staging-validation-before-production) for the remaining service and application checks.

For a public Beta, complete the pending staging gates, including a failure on the second upgrade and a functional restore with matching filestore. Verify the documented workstation, host and GitHub steps end to end on disposable infrastructure. Record both runtime versions and the recovered state before publishing anything.
