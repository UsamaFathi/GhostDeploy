# Multiple databases on one Odoo service

List only the databases you administer under `deployment.databases`. The wizard may display names declared in Odoo's `db_name` setting. Enter the allowlist explicitly because those names can include databases outside the scope of this deployment. When `db_name` is unset, the wizard asks you to enter known names manually.

| Database | Attendance installed | Effect of a reviewed attendance change |
| --- | --- | --- |
| `training_east` | Yes | Requires a verified database and filestore backup. GhostDeploy then requests and verifies the addon upgrade. |
| `training_west` | No | GhostDeploy checks database state and does not request an attendance upgrade. |
| Another tenant outside the allowlist | Unknown | GhostDeploy does not inspect or mutate it. It can still experience shared-service downtime. |

An offline plan only identifies candidates. Run `plan --inspect-databases` as the Odoo user on the host for installed candidates. The explicit selection limits upgrade commands, but any shared code change can affect runtime behavior across all tenants. Coordinate the maintenance window with everyone using the service.

If an earlier database succeeds and a later database fails, preserve the journal and backups for every affected database. Confirm the commit, the completed upgrades, the uncertain database and the matching filestores before starting the service. A single code revision must be compatible with all of the tenants served by that Odoo process. See [backup and recovery](operations.md#backups-and-lock-files).
