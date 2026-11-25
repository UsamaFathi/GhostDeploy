# Architecture and trust boundaries

GhostDeploy runs on the operator's Ubuntu host. The diagram shows the GitHub Actions route. A local operator with the same host permissions can invoke the CLI directly.

```mermaid
flowchart TD
    D[Developer]
    subgraph GH[GitHub repository and deployment environment]
        R[Protected addons branch]
        A[Generated GitHub Actions workflow]
    end
    subgraph Host[Operator-controlled Ubuntu host]
        S[Restricted SSH gateway]
        C[GhostDeploy CLI]
        G[Git checkout and exact SHA verification]
        B[PostgreSQL and filestore backups]
        U[Allowlisted Odoo module upgrades]
        T[Systemd service restart and identity check]
        J[Deployment report and durable journal]
    end
    D -->|Reviewed change| R
    R -->|Exact event SHA| A
    A -->|Pinned host key and dedicated identity| S
    S -->|Fixed forced-command entry point| C
    C -->|Lock, preflight and plan| G
    G -->|Stop service before backup| B
    B -->|Verify artifacts, then synchronize code| U
    U -->|Verify installed module state| T
    T --> J
    C -.->|Record failures and recovery state| J
    J -.->|Outcome and exit status via SSH| A
```

- The repository owner controls branch reviews, workflow permissions, environment rules and the deployment key. The host checks the exact SHA against its configured trusted branch. GitHub environment approval is enforced by GitHub. Treat possession of the dedicated key as authorization to submit the host's restricted deployment request.
- The host administrator controls the forced SSH entry point, root-owned configuration and systemd unit. An unprivileged Odoo service account uses scoped sudo for stop and start. This account executes the reviewed addon code.
- The operator controls PostgreSQL, filestore data, the checkout and private storage for backups and journals. The database allowlist scopes upgrade operations. Every database served by the unit shares the restart and may use the changed addon code.
- GitHub Actions receives the remote command outcome. Application state and recovery artifacts remain on the host. GhostDeploy requires no separate service operated by this project.

See [GitHub Actions setup](github-actions.md), [operations and recovery](operations.md) and the [security policy](../SECURITY.md).
