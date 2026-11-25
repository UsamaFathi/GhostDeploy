"""Run real executor scenarios with a private Odoo process instead of systemd."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from contextlib import redirect_stdout
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

import browser_check
from fixture import ADDON, ATTACHMENT, Cluster, FixtureHost, ProcessHost, make_host, shell

from ghostdeploy import cli
from ghostdeploy.core import GhostError, Runner
from ghostdeploy.deploy import deployment_lock
from ghostdeploy.git import GitRepository

FAILURE = "GHOSTDEPLOY_INTENTIONAL_SECOND_DATABASE_FAILURE"
MODULE_STATES = (
    "SELECT json_agg(module_state ORDER BY name) FROM "
    "(SELECT name, state, latest_version FROM ir_module_module) AS module_state"
)


def git(path: Path, *args: str) -> str:
    return Runner().run(["git", *args], cwd=path, label="Fixture Git " + args[0]).decode().strip()


def write_addon(checkout: Path, minor: int, series: str, *, fail_second: bool) -> None:
    addon = checkout / ADDON
    addon.mkdir(exist_ok=True)
    (addon / "__manifest__.py").write_text(
        repr(
            {
                "name": "GhostDeploy fixture",
                "version": f"1.{minor}",
                "depends": ["web"],
                "data": ["data.xml"],
                "license": "LGPL-3",
            }
        ),
        encoding="utf-8",
    )
    model = (
        "from odoo import fields, models\n\n"
        "class Record(models.Model):\n"
        "    _name = 'ghostdeploy.fixture'\n"
        "    _description = 'GhostDeploy test record'\n"
        "    name = fields.Char(required=True)\n"
    )
    if minor:
        model += "    release_note = fields.Char()\n"
    (addon / "__init__.py").write_text(f"{model}\nfrom . import controller\n", encoding="utf-8")
    (addon / "controller.py").write_text(browser_check.CONTROLLER, encoding="utf-8")
    extra = '<field name="release_note">schema verified</field>' if minor else ""
    (addon / "data.xml").write_text(
        '<odoo><record id="sample" model="ghostdeploy.fixture">'
        f'<field name="name">revision-{minor}</field>{extra}'
        f"</record>{browser_check.ACCESS}</odoo>",
        encoding="utf-8",
    )
    if fail_second:
        migration = addon / "migrations" / f"{series}.1.1"
        migration.mkdir(parents=True)
        (migration / "pre-failure.py").write_text(
            "def migrate(cr, version):\n"
            "    cr.execute('SELECT current_database()')\n"
            "    if cr.fetchone()[0].endswith('_b'):\n"
            f"        raise RuntimeError({FAILURE!r})\n",
            encoding="utf-8",
        )


def make_revisions(root: Path, series: str, *, fail_second: bool) -> tuple[Path, str, str]:
    checkout = root / "addons"
    checkout.mkdir()
    git(checkout, "init", "-b", "main")
    git(checkout, "config", "user.name", "GhostDeploy fixture")
    git(checkout, "config", "user.email", "fixture@example.invalid")
    git(checkout, "config", "commit.gpgsign", "false")
    write_addon(checkout, 0, series, fail_second=False)
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "Fixture base")
    base = git(checkout, "rev-parse", "HEAD")
    git(checkout, "switch", "-c", "approved-revision")
    write_addon(checkout, 1, series, fail_second=fail_second)
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "Fixture upgrade")
    target = git(checkout, "rev-parse", "HEAD")
    # A fixture-only remote-tracking ref. The deployment never fetches from a network.
    git(checkout, "update-ref", "refs/remotes/origin/main", target)
    git(checkout, "switch", "main")
    return checkout, base, target


def install(host: FixtureHost, db: str, module: str) -> None:
    host.runner.run(
        [
            *host.odoo_command(),
            "-c",
            host.odoo.config_file,
            "-d",
            db,
            "-i",
            module,
            "--stop-after-init",
            "--no-http",
            "--max-cron-threads=0",
        ],
        label=f"Initialize disposable database {db}",
        env=host.odoo_env(),
        timeout=240,
    )


def app_record(host: FixtureHost, db: str, *, attachment: bool) -> dict:
    statements = (
        "import json\n"
        f"record = env.ref('{ADDON}.sample')\n"
        "result = {'record': record.name, 'release_note': record.release_note if 'release_note' in record._fields else None}\n"
    )
    if attachment:
        statements += (
            "import base64\n"
            "attachment = env['ir.attachment'].search([('name', '=', 'ghostdeploy-recovery.bin')], limit=1)\n"
            "result['attachment'] = base64.b64decode(attachment.datas).hex()\n"
            "result['store_fname'] = attachment.store_fname\n"
        )
    return shell(
        host, db, statements + "print('GHOSTDEPLOY_FIXTURE_RESULT=' + json.dumps(result))"
    )


def write_attachment(host: FixtureHost, db: str, payload: bytes, *, create: bool) -> None:
    target = (
        "env['ir.attachment'].create({'name': 'ghostdeploy-recovery.bin', 'datas': payload, "
        f"'res_model': 'ghostdeploy.fixture', 'res_id': env.ref('{ADDON}.sample').id}})"
        if create
        else "env['ir.attachment'].search([('name', '=', 'ghostdeploy-recovery.bin')], limit=1).write({'datas': payload})"
    )
    shell(
        host,
        db,
        f"import base64\npayload = base64.b64encode({payload!r})\n{target}\nenv.cr.commit()\n"
        "print('GHOSTDEPLOY_FIXTURE_RESULT={\"written\":true}')",
    )


def native_cli(host: FixtureHost, args: list[str]) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, "-m", "ghostdeploy", *args],
        env=host.odoo_env(),
        capture_output=True,
        timeout=360,
        check=False,
    )
    return result.returncode, json.loads(result.stdout)


def cli_deploy(host: FixtureHost, root: Path, target: str) -> tuple[int, dict]:
    destination = root / "ghostdeploy.json"
    # JSON is a YAML subset. Only the GhostDeploy configuration, never the Odoo password.
    destination.write_text(host.config.model_dump_json(), encoding="utf-8")
    stdout = io.StringIO()
    args = [
        "deploy",
        "--config",
        str(destination),
        "--execute",
        "--target-sha",
        target,
        "--upgrade",
        ADDON,
        "--approve-review",
        "--output",
        "json",
    ]
    if not isinstance(host, ProcessHost):
        return native_cli(host, args)
    with (
        patch.object(cli, "Host", return_value=host),
        patch.object(cli, "Runner", return_value=host.runner),
        redirect_stdout(stdout),
    ):
        exit_code = cli.app(args=args, standalone_mode=False)
    return int(exit_code or 0), json.loads(stdout.getvalue())


def systemd_pid(host: FixtureHost) -> int:
    state = host.systemd()
    host.service_process(state)
    pid = int(state["MainPID"])
    cgroups = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    assert any(line.endswith(f"/system.slice/{host.odoo.unit}") for line in cgroups)
    print(f"Verified Odoo PID {pid} in systemd cgroup {host.odoo.unit}", flush=True)
    return pid


def inspect_native_plan(
    host: FixtureHost, root: Path, dbs: list[str], base: str, target: str
) -> None:
    location = root / "plan.json"
    location.write_text(host.config.model_dump_json())
    code, plan = native_cli(
        host,
        [
            "plan",
            "--config",
            str(location),
            "--target-sha",
            target,
            "--inspect-databases",
            "--output",
            "json",
        ],
    )
    assert code == 0, plan
    assert (plan["base_sha"], plan["target_sha"]) == (base, target)
    assert plan["affected_addons"] == [ADDON] and plan["candidate_upgrades"] == [ADDON]
    assert plan["databases"] == dbs
    assert plan["installed_by_database"] == {dbs[0]: [ADDON], dbs[1]: [ADDON], dbs[2]: []}
    host.require_service_permissions()
    print(
        "Verified read-only CLI plan: exact target, one changed addon, three inspected databases",
        flush=True,
    )


def check_backups(report: dict, dbs: list[str], base: str, target: str) -> None:
    assert [backup["database"] for backup in report["backups"]] == dbs[:2]
    for backup in report["backups"]:
        assert (backup["base_sha"], backup["target_sha"], backup["status"]) == (
            base,
            target,
            "verified",
        )
        for artifact in (backup["dump"], backup["filestore"]):
            if "path" in artifact:
                path = Path(artifact["path"])
                with path.open("rb") as stream:
                    assert hashlib.file_digest(stream, "sha256").hexdigest() == artifact["sha256"]
                assert (
                    path.stat().st_size == artifact["bytes"] and (path.stat().st_mode & 0o077) == 0
                )


def restore(
    root: Path,
    checkout: Path,
    cluster: Cluster,
    series: str,
    report: dict,
    expected: dict,
    password: str | None,
    native: Path | None = None,
) -> None:
    restored = cluster.database(f"ghostdeploy_test_{uuid4().hex[:12]}_restored")
    if (
        cluster.sql(
            "SELECT to_regclass('public.ir_module_module') IS NULL", cluster.role(restored)
        )
        != b"t"
    ):
        raise GhostError("The restore destination is not empty.")
    original = report["backups"][0]
    cluster.runner.run(
        [
            "pg_restore",
            "--no-password",
            "--no-owner",
            "--exit-on-error",
            "--dbname",
            restored,
            original["dump"]["path"],
        ],
        label="Restore into the new Odoo fixture database",
        env=cluster.role(restored),
        timeout=180,
    )
    old_checkout = root / "restored-addons"
    git(checkout, "worktree", "add", "--detach", str(old_checkout), report["base_sha"])
    restored_host = make_host(
        root / "restored-runtime", old_checkout, cluster, series, [restored], native=native
    )
    filestore = restored_host.filestore
    assert filestore is not None
    filestore.mkdir()
    with (
        TemporaryDirectory(dir=filestore) as extraction,
        tarfile.open(original["filestore"]["path"]) as archive,
    ):
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if (
                not name.is_relative_to("filestore")
                or ".." in name.parts
                or not (member.isdir() or member.isfile())
            ):
                raise GhostError("Unexpected member in the disposable filestore archive.")
        archive.extractall(extraction, filter="data")
        (Path(extraction) / "filestore").rename(filestore / restored)
    with restored_host.running():
        if native:
            systemd_pid(restored_host)
        assert app_record(restored_host, restored, attachment=True) == expected
        assert restored_host.snapshot_databases()[restored].versions[ADDON] == f"{series}.1.0"
        if password:
            browser_check.verify(restored_host, restored, password, revision=0, content=ATTACHMENT)
    print(
        "PASS functional restore: independent Odoo process, baseline addon, restored ORM record and matching attachment bytes",
        flush=True,
    )


def scenario(
    root: Path,
    cluster: Cluster,
    series: str,
    *,
    fail_second: bool,
    browser: bool = False,
    native: Path | None = None,
    recovery: Cluster | None = None,
) -> None:
    started = monotonic()
    root.mkdir(mode=0o700)
    checkout, base, target = make_revisions(root, series, fail_second=fail_second)
    prefix = "ghostdeploy_test_" + uuid4().hex[:12]
    dbs = [f"{prefix}_{suffix}" for suffix in ("a", "b", "c")]
    for db in dbs:
        cluster.database(db)
    host = make_host(root / "host", checkout, cluster, series, dbs, native=native)
    password = uuid4().hex if browser else None
    try:
        for db in dbs:
            install(host, db, "web" if db == dbs[2] else ADDON)
        write_attachment(host, dbs[0], ATTACHMENT, create=True)
        if password:
            browser_check.create_reader(host, dbs[0], password)
        expected = app_record(host, dbs[0], attachment=True)
        assert expected["attachment"] == ATTACHMENT.hex()
        assert expected["store_fname"] and host.filestore is not None
        assert (host.filestore / dbs[0] / expected["store_fname"]).read_bytes() == ATTACHMENT
        before = host.snapshot_databases()
        unchanged_modules = cluster.sql(MODULE_STATES, cluster.role(dbs[2]))
        assert [ADDON in before[db].versions for db in dbs] == [True, True, False]
        assert "web" in before[dbs[2]].versions
        with host.running():
            if isinstance(host, ProcessHost):
                original_pid = int(host.systemd()["MainPID"])
                host.runner.events.clear()
            else:
                original_pid = systemd_pid(host)
                inspect_native_plan(host, root, dbs, base, target)
            exit_code, report = cli_deploy(host, root, target)
            if "id" not in report and native:
                print(git(checkout, "status", "--porcelain", "--ignored"), flush=True)
            assert "id" in report, report
            assert report["base_sha"] == base and report["target_sha"] == target
            assert report["upgrades"] == {db: [ADDON] for db in dbs[:2]}
            check_backups(report, dbs, base, target)
            repo = GitRepository(checkout, "main", Runner())
            assert repo.clean_head() == target
            if isinstance(host, ProcessHost):
                assert host.runner.events == [
                    ("stop", host.odoo.unit),
                    ("pg_dump", dbs[0]),
                    ("pg_restore", dbs[0]),
                    ("pg_dump", dbs[1]),
                    ("pg_restore", dbs[1]),
                    ("sync", target),
                    ("upgrade", dbs[0]),
                    ("upgrade", dbs[1]),
                    *([] if fail_second else [("start", host.odoo.unit)]),
                ]
            else:
                assert report["unit"] == host.odoo.unit
                assert report["completed_phases"] == [
                    "preflight",
                    "service_stopped",
                    "backups_verified",
                    "code_synchronized",
                    f"database_verified:{dbs[0]}",
                    *(
                        []
                        if fail_second
                        else [f"database_verified:{dbs[1]}", "service_checks_verified"]
                    ),
                ]
                assert not Path(f"/proc/{original_pid}").exists()
            state = Path(host.config.deployment.state_path)
            archive = json.loads((state / f"{report['id']}.json").read_text(encoding="utf-8"))
            assert archive == {key: value for key, value in report.items() if key != "ok"}
            pending = state / f"{host.odoo.unit}.pending.json"
            with deployment_lock(state, host.odoo.unit):
                assert pending.exists() == fail_second
            if fail_second:
                assert (exit_code, report["status"], report["in_progress_database"]) == (
                    1,
                    "failed",
                    dbs[1],
                )
                assert report["verified_databases"] == report["upgraded_databases"] == dbs[:1]
                assert report["health"] is None
                assert (root / "host" / "odoo.log").read_text().count(FAILURE) >= 1
                assert json.loads(pending.read_text(encoding="utf-8")) == archive
                host.require_inactive()
                repeat_exit, repeat = cli_deploy(host, root, target)
                assert repeat_exit == 1 and "unfinished deployment" in repeat["error"]
                assert repo.clean_head() == target and pending.exists()
                assert "matching filestores" in report["recovery"]
                assert cluster.sql(MODULE_STATES, cluster.role(dbs[2])) == unchanged_modules
                assert (
                    app_record(host, dbs[0], attachment=False)["release_note"] == "schema verified"
                )
                if native:
                    assert recovery is not None and recovery.connection != cluster.connection
                    restore(root, checkout, recovery, series, report, expected, password, native)
                    assert json.loads(pending.read_text(encoding="utf-8")) == archive
                    assert repo.clean_head() == target
                    host.require_inactive()
            else:
                assert (exit_code, report["status"], report["verified_databases"]) == (
                    0,
                    "succeeded",
                    dbs[:2],
                )
                assert report["health"]["http_status"] == 200
                new_pid = int(host.systemd()["MainPID"]) if not native else systemd_pid(host)
                assert new_pid != original_pid and report["health"]["process_pid"] == new_pid
                after = host.snapshot_databases()
                assert after[dbs[2]] == before[dbs[2]]
                for db in dbs[:2]:
                    assert after[db].versions == {**before[db].versions, ADDON: f"{series}.1.1"}
                    assert app_record(host, db, attachment=False) == {
                        "record": "revision-1",
                        "release_note": "schema verified",
                    }
                write_attachment(host, dbs[0], b"changed after backup", create=False)
                assert (
                    app_record(host, dbs[0], attachment=True)["attachment"]
                    != expected["attachment"]
                )
                if password:
                    browser_check.verify(
                        host, dbs[0], password, revision=1, content=b"changed after backup"
                    )
                if not native:
                    restore(root, checkout, cluster, series, report, expected, password)
            print(
                f"PASS Odoo {series} {'intentional partial failure' if fail_second else 'executor upgrade'}: "
                f"3 allowlisted databases, CLI exit={exit_code}, {monotonic() - started:.1f}s",
                flush=True,
            )
    except (Exception, KeyboardInterrupt):
        log = root / "host" / "odoo.log"
        if log.is_file():
            print(
                log.read_text(encoding="utf-8", errors="replace")[-10_000:]
                .replace(cluster.password, "[synthetic secret]")
                .replace(password or cluster.password, "[synthetic secret]"),
                flush=True,
            )
        raise


def main() -> None:
    series = os.environ.get("ODOO_TEST_VERSION", "")
    if series not in {"18.0", "19.0"}:
        raise GhostError("Select one supported Odoo series.")
    cluster = Cluster(Runner(), os.environ.get("GHOSTDEPLOY_FIXTURE_TOKEN", ""))
    browser = os.environ.get("GHOSTDEPLOY_FIXTURE_STAGE") == "browser"
    version = (
        Runner().run(["/usr/bin/odoo", "--version"], label="Fixture Odoo version").decode().strip()
    )
    assert series in version
    print(version, flush=True)
    with TemporaryDirectory(prefix="odoo-fixture-") as directory:
        root = Path(directory)
        scenario(root / "success", cluster, series, fail_second=False, browser=browser)
        scenario(root / "failure", cluster, series, fail_second=True)


def combined(root: Path, series: str) -> None:
    assert root.parent in {Path("/srv"), Path("/var/lib"), Path("/var/tmp")}
    assert root.stat().st_uid == 0
    work = root / "work"
    assert work.stat().st_uid == os.geteuid() != 0
    os.umask(0o077)
    connections = json.loads((work / "connection.json").read_text())
    assert connections["live"]["port"] != connections["recovery"]["port"]
    clusters = {
        name: Cluster(Runner(), connection["token"], port=connection["port"])
        for name, connection in connections.items()
    }
    binary = root / "odoo"
    version = Runner().run([sys.executable, str(binary), "--version"], label="Odoo version")
    assert series.encode() in version
    print(version.decode().strip(), flush=True)
    failures = []
    for label, fail_second in (("success", False), ("failure", True)):
        try:
            scenario(
                work / label,
                clusters["live"],
                series,
                fail_second=fail_second,
                browser=True,
                native=root,
                recovery=clusters["recovery"],
            )
        except Exception as error:
            failures.append(error)
    if failures:
        raise ExceptionGroup("Combined staging scenario failures", failures)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--native" and sys.argv[3] in {"18.0", "19.0"}:
        combined(Path(sys.argv[2]), sys.argv[3])
    else:
        main()
