# Phase 3.7 combined staging

GhostDeploy remains **Alpha** at `0.3.0.dev0`. Phase 3.7 started from `87d9d605b558111ab4b46952cc547ccbaa2f708f` on `main`. [Baseline CI](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45157357546) · [Phase 3.6 evidence](phase-36-beta-validation.md)

## Environment

The hosted suite uses a fresh 64-bit Ubuntu 24.04 VM with systemd as PID 1. The official, digest-pinned Odoo image supplies the vendor executable, Python modules and packaged data. A read-only, network-disabled container runs a file export. The real Odoo process runs natively under the VM's systemd and a dedicated service account. The fixture supplies missing shared libraries from the matching Ubuntu image in a private path. It reuses the runner's PostgreSQL binaries and browser system libraries.

Root provisions three unique test units, one unprivileged Odoo account, and two private PostgreSQL clusters under separate Unix accounts. Each cluster binds to a different loopback port with SCRAM, a generated credential and a marker that GhostDeploy's fixture checks before creating databases. The use of stock PostgreSQL binaries is independent of the runner's installed PostgreSQL service and databases. The sudoers rule permits start and stop on the fixture units. The fixture checks denial of restart and generic root commands. Each deployment uses private backup, filestore and journal paths. The restore uses a newly created database in the second cluster and a separate Odoo unit and data directory. Cleanup controls exact owned units, processes, accounts and paths.

Ordinary Compose is useful for isolated Odoo and PostgreSQL checks. It does not provide the host manager, cgroup, account or sudo boundary this suite needs. A privileged nested container would add broad host capabilities and cgroup assumptions. Running directly on a fresh VM lets the fixture verify the real service PID, ownership and membership in its systemd cgroup, without giving the Odoo account Docker access. The [disposable VM procedure](systemd-staging.md) covers staging outside this hosted fixture. [Ubuntu runner inventory](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md) · [Official Odoo images](https://github.com/odoo/docker) · [Playwright browser channels](https://playwright.dev/python/docs/api/class-browsertype)

## What the combined fixture asserts

1. The initial service is healthy. The database-aware CLI plan checks an exact approved commit, one changed addon and three allowlisted databases.
2. The real deploy CLI stops the original service, backs up A and B, synchronizes Git, upgrades A and B, verifies their state and starts the service. The fixture checks backup hashes and mode, the completed journal, that C's module state is unchanged, and that the restarted HTTP responder's PID belongs to the service. The browser authenticates, reads addon data and checks a downloaded attachment.
3. A separate deployment injects an error in B after A was verified. The CLI and refused retry return 1. The fixture validates the pending journal, A's completed entry, B's uncertainty, complete C module-state comparison, the exact checkout and a stopped unit.
4. The recovery uses the failed deployment's database and filestore archives and the recorded base commit. An independent service on the new recovery cluster accepts login and returns the original record and attachment bytes. The failed source checkout, service state and pending journal are checked again afterward.

## Reliability and status

The Phase 3.6 Odoo 18 run that hit its timeout spent the time on Ubuntu and Chromium packages before the browser started. The hosted Phase 3.7 suites export the vendor runtime from pinned images. Playwright `1.64.1` downloads only its pinned headless Chromium executable and bundled support files. The runner supplies the browser libraries, removing the slow Ubuntu package indexes and dependency install from the hosted job. The fixture records Python dependency setup, host provisioning and assertion durations separately. The earlier Odoo 18 success is preserved in the Phase 3.6 report.

Initial attempts failed at vendor extraction because of cross-package symlinks and dangling optional ReportLab PDF font links. The export now resolves vendor data links in the image and excludes the affected PDF font paths. A subsequent run reached Odoo's healthy systemd process, and GhostDeploy correctly refused an untrusted storage parent. The fixture now checks ownership and executable mount flags before choosing a parent and creates private scenario paths explicitly. [Extraction attempt](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45598518394) · [Font-link attempt](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45598931585) · [Storage refusal](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45599619532)

Native Odoo also exposed a dirty checkout from generated Python bytecode and a home-state permissions failure. The fixture follows the [installation guide](installation.md) by disabling bytecode writes and sets a private umask for the home and state. Both series subsequently verified systemd's new Odoo PID after deployment. The runner's Chrome build then failed at Crashpad startup and timed out at navigation after a retry. The pinned headless browser uses private configuration and cache paths and the runner's native libraries. The test driver and browser run under a dedicated transient systemd service for process cleanup. [Odoo 18 preflight refusal](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45600210316) · [Chrome setup failure](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45600998034) · [Navigation timeout](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45601709650)

Requested systemd gates record `PASS` after assertions and cleanup reach exit 0. A missing capability records `SKIPPED` with exit 77, leaving the release gate `PENDING`. Other nonzero outcomes record `FAIL`. An interrupted workflow without a result leaves the gate `PENDING`. The workflow summaries preserve these distinctions, and a hermetic regression runs the actual shell step for the refused-provisioning case.

## Combined results

Both runs passed on code commit `849c049c880340ab22f2167491bc51da2382581a`. The runner was Ubuntu 24.04.5 LTS with kernel `7.0.0-1014-aws` and systemd `255.4-1ubuntu8.18`. Each run used PostgreSQL `16.16` on both clusters and Chromium `156.0.8078.4`. The successful deployment exited 0. Intentional failure on B and the rejected retry exited 1. The combined assertions and owned-resource cleanup completed successfully.

| Suite | Odoo runtime | Python dependencies | Host setup | Assertions | Result |
| --- | --- | --- | --- | --- | --- |
| `browser18` | `18.0-20261112` | 12s | 84.7s | 98.5s | PASS for deployment, partial failure and authenticated restore. [Hosted Odoo 18](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45602557905) |
| `combined19` | `19.0-20261112` | 10s | 95.1s | 82.3s | PASS for deployment, partial failure and authenticated restore. [Hosted Odoo 19](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45602558006) |

The successful cases verified the old and new Odoo PIDs in the same systemd unit and confirmed two upgrades on a three-database allowlist. The failure cases kept A verified, B uncertain and C's module state unchanged. The failed service stayed inactive and the original checkout and pending journal stayed at the attempted deployment. Chromium logged in to the independent restored service at the base revision and downloaded bytes matching the original attachment.

## Reproduce

Manually dispatch **Disposable integration** on the reviewed branch with `combined19` or `browser18`. The latter also runs the complete combined staging path. The hosted prerequisite guard avoids native provisioning on a shared or self-hosted machine. Local Docker installations can still run the independent fixture with `python tests/odoo/run.py 18.0 --browser`.

```sh
gh workflow run integration.yml --ref phase-37-combined-staging -f suite=combined19
gh workflow run integration.yml --ref phase-37-combined-staging -f suite=browser18
```

```sh
python -m pytest
ruff check .
ruff format --check .
python -m build
python -m twine check --strict dist/*
actionlint
```

## Supporting runs

- The existing synthetic systemd test passed on the Phase 3.7 branch. It covered scoped sudo, PID verification, timeout and startup failure and reported `systemd: PASS`. [Synthetic systemd](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45598518441)
- The existing Docker executor path passed with Odoo `19.0-20261123` and PostgreSQL `17.12` at `25cd683`. It finished the upgrade, intentional failure and ORM restore scenarios, and Compose cleanup exited 0. [Compose fallback](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45599854030)
- Local Windows on Python 3.11.9 collected 127 pytest tests, with 122 passed and five Linux or POSIX skips. This run included the opt-in PostgreSQL 17.12 archive restore. Ruff lint and formatting, actionlint for the repository and generated workflows, the source and wheel builds, and strict Twine checks passed.

## Remaining gate

GitHub Actions deployment to an independently reachable restricted staging host is **PENDING**. No such host was both available and explicitly authorized. This gate must exercise environment approval, branch rules, exact event SHA, host-key pinning, forced command, scoped sudo, concurrency and remote failure behavior together. Continue to classify GhostDeploy as Alpha.

## Changed files

- Hosted provisioning and shared scenarios include `tests/odoo/run_host.py`, `tests/odoo/check_upgrade.py`, `tests/odoo/fixture.py` and `tests/odoo/browser_check.py`.
- Workflow reporting and packaging include `.github/workflows/integration.yml`, `tests/test_workflow.py` and `MANIFEST.in`.
- Documentation includes `docs/testing.md`, this report and `CHANGELOG.md`.
