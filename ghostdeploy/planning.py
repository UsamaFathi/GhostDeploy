import ast
from dataclasses import dataclass
from pathlib import PurePosixPath

from ghostdeploy.config import MODULE, Config
from ghostdeploy.core import GhostError
from ghostdeploy.git import GitRepository

CHECKS = (
    "Clean configured Git branch with the target reachable from origin",
    "Supported Odoo runtime, systemd identity and healthy starting service",
    "Allowlisted databases inspected for installed addons and pending operations",
    "Sufficient private backup storage and verified database and filestore archives before upgrades",
)


@dataclass(frozen=True)
class PlannedChange:
    status: str
    paths: tuple[str, ...]
    addons: tuple[str, ...]
    classifications: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    base_sha: str
    target_sha: str
    changes: tuple[PlannedChange, ...]
    affected_addons: tuple[str, ...]
    candidate_upgrades: tuple[str, ...]
    databases: tuple[str, ...]
    review: tuple[str, ...]
    blockers: tuple[str, ...]
    restart_required: bool
    warnings: tuple[str, ...]
    installed_by_database: dict[str, tuple[str, ...]] | None
    preflight: tuple[str, ...] = CHECKS


def discover(repo: GitRepository, tree, roots):
    addons = {}
    for path, (mode, blob) in tree.items():
        if path.name != "__manifest__.py" or path.parent.parent not in roots:
            continue
        addon = path.parent.name
        if not MODULE.fullmatch(addon) or mode not in (b"100644", b"100755"):
            raise GhostError(f"Invalid addon name or manifest file type at {str(path)!r}.")
        if addon in addons:
            raise GhostError(f"Ambiguous addon {addon!r} exists in more than one configured root.")
        try:
            manifest = ast.literal_eval(repo.manifest(blob, path).decode("utf-8-sig"))
        except (SyntaxError, ValueError, UnicodeError, RecursionError) as exc:
            raise GhostError(
                f"Manifest must be a UTF-8 Python dictionary literal: {str(path)!r}."
            ) from exc
        if (
            not isinstance(manifest, dict)
            or not isinstance(manifest.get("name"), str)
            or not manifest["name"].strip()
        ):
            raise GhostError(f"Odoo manifest is missing a display name: {str(path)!r}.")
        addons[addon] = (path.parent, manifest)
    return addons


def classify(path: PurePosixPath, directory: PurePosixPath | None) -> str:
    name = path.name.lower()
    relative = path.relative_to(directory) if directory else path
    folders = {part.lower() for part in relative.parts[:-1]}
    if name.startswith(("requirements", "constraints")) or name in {
        "__manifest__.py",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "package.json",
        "uv.lock",
        "poetry.lock",
        "pipfile",
        "pipfile.lock",
        "package-lock.json",
        "yarn.lock",
    }:
        return "dependency_or_manifest"
    if folders.intersection({"migrations", "upgrades"}):
        return "migration"
    if "security" in folders or name == "ir.model.access.csv":
        return "security"
    if directory and path.suffix.lower() == ".py":
        return "potential_schema"
    if directory and (
        folders.intersection({"views", "data", "demo", "i18n", "report"})
        or path.suffix.lower() in {".xml", ".csv", ".po"}
    ):
        return "odoo_data"
    if name.split(".")[0] in {
        "readme",
        "changelog",
        "license",
        "contributing",
        "security",
    } and path.suffix.lower() in {"", ".md", ".rst", ".txt"}:
        return "documentation"
    if folders.intersection({"doc", "docs"}) and path.suffix.lower() in {".md", ".rst", ".txt"}:
        return "documentation"
    if not directory and path.parts[:2] == (".github", "workflows"):
        return "automation"
    if (
        directory
        and "static" in folders
        and path.suffix.lower() in {".js", ".css", ".scss", ".svg", ".png", ".jpg"}
    ):
        return "code"
    return "unknown"


def build_plan(
    config: Config,
    repo: GitRepository,
    *,
    base_sha: str | None = None,
    target_sha: str | None = None,
    installed: dict[str, frozenset[str]] | None = None,
) -> Plan:
    base = repo.commit(base_sha) if base_sha is not None else repo.resolve("HEAD")
    target, note = (
        (repo.commit(target_sha), None) if target_sha is not None else repo.default_target(base)
    )
    warnings = list(config.warnings())
    if note:
        warnings.append(note)
    checkout = PurePosixPath(config.repository.checkout_path)
    roots = {PurePosixPath(path).relative_to(checkout) for path in config.odoo.addons_path}
    old_tree, new_tree = repo.tree(base), repo.tree(target)
    old, new = discover(repo, old_tree, roots), discover(repo, new_tree, roots)
    manifest_paths = {
        directory: addon for addons in (old, new) for addon, (directory, _) in addons.items()
    }
    changes = []
    candidates = set()
    touched = set()
    blockers = set()
    review = set()
    restart = False
    if not repo.ancestor(base, target):
        blockers.add("The deployment target must be a fast-forward from the checkout.")
    if any(
        mode not in (b"100644", b"100755")
        for tree in (old_tree, new_tree)
        for mode, _ in tree.values()
    ):
        blockers.add(
            "This release requires regular Git files. Symlinks and submodules need manual deployment."
        )
    for addon, (directory, manifest) in new.items():
        previous = old.get(addon)
        if not previous:
            review.add(
                f"New addon {addon} requires review. GhostDeploy does not explicitly install addons."
            )
            if manifest.get("auto_install") is not None and manifest["auto_install"] is not False:
                blockers.add(f"New addon {addon} enables auto_install. Deploy it manually.")
            continue
        for key in ("depends", "external_dependencies", "auto_install", "installable"):
            if manifest.get(key) != previous[1].get(key):
                blockers.add(
                    f"Addon {addon} changed {key}. Prepare and deploy that change manually."
                )
        if directory != previous[0]:
            review.add(f"Review the new path for addon {addon}.")
    for addon in old.keys() - new.keys():
        blockers.add(
            f"Addon {addon} was removed or renamed. Reconcile installed databases manually."
        )
    for change in repo.diff(base, target):
        addons = set()
        categories = set()
        for value in change.paths:
            path = PurePosixPath(value)
            directory = next((parent for parent in path.parents if parent in manifest_paths), None)
            category = classify(path, directory)
            categories.add(category)
            if directory is not None:
                addon = manifest_paths[directory]
                addons.add(addon)
                if category != "documentation" and addon in new:
                    candidates.add(addon)
            if category in {"unknown", "dependency_or_manifest", "migration"}:
                review.add(f"Review {category.replace('_', ' ')}: {value!r}.")
        touched.update(addons)
        restart |= bool(categories - {"documentation", "automation"})
        changes.append(
            PlannedChange(
                change.status, change.paths, tuple(sorted(addons)), tuple(sorted(categories))
            )
        )
    if restart:
        warnings.append("A service restart affects every database served by this Odoo instance.")
    if candidates and config.deployment.upgrade_policy == "manual_review":
        warnings.append(
            "Execution requires explicit approval of every candidate found installed in an allowlisted database."
        )
    databases = tuple(config.deployment.databases)
    mapping = None
    if installed is not None:
        if set(installed) != set(databases):
            raise GhostError(
                "Database inspection results must cover exactly the configured allowlist."
            )
        mapping = {
            database: tuple(sorted(installed[database] & candidates)) for database in databases
        }
    return Plan(
        base,
        target,
        tuple(changes),
        tuple(sorted(touched)),
        tuple(sorted(candidates)),
        databases,
        tuple(sorted(review)),
        tuple(sorted(blockers)),
        restart,
        tuple(warnings),
        mapping,
    )
