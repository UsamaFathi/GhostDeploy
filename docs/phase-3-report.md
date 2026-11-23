# Phase 3 engineering report

GhostDeploy 0.3.0.dev0 is an **alpha development candidate**. The development branch was created from the clean GitHub `main` baseline `c210b50`. Phase 3 focuses on onboarding and operator feedback. The Phase 2 executor still owns deployment authorization, locking, Git alignment, backup verification and recovery.

| Area | Files and behavior |
| --- | --- |
| Setup | `cli.py` and `discovery.py` provide explicit service selection, safe version and path hints, scoped addon checkout suggestions, an explicit database allowlist and a configuration preview. Non-interactive mode remains discovery-free. |
| Diagnostics | `diagnostics.py` reuses the host, Git and PostgreSQL checks and adds explanations, remediation and JSON statuses. Checks for unavailable prerequisites may be skipped. |
| Feedback | `feedback.py` and `cli.py` provide approval and operation summaries for preview. `deploy.py` records completion timestamps and completed checkpoints without changing the meaning of the pending phase. |
| Workflow | `workflow.py` adds a manual setup checklist, optional manual-only staging workflows and explicit dispatch confirmation for newly generated production workflows. The host SSH request format stays compatible. |
| Verification | Onboarding, documentation, journal and generated-shell regressions are in `tests/`. The local PostgreSQL restore fixture now tests the same database probe used by doctor. CI also smoke-tests a built wheel outside the checkout. |
| Documentation | The [Quick start](quick-start.md), [first deployment tutorial](first-deployment.md), [multi-database guide](multi-database.md) and [troubleshooting guide](troubleshooting.md) walk through an Odoo 19 example with two staging databases. |

## Security and remaining risks

Discovery reads the selected service's metadata and checks process ownership before using its command line or configuration. It never executes discovered binaries. Database permission remains explicit. The wizard's optional verification connects only to named databases over a local socket or loopback. Doctor verifies the process and Odoo configuration before using read-only queries against the allowlist.

Review the protected branch, host SSH key, restricted authorized key, sudo permissions and GitHub environment policy yourself. The wizard and setup checklist never change those controls. Generated staging workflows can use manual dispatch for the first test. A production confirmation in the workflow is a separate operator check from the GitHub environment reviewer policy and the host's forced-command authorization.

A restart can affect every tenant served by the unit. An accepted HTTP probe and expected module versions do not validate business behavior. The operator must run application checks and reconcile a failed deployment before retrying. [Phase 2 threat model](phase-2-audit.md#threat-model) · [Recovery decisions](operations.md#recovery-decisions)

## Verification status

| Local run | Collected | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: | ---: |
| Windows, Python 3.11.9, PostgreSQL integration enabled | 118 | 113 | 0 | 5 |
| Windows, Python 3.14.10, ordinary suite | 118 | 112 | 0 | 6 |

The PostgreSQL 17.12 test started a new loopback cluster, checked the selected database connection, restored an archive into a separately created database and compared attachment bytes. Windows skipped the named FIFO case, POSIX permissions and three Linux locking or state-binding tests. The ordinary suite also skipped the opt-in PostgreSQL test. Ruff, editable CLI smoke, documentation links and the supplied YAML examples passed. Actionlint checked both repository workflows and the generated staging push, manual staging and production workflows.

Odoo 18 and 19 are target versions. The local onboarding tests simulate both. Linux locking, real Odoo CLI upgrades, real Ubuntu systemd and SSH, and login and attachment access on a restored Odoo installation remain separate checks. The [release checklist](testing.md#release-gate) describes the outstanding Beta gates and the disposable fixtures that can run without a paid VPS.

The next step toward Beta is to run the Odoo 18 and 19 container suites, then follow the first deployment tutorial on disposable Ubuntu using the actual systemd and restricted SSH setup. Include a failure on the second database and validate an isolated restore before marking those gates complete.
