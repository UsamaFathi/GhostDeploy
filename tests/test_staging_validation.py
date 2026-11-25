import hashlib
import json
from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

from ghostdeploy.config import load, validate
from ghostdeploy.core import GhostError

KIT = Path(__file__).resolve().parents[1] / "validation/staging/staging.py"
SPEC = spec_from_file_location("ghostdeploy_staging_kit", KIT)
assert SPEC is not None and SPEC.loader is not None
staging = module_from_spec(SPEC)
SPEC.loader.exec_module(staging)

NOW = datetime(2026, 11, 25, tzinfo=UTC)
FACTS = {"declaration": staging.DECLARATION, "machine_sha256": "fixture"}


@pytest.mark.parametrize(
    "changes",
    [
        {"machine_sha256": "other-host"},
        {"config_sha256": "unexpected"},
        {"expires_at": NOW.isoformat()},
        {"expires_at": (NOW + timedelta(hours=25)).isoformat()},
        {"expires_at": "2026-11-25T01:00:00"},
    ],
)
def test_authorization_rejects_mismatch_or_invalid_expiry(changes):
    record = {**FACTS, "expires_at": (NOW + timedelta(hours=2)).isoformat()}
    staging.check_authorization(record, FACTS, NOW)
    with pytest.raises(GhostError):
        staging.check_authorization({**record, **changes}, FACTS, NOW)


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_authorization_rejects_production_or_nonsynthetic_scope(
    environment, config_data, tmp_path, monkeypatch
):
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("a" * 32)
    monkeypatch.setattr(staging, "MACHINE_ID", machine_id)
    monkeypatch.setattr(staging, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(staging.platform, "freedesktop_os_release", lambda: {"ID": "ubuntu"})
    monkeypatch.setattr(staging, "trusted_file", lambda path: path)
    config_data["deployment"]["environment"] = environment
    config_data["deployment"]["health_check"] = {"url": "http://127.0.0.1:8069/web/health"}
    monkeypatch.setattr(staging, "load", lambda _path: validate(config_data))
    with pytest.raises(GhostError, match="three kit database names"):
        staging.authorized_facts(Path("/etc/ghostdeploy-alpha/ghostdeploy.yml"))


def test_wrong_target_is_rejected_before_reading_staging_evidence():
    with pytest.raises(GhostError, match="full deployment ID"):
        staging.verify(None, "a" * 32, "HEAD", "succeeded", "fixture")


def test_backup_evidence_checks_hash_and_rejects_outside_path(tmp_path, monkeypatch):
    database = staging.DATABASES[0]
    report = {"id": "a" * 32, "base_sha": "b" * 40, "target_sha": "c" * 40}
    monkeypatch.setattr(staging, "private_read", lambda path: path.open("rb"))
    dump = tmp_path / f"{database}.dump"
    store = tmp_path / f"{database}.filestore.tar.gz"
    blob = b"synthetic backup"
    digest = hashlib.sha256(blob).hexdigest()
    for path in (dump, store):
        path.write_bytes(blob)
    evidence = {
        "deployment_id": report["id"],
        "base_sha": report["base_sha"],
        "target_sha": report["target_sha"],
        "database": database,
        "status": "verified",
        "dump": {"path": str(dump), "bytes": len(blob), "sha256": digest},
        "filestore": {
            "path": str(store),
            "bytes": len(blob),
            "sha256": digest,
            "status": "verified",
        },
    }
    staging.check_backup(evidence, tmp_path, report, database)
    dump.write_bytes(b"x" * len(blob))
    with pytest.raises(GhostError, match="recorded size and SHA-256"):
        staging.check_backup(evidence, tmp_path, report, database)
    evidence["dump"]["path"] = str(tmp_path.parent / dump.name)
    with pytest.raises(GhostError, match="artifact path"):
        staging.check_backup(evidence, tmp_path, report, database)


def test_example_uses_staging_and_the_exact_synthetic_database_allowlist():
    example = Path(staging.__file__).with_name("configuration.example.yml")
    config = load(example)
    assert config.deployment.environment == "staging"
    assert config.deployment.databases == staging.DATABASES


@pytest.mark.parametrize("expected", ["succeeded", "failed"])
def test_verifier_preserves_journal_and_distinguishes_outcomes(tmp_path, monkeypatch, expected):
    identifier = "a" * 32
    target = "b" * 40
    unit = "fixture.service"
    databases = staging.DATABASES
    success = expected == "succeeded"
    completed = databases[: 2 if success else 1]
    report = {
        "id": identifier,
        "unit": unit,
        "environment": "staging",
        "base_sha": "c" * 40,
        "target_sha": target,
        "database_oids": dict.fromkeys(databases, 1),
        "upgrades": dict.fromkeys(databases[:2], ["fixture"]),
        "status": expected,
        "phase": "complete" if success else "upgrading_database",
        "in_progress_database": None if success else databases[1],
        "upgraded_databases": completed,
        "verified_databases": completed,
        "backups": [{}, {}],
    }
    state = tmp_path / "state"
    state.mkdir()
    (state / f"{identifier}.json").write_text(json.dumps(report))
    pending = state / f"{unit}.pending.json"
    if not success:
        pending.write_text(json.dumps(report))
    calls = []
    host = SimpleNamespace(
        require_identity=lambda: None,
        odoo=SimpleNamespace(unit=unit),
        config=SimpleNamespace(
            deployment=SimpleNamespace(state_path=state, backup=SimpleNamespace(path=tmp_path)),
            repository=SimpleNamespace(checkout_path=tmp_path, branch="main"),
        ),
        runner=None,
        health=lambda: calls.append("healthy"),
        require_inactive=lambda: calls.append("inactive"),
    )
    monkeypatch.setattr(staging, "private_directory", lambda _: None)
    monkeypatch.setattr(staging, "read_report", lambda path: json.loads(path.read_text()))
    monkeypatch.setattr(
        staging, "GitRepository", lambda *_: SimpleNamespace(clean_head=lambda: target)
    )
    monkeypatch.setattr(
        staging, "check_backup", lambda _, _path, _report, database: calls.append(database)
    )
    result = staging.verify(host, identifier, target, expected, "fixture")
    assert calls == ["healthy" if success else "inactive", *databases[:2]]
    assert result["external_validation"] == "PENDING"
    assert result["uncertain_databases"] == (0 if success else 1)
    assert pending.exists() is (not success)
