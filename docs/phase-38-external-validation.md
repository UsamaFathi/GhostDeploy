# Phase 3.8 external validation and Alpha preparation

Phase 3.8 ran against **Alpha** development version `0.3.0.dev0`. The fetched `main` baseline was `8b53a1326a4aab3ce482b9db3426302b0b0bb61b`. See the later [0.3.0a1 release notes](releases/0.3.0a1.md) and [maintainer checklist](alpha-release.md) for release preparation.

The SSH fix is `f824da771988d3b4ea1dcc73296bd2ae1300b691`. The final implementation tested for Phase 3.8 is `ebde21cf66324560ca4f5388ac4ec5510075c231`. The subsequent report commit records results and operator clarifications.

## Staging kit

The [independent staging guide](../validation/staging/README.md) covers the baseline addon, the two target commits, synthetic data, the dedicated service and local PostgreSQL cluster, scoped sudo, the restricted key, dispatch and evidence submission. It uses the existing addon fixture and workflow generator. The kit is included in the source distribution.

Root issues an expiring, host-bound authorization for the exact staging and Odoo configs. The kit accepts only the three named synthetic databases, a staging environment, an Odoo listener and PostgreSQL on loopback, and administrator-controlled config paths. The forced-command gate checks that authorization and delegates to the existing GhostDeploy SSH entry point. Local preflight and evidence checks are read-only. The verifier checks the reviewed SHA, deployment ID, backup sizes and hashes, and the completed or uncertain journal state. It keeps the external gate pending. The separate restore and data checks are independent operator steps.

The [sanitized report template](../validation/staging/report-template.md) and GitHub issue form request version, platform and outcome evidence. The kit's [cleanup procedure](../validation/staging/README.md#restore-report-and-clean-up) uses operator revocation and disposal for the authorized VM.

## Security and dependency review

| Area | Finding and verification |
| --- | --- |
| Remote exit status | The SSH runner previously treated every status other than `failed` as success. It now returns zero only for `succeeded` or `unchanged` with phase `complete`. Regression tests cover incomplete, failed and successful records. |
| Workflow and remote authorization | The generator uses structured inputs, exact event SHA, a branch condition, environment-scoped secrets, concurrency and strict host-key checking. The runner unsets key variables after writing private temporary files. CI actions use full commit pins and read-only contents access. The release design separates build from any future publication privileges. |
| Trusted inputs and execution | Existing strict config, argument arrays, exact Git revision verification and branch-tip checks remain in force. The deploy account and reviewed addon code are trusted. The staging gate checks root-controlled paths and uses an isolated Python invocation. A copied or expired authorization cannot admit a new run. |
| Sudo, allowlists and storage | Host preflight checks service identity and required sudo commands. The kit makes read-only denial probes and requires the precise synthetic allowlist. Existing private backup and state checks, file ownership controls, per-unit locking and durable pending journals remain in force. The verifier follows only the named private artifact paths and checks recorded hashes. |
| Production confirmation | The generated production workflow requires manual dispatch and explicit confirmation. An already authorized forced-command key is a host-side trust boundary, separate from GitHub reviewer enforcement. The kit refuses production configurations and uses a distinct staging identity and authorization. |
| Python dependencies | `pip-audit 2.10.1` queried the advisory service for 43 exact installed package versions from the runtime and development environment. It reported zero known advisories and no skipped packages. This covers the audited snapshot. Deployment hosts and later resolved dependency sets need their own audit. |

The repository uses pinned checkout v4.2.2 and setup-python v5.6.0, with credential persistence disabled for checkout. The hosted logs retained a warning about the Node.js 20 metadata on those pins and ran them on Node.js 24. Test workflows use ordinary pull requests or manual dispatch, with read-only contents access. [Checkout releases](https://github.com/actions/checkout/releases) · [setup-python releases](https://github.com/actions/setup-python/releases)

## Verification

| Check | Result |
| --- | --- |
| Local pytest | Windows and Python 3.11.9 collected 148 tests. 142 passed and six skipped, including the opt-in PostgreSQL suite and five platform cases. |
| Hosted pytest | Each Ubuntu job collected 148, passed 147 and skipped the opt-in PostgreSQL test. Windows collected 148, passed 142 and skipped six. [CI](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45615378687) |
| Workflow regression | The disposable real OpenSSH fixture passed with a pinned host key, forced command, approval and exact-SHA checks, and remote failure propagation. [Loopback SSH run](https://github.com/UsamaFathi/GhostDeploy/actions/runs/45615379051) |
| Quality and packaging | Ruff lint and formatting, actionlint on repository and generated staging and production workflows, the wheel and source builds, and strict Twine passed. A fresh wheel install passed help, version and staging config smoke checks. The installed Python also ran the staging kit's help in isolated mode. Candidate hashes are generated alongside the artifacts as `SHA256SUMS`. |

The source changes are in `ghostdeploy/workflow.py`, `validation/staging/`, `MANIFEST.in`, and the staging, documentation and SSH regressions under `tests/`. The remaining changes add the Alpha issue form, release and validation reports, and update `README.md`, `CHANGELOG.md` and the support and release-testing guide.

## Evidence and remaining gates

The existing Odoo 18 and 19 combined runs cover real systemd, the allowlist, PostgreSQL backup and restore, an intentional second-database failure, and authenticated browser recovery. The Phase 3.5 and 3.6 reports retain locking, scoped sudo and restricted OpenSSH evidence. [Phase 3.5](phase-35-integration-report.md) · [Phase 3.6](phase-36-beta-validation.md) · [Phase 3.7](phase-37-combined-staging.md)

**External deployment remains PENDING.** This phase had no independently reachable, explicitly authorized staging host. GitHub environment approval, branch enforcement, successful real dispatch and remote failure propagation still need their independent combined run. An isolated local self-hosted runner can rehearse those operator steps. Its results have a separate evidence scope.

For the next milestone, follow the [independent staging guide](../validation/staging/README.md) through a successful dispatch, intentional failure, blocked retry and independent authenticated restore. Submit the minimal evidence through the Alpha issue form. Maintainer approval of the release version and authorization to publish remain separate gates.
