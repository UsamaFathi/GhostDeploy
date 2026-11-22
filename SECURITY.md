# Security

For a suspected vulnerability, use the repository's private GitHub vulnerability reporting feature if enabled. If it is unavailable, contact the repository maintainers privately to agree on a reporting channel. Include the affected version, preconditions, impact and reproduction in a disposable environment. Remove credentials, database contents and customer identifiers from the report.

GhostDeploy trusts the operator, host configuration, service account and approved Git branch. A deploy key can trigger code from that branch and must be treated as sensitive. Use an admin-owned runtime and systemd unit, a restricted SSH command and narrowly scoped sudo permissions. Verify host keys, protect the deployment branch, and use private, backed-up local storage for reports and snapshots. Review the [SSH setup](docs/github-actions.md) and [recovery constraints](docs/operations.md).
