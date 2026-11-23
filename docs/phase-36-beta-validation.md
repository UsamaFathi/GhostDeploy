# Phase 3.6 Beta validation

GhostDeploy remains **Alpha** at `0.3.0.dev0`. Phase 3.6 started from `1e49b355d66909b4c0bda531dc2fbcec1e4fc707` on `main`. Ordinary CI passed on that baseline. [Baseline CI](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45151200537)

## Scope

The existing Phase 3.5 executor suites still cover approved Git revisions, multi-database selection, an intentional failure on the second database, backup integrity and isolated recovery. Phase 3.6 extends the successful scenario with the real Odoo login form and Chromium. An internal test reader opens an authenticated addon page and downloads the linked attachment. The live instance shows the upgraded record and a changed attachment. A fresh browser session against the independently restored instance checks that the original record and bytes are recovered. The existing ORM assertions run independently. [Phase 3.5 evidence](phase-35-integration-report.md)

The systemd suite uses the real `Host` methods for service status, PID and process verification, HTTP probe, and sudo authorization. A synthetic service and modeled database state let the normal deployment CLI exercise start-failure recovery on a separate disposable Ubuntu runner. The fixture verifies that the recorded database upgrade was already confirmed at the point of failure. An interrupted migration can leave a different state with a database marked uncertain, as the Odoo suite demonstrates.

## Safety boundaries

- The systemd job verifies systemd is PID 1 and checks provisioning sudo on a GitHub-hosted runner. Root provisions one randomly named unit, one unprivileged account and a temporary sudoers file permitting only start and stop for that unit. Its account is refused permission to restart, start another unit, or run a generic root command. Cleanup removes the owned resources. The unit starts a synthetic HTTP process on loopback and does not use a real database. [GitHub-hosted runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- The browser stays on the fixture's loopback origin, in an internal Docker network. The fixture creates a read-only addon user with a generated password, keeps credentials out of the logs, and creates the restore destination in the existing uniquely owned PostgreSQL fixture. The browser dependency is confined to the optional image stage. [Playwright browser setup](https://playwright.dev/python/docs/browsers) · [Odoo authenticated routes](https://www.odoo.com/documentation/19.0/developer/reference/backend/http.html)
- The real OpenSSH suite executes the exact generated Bash script. It uses a dedicated loopback account, ephemeral keys and an authorized-keys forced command. Branch and concurrency settings are checked on the generated YAML; GitHub enforcement of those settings and the environment policy requires a real staging run.

## Reproduce the tests

```sh
python -m pytest
ruff check .
ruff format --check .
python -m build
python -m twine check --strict dist/*
actionlint
python tests/odoo/run.py 18.0 --browser
python tests/odoo/run.py 19.0 --browser
```

The last two commands require a verified local Docker daemon and run the full executor suite together with browser checks. Use the manually dispatched **Disposable integration** workflow for `systemd`, `ssh`, `browser18`, `browser19` and `postgresql`. The [test guide](testing.md) documents local equivalents where available. The [systemd staging procedure](systemd-staging.md) covers the remaining combined host test on a dedicated VM.

## Recorded evidence

| Gate | Result | Evidence |
| --- | --- | --- |
| Authenticated Odoo 18 upgrade and restore | PASS | `f7de6d10c1c58210497202e40943562c0321ac2b` ran Odoo `18.0-20261112`, PostgreSQL `17.12` and Chromium `156.0.8078.4` in Docker. Browser checks passed on the upgraded instance and the restored base revision. The existing executor, ORM and intentional failure checks passed. [Odoo 18 run](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45153913694) |
| Authenticated Odoo 19 upgrade and restore | PASS | `7ddcc4a15e5321bc27621bf491a88c41fa341e82` ran Odoo `19.0-20261112`, PostgreSQL `17.12` and Chromium `156.0.8078.4` in Docker. Both browser checks, executor, ORM and intentional partial failure passed. Test and cleanup exited 0 in 557.9 seconds including the image build. [Odoo 19 run](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45154298745) |
| Real systemd and scoped sudo | PASS | `c5246d5b649b93d4f84a03f405b08aba1e1acf03` on Ubuntu 24.04.5 LTS with kernel `7.0.0-1014-aws` and systemd `255.4-1ubuntu8.18`. Real start and stop exited 0. A refused sudo probe, timeout and intentional failed start each returned nonzero. [Systemd run](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45153741965) |
| Recovery after real unit start failure | PASS | Deploy and retry returned 1. The journal retained the correct base, target, synthetic backup and verified database, with no in-progress database. The checkout stayed at the target. Real systemd reported `failed` with `MainPID=0`. [Systemd run](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45153741965) |
| Loopback OpenSSH and workflow checks | PASS | `d3ad9490e98f640704eb797c08aa18a53b4216ae` on a GitHub-hosted Ubuntu runner and Debian Bookworm test container with OpenSSH `1:9.2p1-2+deb12u10`. The script rejected a malformed SHA, wrong client key, wrong host pin and a production push event. The generated job pins the event SHA and branch and sets a unit/environment concurrency key. [OpenSSH run](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45152708598) |
| PostgreSQL archive restore | PASS | Local PostgreSQL `17.12` ran as part of the opt-in Windows suite. Hosted Ubuntu ran the same standalone test on `f7de6d10c1c58210497202e40943562c0321ac2b` with 1 collected and 1 passed. [PostgreSQL run](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45153913454) |
| Real Odoo and PostgreSQL on systemd together | PENDING | The systemd job uses a synthetic unit. The Odoo container fixture controls a private Odoo process. |
| GitHub Actions to a reachable staging server | PENDING | No disposable external host was available. Live GitHub environment approval, branch rules, and the network path remain unverified. |

The systemd runner confirmed that a successful `systemctl stop` can leave a previously failed unit in `failed` with `MainPID=0`. GhostDeploy now reports that observation and asks the operator to confirm the entire unit is stopped. It still requires `inactive` with no main PID for a confirmed shutdown. The initial hosted systemd attempts exposed fixture dependency permissions, the runner's non-executable `/run` mount and the previously untested unit state. [Setup attempt](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45153190998) · [Mount attempt](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45153319779) · [Unit state attempt](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45153454434)

The Odoo containers ran on GitHub-hosted Ubuntu with Docker. The fixture logged exit 0 for each successful upgrade and exit 1 for each intentional failure. The first browser attempts were canceled during package provisioning, before any browser assertions. HTTPS package downloads improved provisioning for the successful runs, but download times still varied. [Initial Odoo 18 setup](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45152696700) · [Initial Odoo 19 setup](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45152702891) · [Superseded Odoo 19 setup](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45153913708)

Local Windows with Python 3.11.9 and PostgreSQL integration enabled collected 125 pytest tests. There were 120 passed, zero failed and five skipped because the tests require Linux locking or POSIX filesystem behavior. Ruff lint and formatting, the wheel and source builds, strict Twine and actionlint on the repository and generated workflows passed.

Hosted CI on `7ddcc4a15e5321bc27621bf491a88c41fa341e82` collected 124 tests per job. Ubuntu with Python 3.11 and 3.14 each passed 123 with one opt-in skip. Windows with Python 3.11 passed 118 with six skips. All jobs had zero failures. [Hosted CI](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45154277956)

## Remaining Beta gates

Run real Odoo and PostgreSQL under the same systemd-managed Ubuntu deployment, then exercise authenticated recovery and a failed deployment there. Run the generated GitHub workflow through a reachable, disposable restricted SSH account with the actual staging environment and branch policy in force. A loopback SSH result establishes the protocol and authorization behavior locally. The combined staging gate also includes GitHub policy, network access, onboarding and operating the installed software on the target host.

## Changed files

- Executor recovery and its regression: `ghostdeploy/deploy.py`, `tests/test_deploy.py`.
- Odoo and Chromium fixture: `tests/odoo/Dockerfile`, `tests/odoo/browser_check.py`, `tests/odoo/check_upgrade.py`, `tests/odoo/compose.yml`, `tests/odoo/run.py`.
- Hosted systemd fixture: `tests/systemd/run.py`, `tests/systemd/check.py`, `tests/systemd/service.py`.
- SSH and CI configuration: `tests/ssh/check_forced_command.py`, `.dockerignore`, `.github/workflows/integration.yml`.
- Documentation: `CHANGELOG.md`, `docs/operations.md`, `docs/phase-36-beta-validation.md`, `docs/systemd-staging.md`, `docs/testing.md`.
