# GhostDeploy Phase 2 audit

**Status: Alpha, 0.2.0.dev0.** The baseline was `c7c9496`, the 0.1.0 MVP. The audit followed authorization, lock, preflight, Git provenance, database inspection, backups, upgrade, service startup and reporting in that order. It prioritized data integrity, irreversible transitions and least privilege. The existing CLI, configuration schema and module boundaries were retained.

## Findings

| ID | Severity | Component | Problem and risk | Resolution | Verification |
| --- | --- | --- | --- | --- | --- |
| GD-01 | High | `deploy.py`, `runtime.py` | A replacement database or an upgrade that returned zero without updating its version could look unchanged. A second database could then be upgraded against uncertain state. | Snapshot database OID, server identity and every installed module's version. Recheck snapshots and the exact checkout between critical transitions. Record returned commands separately from version-verified upgrades. | Three-database fixtures, zero-exit/version mismatch, missing and replaced databases, partial upgrades, real PostgreSQL snapshot query. |
| GD-02 | High | `deploy.py`, `core.py` | An unexpected exception or interruption between journal writes could leave the reported outcome ambiguous. A failure path could also clear the recovery gate. | Persist intent before mutation. Preserve every pending failure, record backup milestones and archive success only after operational checks. Handle interrupts and subprocess groups on Linux. | Interrupt, timeout, out-of-space and unexpected error fixtures. Linux signal and service recovery in staging remain pending. |
| GD-03 | High | `deploy.py`, `runtime.py` | A different state directory could bypass the file lock or unresolved journal. Unit aliases could refer to the same service under a different key. | Combine Linux namespace locking and file locking with a private per-service binding in the service account's home. Require the canonical systemd unit ID. | Regression fixtures and Linux-only locking/binding tests. Real systemd alias behavior pending. |
| GD-04 | High | `backup.py` | Link replacement, special files or a partial artifact could make a snapshot unsafe or unusable. | Use private staging files, walk Linux filestores with descriptor-relative no-follow operations, reject links and verify archive streams before publishing. Persist completed metadata per database. | Windows archive integrity fixtures and PostgreSQL 17.12 restore. Linux archive permission and special-file cases remain pending. |
| GD-05 | High | `git.py`, `planning.py`, `workflow.py` | A queued SSH job could deploy an older ancestor of the fetched branch. Incomplete or altered history weakens provenance. | Require a full clone and the exact fetched tip. Preserve the manual cached-tip fast-forward option. Reject grafts, dirty checkouts, unsafe tree entries and non-fast-forward requests. | Real temporary Git repositories and generated workflow tests. Live SSH pending. |
| GD-06 | High | `runtime.py`, `planning.py` | An unmanaged addon root, changed config, missing dependency or persistent Odoo reinitialization could change the effective upgrade scope. | Guard Odoo config and env overrides, reject untracked addon roots under the checkout and check dependencies, versions and addon shadowing. | Hermetic regression tests. Real Odoo CLI checks for both series are pending. |
| GD-07 | Medium | `runtime.py`, `cli.py` | HTTP success could be read as application validation. | Record the verified systemd PID and optional HTTP status as separate evidence, plus explicit `application_verified: false`. Require matching active processes around HTTP checks. | Health and failure fixtures. Host and restored Odoo checks pending. |
| GD-08 | Medium | `tests` and CI | The baseline had no test restore or repeatable Odoo version integration. | Add independent PostgreSQL and Odoo fixtures and a manually dispatched workflow. Keep ordinary CI hermetic and test Python compatibility endpoints. | PostgreSQL 17.12 restore completed. Odoo fixtures and new hosted jobs have not run. |

## Threat model

Treat YAML, Git objects and working tree paths, database selectors, and filestore filenames as inputs. Fixed argument vectors, validated selectors, protected history and private files constrain how those inputs can affect the service. Raw child output is suppressed to avoid leaking connection details. The deploy key is limited by the forced command on the host. SSH still trusts the pinned server host key and the host's repository configuration. GitHub branch and environment permissions are administrator controls.

The operator, running service account, its local Git and process environment, the systemd unit and reviewed addon code remain trusted. Trusted addon code can execute arbitrary application behavior and independently write to its database or filestore. Quiesce integration jobs and other writers, keep the service account and binary paths protected, and keep independent backups. Use one host Linux namespace for all invocations against a service. Process group termination cannot cancel every systemd job or detached process, so recovery may require inspecting both. No automatic recovery or production database restore is implemented.

## Verification and next milestone

| Local run | Collected | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: | ---: |
| Windows, Python 3.11.9, PostgreSQL opt-in | 81 | 76 | 0 | 5 |
| Windows, Python 3.14.10, ordinary suite | 81 | 75 | 0 | 6 |

The Windows skips are three Linux lock and state-binding tests, POSIX permissions, and the FIFO case. The ordinary suite also skips the opt-in PostgreSQL test. Ruff lint and formatting, the CLI smoke commands, and actionlint for CI and both generated deployment workflows passed locally.

The [test guide](testing.md) records the independent release gates. Local PostgreSQL evidence covers an actual archive restore into a fresh database and byte verification of the filestore archive. A separate Odoo application restore is still pending. The container fixture exercises deliberate upgrades on a fresh Odoo installation without systemd or SSH. A passing version check can confirm metadata while leaving tenant behavior unverified. [Odoo 18 CLI](https://www.odoo.com/documentation/18.0/developer/reference/cli.html) · [Odoo 19 CLI](https://www.odoo.com/documentation/19.0/developer/reference/cli.html)

The smallest next milestone is to run the supplied Odoo 18 and 19 CLI fixtures and the Linux lock tests. Then run one end-to-end disposable Ubuntu staging exercise with three databases and a failure on the second upgrade, and restore the matching database and filestore into an independent Odoo instance. Open an attachment and verify login before advancing readiness.
