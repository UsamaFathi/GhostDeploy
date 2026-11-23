# Phase 3.5 integration report

GhostDeploy remains **Alpha** at `0.3.0.dev0`. The baseline was `dc7a6d3a824114ad1ecd1236daf57c07b5f05ff7`. Work is on `phase-35-real-odoo-e2e`. All fixture data, keys, databases and process state are disposable. No customer deployment or live VPS is involved.

## Verification inventory

| Layer | What it exercises |
| --- | --- |
| Existing PostgreSQL integration | Real connection and snapshot checks, `pg_dump`, `pg_restore`, and byte verification of archived synthetic attachments. It restores into a new database. |
| Original Odoo Docker fixture | Real Odoo 18 and 19 module updates through `Host.upgrade()`. Two databases receive the update and the third stays unchanged. This fixture does not call `Executor.execute()`. |
| Hermetic executor tests | Lock and journal orchestration with fake subprocesses, backed by separate real Git fixtures. Phase 3.5 adds interruption after synchronization and during process startup and health checks to the existing backup and upgrade interruption cases. |
| Phase 3.5 Odoo executor fixture | Calls the deploy CLI and invokes the real executor. Git, PostgreSQL, Odoo CLI, backup files and the journal are real. Its lifecycle adapter controls a private Odoo process and exercises the real HTTP probe. It does not call systemd or sudo. |
| Phase 3.5 functional restore | Uses a verified pre-deployment dump and matching filestore from the executor. Restores into a new database and independent Odoo process at the base SHA. Reads the original record and file through Odoo ORM. |
| Linux lock tests | Real contention over file and namespace locks, lock release after process death, and rejection of an alternate state binding on Linux. |
| SSH tests | Existing Bash stub covers command shape and exit propagation. Phase 3.5 adds real loopback OpenSSH, an authorized-keys restriction, key pinning, a production confirmation check, a valid SHA and an attempted unauthorized SHA. The SSH fixture substitutes the deployment effect. |
| Real systemd and hosted policy | The [Ubuntu systemd procedure](systemd-staging.md) requires a dedicated VM. A workflow connected to a real restricted deploy account and protected GitHub environment is a separate staging test. |

## Recorded evidence

The original Odoo suites ran on standard Ubuntu GitHub runners with Docker and `postgres:17`. Both exercised three synthetic databases and returned success. The resolved Odoo images and stage durations are in the original logs. [Odoo 18 direct upgrade](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45148121147) · [Odoo 19 direct upgrade](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45148122117)

The executor fixtures ran from `566fa20875a27c4d9afda4a7451d9c1c30c8c837` on GitHub-hosted Ubuntu with Docker, Python 3.12.3 in the Ubuntu 24.04 Odoo image and PostgreSQL 17.12. Each run used three allowlisted databases per scenario and a separate new database for the restore.

| Run | Odoo runtime | Successful executor and restore | Intentional B failure | Compose total |
| --- | --- | --- | --- | --- |
| [Odoo 18](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45149230411) | `18.0-20261112` | 63.7s, CLI exit 0 | 53.9s, CLI exit 1 | 198.3s, test and cleanup exit 0 |
| [Odoo 19](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45149230530) | `19.0-20261112` | 53.9s, CLI exit 0 | 46.1s, CLI exit 1 | 188.8s, test and cleanup exit 0 |

- `odoo:18.0` resolved to `sha256:0bd79cf07f59105c5697667c2241b86447e99c6adb036200df0e4e295785bdf3`.
- `odoo:19.0` resolved to `sha256:03a47921f9eef2c225ded1167fe7828465d2b3177883f30d173bb1c3c792949b`.

Both executor runs checked the approved target SHA, exact A and B module scope, backup hashes and permissions, the stop/backup/sync/upgrade/start ordering, updated ORM data and schema field, and an unchanged C. Both intentional failures left B uncertain, A verified and the service process inactive. The pending journal refused another deployment. The worktree remained at the attempted target, and the executor issued no restoration command. Both functional restores booted an independent Odoo process at the original revision and recovered the old ORM record and attachment bytes.

The loopback suite ran from `7f75c85a80b20c361427649b1d9d9f09dbb0ff28` on a GitHub-hosted Ubuntu runner. The network-isolated test container used Debian Bookworm with Python 3.12 and OpenSSH `1:9.2p1-2+deb12u10`. The image build took 19 seconds and the assertions took about two seconds. Both steps and cleanup exited 0. The generated Bash command accepted the pinned key and the approved exact SHA, rejected an incorrect key and an unconfirmed production request, and propagated remote success and failure. The real forced command denied a free-form request. The first attempt was canceled during a stalled Ubuntu package-index download before the SSH assertions ran. [Passing OpenSSH run](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45150899824) · [Canceled setup run](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45149548636)

The local Windows host has Python 3.11.9 and PostgreSQL 17.12 clients and server. Docker, Podman and a disposable systemd VM are unavailable locally. With the PostgreSQL integration enabled and a new workspace temporary directory, pytest collected 123 tests, passed 118, failed 0 and skipped five Linux or POSIX cases. The separately selected PostgreSQL integration passed one test. The local runner never targets an existing database.

[Hosted CI at `e79a7a3dc1a0cca3b3006da03291eb1413e8cdc5`](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45149354593) collected 121 tests on each OS/Python combination. Ubuntu Python 3.11 and 3.14 each passed 120, failed 0 and skipped the opt-in PostgreSQL test. Windows Python 3.11 passed 115, failed 0 and skipped six.

Ruff lint and formatting, distribution builds, strict Twine validation, actionlint and CLI smoke checks passed locally. The CLI checks exercised `--help`, `version` and JSON configuration validation. The Odoo and OpenSSH scripts are standalone integration suites, so they report assertion and process results without pytest collection counts.

## Reproduction commands

```sh
python -m pip install -e '.[dev]'
python -m pytest
ruff check .
ruff format --check .
python -m build
python -m twine check --strict dist/*
GHOSTDEPLOY_TEST_POSTGRESQL=1 python -m pytest tests/integration -v
python tests/odoo/run.py 18.0
python tests/odoo/run.py 19.0
```

Use [testing and release readiness](testing.md) for the Windows PostgreSQL command, OpenSSH container, and paths for hosted workflow execution. Actionlint checks both repository workflows and three generated flavors: staging push, manual staging, and production.

## Fixture boundaries and changed files

The Odoo image refuses to issue setup SQL unless its build marker exists, its user is unprivileged and PostgreSQL proves ownership with the unique per-project token. CLI upgrades operate only on the synthetic allowlist. Restore creates a new database under a generated name, verifies ownership and emptiness, and rejects filestore entries outside the expected archive root. Compose cleanup targets only the generated project. The loopback SSH fixture generates its client and host keys at runtime inside a network-isolated container. No private keys are stored in the repository.

- Packaging and CI: `.dockerignore`, `MANIFEST.in`, `.github/workflows/integration.yml`.
- Odoo fixture: `tests/odoo/Dockerfile`, `tests/odoo/check_upgrade.py`, `tests/odoo/compose.yml`, `tests/odoo/fixture.py`, `tests/odoo/fixture.sql` and `tests/odoo/run.py`.
- SSH fixture: `tests/ssh/Dockerfile` and `tests/ssh/check_forced_command.py`.
- Interruption regressions: `tests/test_deploy.py`.
- Documentation: `README.md`, `CHANGELOG.md`, `docs/first-deployment.md`, `docs/operations.md`, `docs/testing.md`, `docs/systemd-staging.md` and this report.

## Release gates

| Gate | Result | Evidence |
| --- | --- | --- |
| PostgreSQL restore | PASS | Local disposable PostgreSQL 17.12 integration test. |
| Odoo 18 upgrade | PASS | Original Docker fixture, explicit A and B updates and C unchanged. |
| Odoo 19 upgrade | PASS | Original Docker fixture, explicit A and B updates and C unchanged. |
| Multi-database selection | PASS | Original Odoo fixtures and hermetic planning tests. |
| Full deployment executor | PASS | Odoo 18 and 19 container runs call the CLI with real Git, PostgreSQL, artifacts and Odoo processes. The systemd adapter is outside this result. |
| Partial failure handling | PASS | Both real Odoo fixtures intentionally fail on B, preserve the journal and refuse a retry. The hermetic suite separately tests interruptions. |
| Functional Odoo restore | PASS | Both series restore into a new database and read a matching record and filestore attachment through the independent Odoo runtime. Host login is a separate gate. |
| Linux locking | PASS | Hosted Ubuntu CI runs real contention, process-death and binding tests. |
| Real systemd lifecycle | PENDING | Dedicated Ubuntu VM procedure. |
| Restricted SSH | PASS | Disposable loopback OpenSSH enforces the authorized-keys restriction and rejects an incorrect host-key pin. The deployment effect uses a fixture executor. |
| GitHub-to-staging SSH | PENDING | Dedicated Ubuntu account, host firewall, repository environment rules and deployment together. |

Handle an uncertain database by reconciling the recorded commit and backups before retrying. A process health response cannot verify arbitrary addon behavior. The next milestone is real systemd and GitHub environment verification on a dedicated Ubuntu VM, including an authenticated application and restore drill.
