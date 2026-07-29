import asyncio
import hashlib
import os
import re
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.pop("RENDER", None)


def install_dependency_stubs() -> None:
    resend = types.ModuleType("resend")
    resend.api_key = None
    resend.Emails = types.SimpleNamespace(send=lambda payload: payload)
    sys.modules.setdefault("resend", resend)

    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.IntegrityError = type("IntegrityError", (Exception,), {})
    psycopg2.connect = lambda *args, **kwargs: None
    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = object
    psycopg2.extras = extras
    sys.modules.setdefault("psycopg2", psycopg2)
    sys.modules.setdefault("psycopg2.extras", extras)

    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = object
    sys.modules.setdefault("httpx", httpx)

    authlib = types.ModuleType("authlib")
    integrations = types.ModuleType("authlib.integrations")
    starlette_client = types.ModuleType("authlib.integrations.starlette_client")

    class OAuth:
        def register(self, **kwargs):
            return None

    starlette_client.OAuth = OAuth
    integrations.starlette_client = starlette_client
    authlib.integrations = integrations
    sys.modules.setdefault("authlib", authlib)
    sys.modules.setdefault("authlib.integrations", integrations)
    sys.modules.setdefault("authlib.integrations.starlette_client", starlette_client)


install_dependency_stubs()

from fastapi import HTTPException

from app import main


class FakeRequest:
    def __init__(self):
        self.session = {}


class FakeUpload:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.offset = 0

    async def read(self, size: int) -> bytes:
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class PasswordSecurityTests(unittest.TestCase):
    def test_new_password_hash_round_trip(self):
        stored = main.hash_password("correct horse battery staple")

        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertTrue(main.verify_password("correct horse battery staple", stored))
        self.assertFalse(main.verify_password("wrong password", stored))
        self.assertFalse(main.password_needs_upgrade(stored))

    def test_legacy_hash_remains_valid_but_needs_upgrade(self):
        password = "legacy password"
        salt = "0123456789abcdef"
        digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
        stored = f"{salt}${digest}"

        self.assertTrue(main.verify_password(password, stored))
        self.assertTrue(main.password_needs_upgrade(stored))


class RequestSecurityTests(unittest.TestCase):
    def setUp(self):
        self.project = {
            "id": 9,
            "user_id": 10,
            "client_id": 20,
            "client_name": "Acme Studio",
            "review_token": "valid-review-token",
            "review_token_expires_at": None,
        }
        self.owner = {
            "id": 10,
            "role": "owner",
            "client_reference_id": None,
            "studio_name": "Northstar Creative",
            "brand_color": "#6366f1",
            "logo_url": "",
            "email_sender_name": "Northstar Creative",
            "setup_completed": True,
        }
        self.client = {
            "id": 11,
            "role": "client",
            "client_reference_id": 20,
        }

    def test_csrf_token_is_bound_to_session(self):
        request = FakeRequest()
        token = main.get_csrf_token(request)

        main.validate_csrf(request, token)
        with self.assertRaises(HTTPException):
            main.validate_csrf(request, "different-token")

    def test_public_reviewer_requires_matching_token(self):
        actor = main.get_review_actor(
            None,
            self.project,
            "valid-review-token",
            "approve",
        )
        self.assertEqual(actor, ("client", "Acme Studio"))

        with self.assertRaises(HTTPException):
            main.get_review_actor(None, self.project, "wrong-token", "comment")

    def test_cross_workspace_users_are_rejected(self):
        foreign_owner = dict(self.owner, id=999)
        foreign_client = dict(self.client, client_reference_id=999)

        with self.assertRaises(HTTPException):
            main.get_review_actor(foreign_owner, self.project, "", "comment")
        with self.assertRaises(HTTPException):
            main.get_review_actor(foreign_client, self.project, "", "comment")

    def test_owner_cannot_approve_on_behalf_of_client(self):
        with self.assertRaises(HTTPException):
            main.get_review_actor(self.owner, self.project, "", "approve")

        self.assertEqual(
            main.get_review_actor(self.owner, self.project, "", "comment"),
            ("studio", "Northstar Creative"),
        )

    def test_expired_review_token_is_rejected(self):
        expired = dict(
            self.project,
            review_token_expires_at=(
                datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(minutes=1)
            ).isoformat(),
        )
        self.assertFalse(
            main.review_token_is_valid(expired, "valid-review-token")
        )


class NotificationReadTests(unittest.TestCase):
    class RecordingCursor:
        def fetchall(self):
            return []

    class RecordingDB:
        def __init__(self):
            self.calls = []

        def execute(self, query, params=()):
            self.calls.append((query, params))
            return NotificationReadTests.RecordingCursor()

    def test_marking_project_read_uses_per_user_upsert(self):
        db = self.RecordingDB()

        main.mark_project_notifications_read(db, user_id=12, project_id=34)

        query, params = db.calls[0]
        self.assertIn("project_notification_reads", query)
        self.assertIn("ON CONFLICT (user_id, project_id)", query)
        self.assertEqual(params[:2], (12, 34))
        datetime.fromisoformat(params[2])

    def test_owner_notifications_compare_events_to_last_read_time(self):
        db = self.RecordingDB()

        notifications = main.get_owner_notifications(db, user_id=12)

        self.assertEqual(notifications, [])
        query, params = db.calls[0]
        self.assertIn("events.created_at <= reads.last_read_at", query)
        self.assertIn("LEFT JOIN project_notification_reads", query)
        self.assertEqual(params, (12, 12, 12, 12, 8))


class UploadSecurityTests(unittest.TestCase):
    def test_upload_at_limit_is_accepted(self):
        payload = b"a" * 32
        result = asyncio.run(
            main.read_upload_with_limit(FakeUpload(payload), 32)
        )
        self.assertEqual(result, payload)

    def test_upload_over_limit_is_rejected(self):
        with self.assertRaises(HTTPException) as context:
            asyncio.run(
                main.read_upload_with_limit(FakeUpload(b"a" * 33), 32)
            )
        self.assertEqual(context.exception.status_code, 413)


class EmailTemplateTests(unittest.TestCase):
    def capture_email(self, callback):
        payloads = []
        original_send = main.resend.Emails.send
        main.resend.Emails.send = lambda payload: payloads.append(payload)
        try:
            result = callback()
        finally:
            main.resend.Emails.send = original_send
        self.assertEqual(len(payloads), 1)
        return payloads[0], result

    def test_invitation_uses_branding_and_escapes_user_content(self):
        payload, sent = self.capture_email(
            lambda: main.send_client_invitation_email(
                to_email="client@example.com",
                client_name="<script>alert(1)</script>",
                login_email="client+demo@example.com",
                temporary_password="<temporary>",
                login_url="https://example.com/login",
                sender_name="Northstar\r\nBcc: bad@example.com",
                studio_name="Northstar & Co.",
                brand_color="#f5c84c",
                logo_url="https://example.com/northstar-logo.png",
            )
        )

        self.assertTrue(sent)
        self.assertNotIn("\r", payload["from"])
        self.assertNotIn("\n", payload["from"])
        self.assertIn("Northstar &amp; Co.", payload["html"])
        self.assertIn("&lt;script&gt;", payload["html"])
        self.assertNotIn("<script>", payload["html"])
        self.assertIn("&lt;temporary&gt;", payload["html"])
        self.assertIn("color:#111827", payload["html"])
        self.assertIn("https://example.com/northstar-logo.png", payload["html"])
        self.assertNotIn("test invitation", payload["html"].lower())
        self.assertIn("Open client portal:", payload["text"])

    def test_activity_email_rejects_unsafe_links(self):
        payload, _ = self.capture_email(
            lambda: main.send_activity_email(
                to_email="owner@example.com",
                subject="Project activity\r\nBcc: bad@example.com",
                project_name="<Launch>",
                action_text="Client said <great>",
                link_url="javascript:alert(1)",
                sender_name="Northstar",
                brand_name="Northstar Studio",
                brand_color="#6366f1",
            )
        )

        self.assertIn("&lt;Launch&gt;", payload["html"])
        self.assertIn("Client said &lt;great&gt;", payload["html"])
        self.assertIn('href="#"', payload["html"])
        self.assertNotIn("javascript:", payload["html"])
        self.assertNotIn("javascript:", payload["text"])
        self.assertIn("Link unavailable", payload["text"])
        self.assertNotIn("\r", payload["subject"])
        self.assertNotIn("\n", payload["subject"])

    def test_system_email_keeps_lumaire_brand(self):
        payload, _ = self.capture_email(
            lambda: main.send_verification_email(
                "owner@example.com",
                "https://example.com/verify/token",
            )
        )

        self.assertEqual(payload["from"], "Lumaire <onboarding@resend.dev>")
        self.assertIn("Confirm your email address", payload["html"])
        self.assertIn("https://example.com/verify/token", payload["html"])

    def test_invitation_reports_delivery_failure(self):
        original_send = main.resend.Emails.send
        main.resend.Emails.send = lambda payload: (_ for _ in ()).throw(
            RuntimeError("delivery failed")
        )
        try:
            sent = main.send_client_invitation_email(
                to_email="client@example.com",
                client_name="Client",
                login_email="client@example.com",
                temporary_password="temporary-password",
                login_url="https://example.com/login",
            )
        finally:
            main.resend.Emails.send = original_send

        self.assertFalse(sent)


class ErrorResponseTests(unittest.TestCase):
    @staticmethod
    def request_with_accept(accept: str):
        return main.Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": "/missing",
                "raw_path": b"/missing",
                "query_string": b"",
                "headers": [(b"accept", accept.encode("ascii"))],
                "client": ("test", 50000),
                "server": ("test", 443),
                "root_path": "",
                "session": {},
            }
        )

    def test_browser_errors_render_branded_html(self):
        response = asyncio.run(
            main.browser_http_exception(
                self.request_with_accept("text/html"),
                main.StarletteHTTPException(status_code=404),
            )
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn(b"That page is not here", response.body)
        self.assertIn(b"lumaire-mark.svg", response.body)

    def test_json_clients_keep_standard_error_shape(self):
        response = asyncio.run(
            main.browser_http_exception(
                self.request_with_accept("application/json"),
                main.StarletteHTTPException(
                    status_code=403,
                    detail="Project access denied.",
                ),
            )
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.body,
            b'{"detail":"Project access denied."}',
        )

    def test_redirect_exceptions_stay_redirects(self):
        response = asyncio.run(
            main.browser_http_exception(
                self.request_with_accept("text/html"),
                main.StarletteHTTPException(
                    status_code=303,
                    headers={"Location": "/login?error=session-expired"},
                ),
            )
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/login?error=session-expired",
        )


class TemplateSecurityTests(unittest.TestCase):
    def test_every_post_form_includes_csrf_token(self):
        templates_dir = Path(__file__).parents[1] / "app" / "templates"
        post_forms = []
        for template in templates_dir.glob("*.html"):
            source = template.read_text(encoding="utf-8")
            for form in re.findall(r"<form\b.*?</form>", source, re.DOTALL):
                if re.search(r'method=["\']post["\']', form, re.IGNORECASE):
                    post_forms.append((template.name, form))

        self.assertGreater(len(post_forms), 0)
        missing = [
            name
            for name, form in post_forms
            if 'name="csrf_token"' not in form
        ]
        self.assertEqual(missing, [])

    def test_public_review_link_uses_opaque_token(self):
        project_template = (
            Path(__file__).parents[1] / "app" / "templates" / "project.html"
        ).read_text(encoding="utf-8")

        self.assertIn('/review/{{ project.review_token }}', project_template)
        self.assertNotIn('/review/{{ project.id }}', project_template)

    def test_client_management_owns_invitation_actions(self):
        dashboard_template = (
            Path(__file__).parents[1] / "app" / "templates" / "dashboard.html"
        ).read_text(encoding="utf-8")
        clients_template = (
            Path(__file__).parents[1] / "app" / "templates" / "clients.html"
        ).read_text(encoding="utf-8")

        self.assertIn(
            '/clients/{{ client.id }}/resend-invitation',
            clients_template,
        )
        self.assertIn("Resend invitation", clients_template)
        self.assertIn('id="resend-invitation-modal"', clients_template)
        self.assertNotIn("return confirm(", clients_template)
        self.assertNotIn('action="/clients"', dashboard_template)
        self.assertNotIn('action="/projects"', dashboard_template)

    def test_dashboard_links_to_dedicated_management_pages(self):
        dashboard_template = (
            Path(__file__).parents[1] / "app" / "templates" / "dashboard.html"
        ).read_text(encoding="utf-8")

        self.assertIn('href="/clients"', dashboard_template)
        self.assertIn('href="/projects/new"', dashboard_template)

    def test_notification_reads_are_server_managed(self):
        project_root = Path(__file__).parents[1]
        dashboard_template = (
            project_root / "app" / "templates" / "dashboard.html"
        ).read_text(encoding="utf-8")
        project_template = (
            project_root / "app" / "templates" / "project.html"
        ).read_text(encoding="utf-8")

        self.assertIn("unread_count", dashboard_template)
        self.assertIn("{% if n.is_read %}is-read", dashboard_template)
        self.assertNotIn(
            "lumaire-read-notification-projects",
            dashboard_template + project_template,
        )

    def test_management_routes_and_menu_are_available(self):
        route_paths = [route.path for route in main.app.routes]
        base_template = (
            Path(__file__).parents[1] / "app" / "templates" / "base.html"
        ).read_text(encoding="utf-8")

        self.assertIn("/clients", route_paths)
        self.assertIn("/projects/new", route_paths)
        self.assertLess(
            route_paths.index("/projects/new"),
            route_paths.index("/projects/{project_id}"),
        )
        self.assertIn('href="/clients"', base_template)
        self.assertIn('href="/projects/new"', base_template)

    def test_primary_pages_use_first_visit_onboarding(self):
        templates_dir = Path(__file__).parents[1] / "app" / "templates"
        expected_pages = {
            "dashboard.html": "dashboard-{{ user.role }}",
            "clients.html": "clients",
            "new_project.html": "new-project",
            "analytics.html": "analytics",
            "settings.html": "studio-settings",
        }

        for template_name, onboarding_key in expected_pages.items():
            source = (templates_dir / template_name).read_text(encoding="utf-8")
            with self.subTest(template=template_name):
                self.assertIn('id="lumaire-onboarding-data"', source)
                self.assertIn(f'"key": "{onboarding_key}"', source)
                self.assertIn('"userId": "{{ user.id }}"', source)

    def test_onboarding_assets_and_per_user_storage_are_available(self):
        project_root = Path(__file__).parents[1]
        base_template = (
            project_root / "app" / "templates" / "base.html"
        ).read_text(encoding="utf-8")
        dashboard_template = (
            project_root / "app" / "templates" / "dashboard.html"
        ).read_text(encoding="utf-8")
        onboarding_script = (
            project_root / "app" / "static" / "onboarding.js"
        ).read_text(encoding="utf-8")

        self.assertIn("/static/onboarding.css", base_template)
        self.assertIn("/static/onboarding.js", base_template)
        self.assertIn("/static/onboarding.css", dashboard_template)
        self.assertIn("/static/onboarding.js", dashboard_template)
        self.assertIn("${config.userId || \"shared\"}", onboarding_script)
        self.assertIn("${config.key}:v2", onboarding_script)
        self.assertIn("localStorage.setItem(storageKey, \"complete\")", onboarding_script)

    def test_onboarding_uses_spotlight_targets_instead_of_centered_modal(self):
        project_root = Path(__file__).parents[1]
        onboarding_script = (
            project_root / "app" / "static" / "onboarding.js"
        ).read_text(encoding="utf-8")
        onboarding_styles = (
            project_root / "app" / "static" / "onboarding.css"
        ).read_text(encoding="utf-8")
        templates_dir = project_root / "app" / "templates"

        self.assertIn("onboarding-spotlight", onboarding_script)
        self.assertIn("onboarding-shade-top", onboarding_script)
        self.assertIn("scrollIntoView", onboarding_script)
        self.assertIn("onboarding-close", onboarding_script)
        self.assertIn("step.advanceOnTarget", onboarding_script)
        self.assertNotIn("onboarding-overlay", onboarding_script)
        self.assertIn(".onboarding-spotlight", onboarding_styles)
        self.assertIn('[data-placement="mobile"]', onboarding_styles)

        for template_name in (
            "dashboard.html",
            "clients.html",
            "new_project.html",
            "analytics.html",
            "settings.html",
        ):
            source = (templates_dir / template_name).read_text(encoding="utf-8")
            with self.subTest(template=template_name):
                self.assertIn('"target":', source)

    def test_onboarding_target_ids_exist_in_their_templates(self):
        templates_dir = Path(__file__).parents[1] / "app" / "templates"
        expected_targets = {
            "new_project.html": (
                "project-client-step",
                "project-details-step",
                "project-scope-step",
            ),
            "analytics.html": (
                "analytics-pipeline",
                "analytics-client-activity",
            ),
            "settings.html": (
                "settings-brand-identity",
                "settings-visual-style",
                "settings-client-preview",
            ),
        }

        for template_name, target_ids in expected_targets.items():
            source = (templates_dir / template_name).read_text(encoding="utf-8")
            for target_id in target_ids:
                with self.subTest(template=template_name, target=target_id):
                    self.assertIn(f'id="{target_id}"', source)
                    self.assertIn(f'"target": "#{target_id}"', source)

    def test_page_level_intro_copy_is_not_permanently_rendered(self):
        templates_dir = Path(__file__).parents[1] / "app" / "templates"
        removed_copy = {
            "clients.html": "Create client workspaces",
            "new_project.html": "Start with the client",
            "analytics.html": "A compact view",
            "settings.html": "Control the name",
            "dashboard.html": "A focused view",
        }

        for template_name, old_copy in removed_copy.items():
            source = (templates_dir / template_name).read_text(encoding="utf-8")
            with self.subTest(template=template_name):
                self.assertNotIn(old_copy, source)


if __name__ == "__main__":
    unittest.main()
