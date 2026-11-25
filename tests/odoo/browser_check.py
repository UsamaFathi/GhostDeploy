"""Exercise the isolated Odoo login form, addon page and download in Chromium."""

import os
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from fixture import FixtureHost, ProcessHost, shell

from ghostdeploy.core import GhostError

LOGIN = "ghostdeploy-browser@example.invalid"
PAGE = "/ghostdeploy-fixture"
CONTROLLER = """\
from html import escape

from odoo import http
from odoo.http import request


class FixturePage(http.Controller):
    @http.route("/ghostdeploy-fixture", auth="user", methods=["GET"])
    def index(self):
        record = request.env.ref("ghostdeploy_test_addon.sample")
        attachment = request.env["ir.attachment"].search([
            ("res_model", "=", record._name),
            ("res_id", "=", record.id),
            ("name", "=", "ghostdeploy-recovery.bin"),
        ], limit=1)
        note = record.release_note if "release_note" in record._fields else ""
        return request.make_response(
            "<!doctype html><title>GhostDeploy fixture</title>"
            f"<h1>{escape(record.name)}</h1>"
            f"<p id='note'>{escape(note or '')}</p>"
            f"<a href='/web/content/{attachment.id}?download=true'>Download snapshot</a>",
            [("Content-Type", "text/html; charset=utf-8")],
        )
"""
ACCESS = """\
<record id="fixture_read" model="ir.model.access">
    <field name="name">Read GhostDeploy fixture</field>
    <field name="model_id" ref="model_ghostdeploy_fixture"/>
    <field name="group_id" ref="base.group_user"/>
    <field name="perm_read" eval="True"/>
</record>"""


def create_reader(host: FixtureHost, database: str, password: str) -> None:
    assert shell(
        host,
        database,
        "users = env['res.users'].with_context(no_reset_password=True)\n"
        "groups = 'group_ids' if 'group_ids' in users._fields else 'groups_id'\n"
        "user = users.create({"
        f"'name': 'GhostDeploy browser reader', 'login': {LOGIN!r}, 'password': {password!r}, "
        "groups: [(6, 0, [env.ref('base.group_user').id])]})\n"
        "assert user.has_group('base.group_user') and not user.has_group('base.group_system')\n"
        "env.cr.commit()\n"
        "print('GHOSTDEPLOY_FIXTURE_RESULT={\"created\":true}')",
    ) == {"created": True}


def verify(
    host: FixtureHost,
    database: str,
    password: str,
    *,
    revision: int,
    content: bytes,
) -> None:
    from playwright.sync_api import expect, sync_playwright

    url = urlsplit(host.config.deployment.health_check.url or "")
    assert url.scheme == "http" and url.hostname == "127.0.0.1" and url.port
    assert database in host.config.deployment.databases
    origin = urlunsplit((url.scheme, url.netloc, "", "", ""))
    login = "/web/login?" + urlencode({"db": database, "redirect": PAGE, "login": LOGIN})
    launch_options = {}
    if not isinstance(host, ProcessHost):
        storage = Path(host.odoo.config_file).parent / "browser"
        storage.mkdir(mode=0o700)
        environment = {
            k: v
            for k, v in os.environ.items()
            if k != "LD_LIBRARY_PATH" and not k.lower().endswith("_proxy")
        }
        for variable in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            directory = storage / variable
            directory.mkdir(mode=0o700)
            environment[variable] = str(directory)
        launch_options = {
            "env": environment,
            "args": ["--no-proxy-server"],
        }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(
            base_url=origin, accept_downloads=True, service_workers="block"
        )
        context.set_default_timeout(20_000)
        context.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(origin + "/") else route.abort()
            ),
        )
        try:
            page = context.new_page()
            page.goto(login, wait_until="domcontentloaded")
            expect(page.locator("input[name=login]")).to_be_visible()
            page.goto(PAGE, wait_until="domcontentloaded")
            assert urlsplit(page.url).path == "/web/login"
            expect(page.get_by_role("link", name="Download snapshot")).to_have_count(0)
            page.goto(login, wait_until="domcontentloaded")
            page.locator("input[name=login]").fill(LOGIN)
            page.locator("input[name=password]").fill(password)
            page.locator("form.oe_login_form button[type=submit]").click()
            expect(page).to_have_url(origin + PAGE)
            expect(page.get_by_role("heading", level=1)).to_have_text(f"revision-{revision}")
            expect(page.locator("#note")).to_have_text("schema verified" if revision else "")
            with page.expect_download() as requested:
                page.get_by_role("link", name="Download snapshot").click()
            download = requested.value
            assert download.suggested_filename == "ghostdeploy-recovery.bin"
            assert download.path().read_bytes() == content
            print(
                f"PASS Chromium {browser.version}: authenticated fixture revision {revision}, "
                "rendered addon data and matching downloaded bytes",
                flush=True,
            )
        except Exception as exc:
            raise GhostError(
                f"Browser verification failed: {str(exc).replace(password, '[redacted]')}"
            ) from None
        finally:
            context.close()
            browser.close()
