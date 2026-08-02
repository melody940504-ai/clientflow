import asyncio
import hashlib
import os
import re
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


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


class FakeCursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FeedbackDB:
    def __init__(self, comment):
        self.comment = comment
        self.updated_with = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=()):
        if "SELECT cm.id" in query:
            return FakeCursor(self.comment)
        if "UPDATE comments" in query:
            self.updated_with = params
            return FakeCursor()
        raise AssertionError(f"Unexpected query: {query}")


class UserDB:
    def __init__(self, user):
        self.user = user

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=()):
        if "FROM users" in query:
            return FakeCursor(self.user)
        raise AssertionError(f"Unexpected query: {query}")


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
            "brand_color": "#9b8cf6",
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
        self.assertEqual(actor, ("guest", "Guest reviewer"))

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

    def test_public_review_access_limits_actions(self):
        self.assertFalse(main.guest_action_is_allowed("view", "comment"))
        self.assertFalse(main.guest_action_is_allowed("view", "approve"))
        self.assertTrue(main.guest_action_is_allowed("comment", "comment"))
        self.assertFalse(main.guest_action_is_allowed("comment", "reject"))
        self.assertTrue(main.guest_action_is_allowed("approve", "comment"))
        self.assertTrue(main.guest_action_is_allowed("approve", "approve"))
        self.assertTrue(main.guest_action_is_allowed("approve", "reject"))

    def test_review_plan_values_are_normalized(self):
        self.assertEqual(main.normalize_review_due_at("2026-08-15"), "2026-08-15")
        self.assertEqual(main.normalize_review_due_at(""), "")
        self.assertEqual(main.normalize_guest_access("COMMENT"), "comment")
        self.assertEqual(main.normalize_guest_access("unexpected"), "comment")
        with self.assertRaises(HTTPException):
            main.normalize_review_due_at("15/08/2026")

    def test_delivery_checklist_ignores_unknown_items(self):
        self.assertEqual(
            main.parse_delivery_checklist(
                "master,captions,unknown,delivery_link"
            ),
            {"master", "captions", "delivery_link"},
        )

    def test_inactive_account_session_is_rejected(self):
        token = main.serializer.dumps({"user_id": 12, "version": 3})
        inactive_user = {
            "id": 12,
            "session_version": 3,
            "is_active": False,
        }
        with patch.object(main, "get_db", return_value=UserDB(inactive_user)):
            self.assertIsNone(main.get_user_from_session_token(token))


class FeedbackResolutionTests(unittest.TestCase):
    def test_owner_can_resolve_client_feedback(self):
        request = FakeRequest()
        request.session["csrf_token"] = "csrf"
        db = FeedbackDB(
            {
                "id": 41,
                "type": "comment",
                "author_role": "client",
                "is_resolved": False,
                "project_id": 9,
                "project_status": "In Revision",
                "project_archived_at": None,
                "version_status": "Awaiting Review",
            }
        )

        with (
            patch.object(
                main,
                "require_user",
                return_value={"id": 7, "role": "owner"},
            ),
            patch.object(main, "is_demo_user", return_value=False),
            patch.object(main, "get_db", return_value=db),
        ):
            response = main.resolve_comment(request, 41, "csrf")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(db.updated_with[0], True)
        self.assertEqual(db.updated_with[2], 41)
        self.assertIn("/projects/9?success=", response.headers["location"])

    def test_owner_can_resolve_guest_feedback(self):
        request = FakeRequest()
        request.session["csrf_token"] = "csrf"
        db = FeedbackDB(
            {
                "id": 42,
                "type": "comment",
                "author_role": "guest",
                "is_resolved": False,
                "project_id": 9,
                "project_status": "In Revision",
                "project_archived_at": None,
                "version_status": "Awaiting Review",
            }
        )

        with (
            patch.object(main, "require_user", return_value={"id": 7, "role": "owner"}),
            patch.object(main, "is_demo_user", return_value=False),
            patch.object(main, "get_db", return_value=db),
        ):
            response = main.resolve_comment(request, 42, "csrf")

        self.assertEqual(response.status_code, 303)
        self.assertTrue(db.updated_with[0])

    def test_client_cannot_resolve_feedback(self):
        request = FakeRequest()
        request.session["csrf_token"] = "csrf"

        with patch.object(
            main,
            "require_user",
            return_value={"id": 8, "role": "client"},
        ):
            with self.assertRaises(HTTPException) as error:
                main.resolve_comment(request, 41, "csrf")

        self.assertEqual(error.exception.status_code, 403)

    def test_archived_project_feedback_is_immutable(self):
        request = FakeRequest()
        request.session["csrf_token"] = "csrf"
        db = FeedbackDB(
            {
                "id": 43,
                "type": "comment",
                "author_role": "guest",
                "is_resolved": False,
                "project_id": 9,
                "project_status": "In Revision",
                "project_archived_at": "2026-08-02T10:00:00",
                "version_status": "Awaiting Review",
            }
        )

        with (
            patch.object(main, "require_user", return_value={"id": 7, "role": "owner"}),
            patch.object(main, "is_demo_user", return_value=False),
            patch.object(main, "get_db", return_value=db),
        ):
            with self.assertRaises(HTTPException) as error:
                main.resolve_comment(request, 43, "csrf")

        self.assertEqual(error.exception.detail, "Archived projects are read-only.")
        self.assertIsNone(db.updated_with)

    def test_approved_project_feedback_is_immutable(self):
        request = FakeRequest()
        request.session["csrf_token"] = "csrf"
        db = FeedbackDB(
            {
                "id": 41,
                "type": "comment",
                "author_role": "client",
                "is_resolved": False,
                "project_id": 9,
                "project_status": "Approved",
                "project_archived_at": None,
                "version_status": "Approved",
            }
        )

        with (
            patch.object(
                main,
                "require_user",
                return_value={"id": 7, "role": "owner"},
            ),
            patch.object(main, "is_demo_user", return_value=False),
            patch.object(main, "get_db", return_value=db),
        ):
            with self.assertRaises(HTTPException) as error:
                main.resolve_comment(request, 41, "csrf")

        self.assertEqual(error.exception.status_code, 400)
        self.assertIsNone(db.updated_with)

    def test_approved_version_feedback_stays_immutable_after_project_reopens(self):
        request = FakeRequest()
        request.session["csrf_token"] = "csrf"
        db = FeedbackDB(
            {
                "id": 41,
                "type": "comment",
                "author_role": "client",
                "is_resolved": True,
                "project_id": 9,
                "project_status": "Awaiting Review",
                "project_archived_at": None,
                "version_status": "Approved",
            }
        )

        with (
            patch.object(
                main,
                "require_user",
                return_value={"id": 7, "role": "owner"},
            ),
            patch.object(main, "is_demo_user", return_value=False),
            patch.object(main, "get_db", return_value=db),
        ):
            with self.assertRaises(HTTPException) as error:
                main.resolve_comment(request, 41, "csrf")

        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(error.exception.detail, "Approved versions are read-only.")
        self.assertIsNone(db.updated_with)


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

    def test_upload_signature_must_match_declared_file_type(self):
        self.assertTrue(
            main.upload_signature_matches(b"%PDF-1.7\n", ".pdf")
        )
        self.assertTrue(
            main.upload_signature_matches(b"\x89PNG\r\n\x1a\n", ".png")
        )
        self.assertFalse(
            main.upload_signature_matches(b"<script>alert(1)</script>", ".png")
        )

    def test_unknown_attachment_type_is_rejected(self):
        self.assertFalse(
            main.upload_signature_matches(b"MZ", ".exe")
        )


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
                brand_color="#9b8cf6",
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

    def test_feedback_resolution_is_owner_scoped(self):
        project_root = Path(__file__).parents[1]
        source = (project_root / "app" / "main.py").read_text(
            encoding="utf-8"
        )
        project_template = (
            project_root / "app" / "templates" / "project.html"
        ).read_text(encoding="utf-8")
        dashboard_template = (
            project_root / "app" / "templates" / "dashboard.html"
        ).read_text(encoding="utf-8")
        route_paths = [route.path for route in main.app.routes]

        self.assertIn("/comments/{comment_id}/resolve", route_paths)
        self.assertIn("p.user_id = ?", source)
        self.assertIn("Only studio collaborators can update feedback status.", source)
        self.assertIn("Only feedback notes can be resolved.", source)
        self.assertIn("Approved projects are read-only.", source)
        self.assertIn("ALTER TABLE comments ADD COLUMN IF NOT EXISTS is_resolved", source)
        self.assertEqual(source.count("LIKE 'timestamp_%%'"), 3)
        self.assertNotIn("LIKE 'timestamp_%'", source)
        self.assertEqual(
            source.count("AND p.status NOT IN ('Approved', 'Published')"),
            2,
        )
        self.assertIn("SET is_resolved = TRUE, resolved_at = ?", source)
        self.assertIn("comments_open_feedback_idx", source)
        self.assertIn("GZipMiddleware", source)
        self.assertEqual(source.count("background_tasks.add_task("), 4)
        self.assertIn("sent = send_activity_email(", source)
        self.assertIn("if not sent:", source)
        self.assertIn('author_role in {"client", "guest"}', source)
        self.assertIn('action="/comments/{{ c.id }}/resolve"', project_template)
        self.assertIn("Mark resolved", project_template)
        self.assertIn(
            "{% if project.status in ['Approved', 'Published'] %}Closed",
            project_template,
        )
        self.assertIn("p.unresolved_count", dashboard_template)

    def test_lumaire_mark_is_transparent_and_theme_aware(self):
        project_root = Path(__file__).parents[1]
        mark = (
            project_root / "app" / "static" / "lumaire-mark.svg"
        ).read_text(encoding="utf-8")
        mark_partial = (
            project_root / "app" / "templates" / "_lumaire_mark.html"
        ).read_text(encoding="utf-8")

        self.assertNotIn("<rect", mark)
        self.assertIn("prefers-color-scheme: dark", mark)
        self.assertIn('class="lumaire-mark-main"', mark_partial)
        self.assertIn('class="lumaire-mark-accent"', mark_partial)

    def test_demo_banners_use_brand_styling(self):
        templates_dir = Path(__file__).parents[1] / "app" / "templates"
        dashboard = (templates_dir / "dashboard.html").read_text(
            encoding="utf-8"
        )
        project = (templates_dir / "project.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("demo-banner-fix", dashboard)
        self.assertIn("demo-banner-fix", project)
        self.assertNotIn("border-teal-500", dashboard + project)
        self.assertNotIn("text-teal-300", dashboard + project)

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

    def test_dashboard_archives_only_published_projects(self):
        project_root = Path(__file__).parents[1]
        dashboard = (
            project_root / "app" / "templates" / "dashboard.html"
        ).read_text(encoding="utf-8")
        completed_card = (
            project_root
            / "app"
            / "templates"
            / "_dashboard_project_card.html"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "projects|rejectattr('status', 'equalto', 'Published')",
            dashboard,
        )
        self.assertIn(
            "projects|selectattr('status', 'equalto', 'Published')",
            dashboard,
        )
        self.assertIn('class="completed-projects', dashboard)
        self.assertIn("Completed projects", dashboard)
        self.assertIn("View history", completed_card)
        self.assertNotIn("Approved')|list", dashboard)

    def test_workspace_theme_toggle_matches_landing_control(self):
        project_root = Path(__file__).parents[1]
        templates_dir = project_root / "app" / "templates"
        base_template = (templates_dir / "base.html").read_text(
            encoding="utf-8"
        )
        dashboard_template = (templates_dir / "dashboard.html").read_text(
            encoding="utf-8"
        )
        project_template = (templates_dir / "project.html").read_text(
            encoding="utf-8"
        )
        app_script = (
            project_root / "app" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        workspace_styles = (
            project_root / "app" / "static" / "workspace.css"
        ).read_text(encoding="utf-8")

        for source in (base_template, dashboard_template, project_template):
            self.assertIn("data-theme-toggle", source)
            self.assertIn("data-theme-icon", source)
            self.assertIn("theme-icon-sun", source)
            self.assertIn("theme-icon-moon", source)

        self.assertNotIn('id="theme-toggle"', dashboard_template)
        self.assertNotIn('id="theme-toggle"', project_template)
        self.assertIn("root.classList.toggle('light-theme'", app_script)
        self.assertIn(".workspace-theme-toggle", workspace_styles)

    def test_landing_review_promises_are_backed_by_workspace_controls(self):
        project_root = Path(__file__).parents[1]
        templates_dir = project_root / "app" / "templates"
        new_project = (templates_dir / "new_project.html").read_text(
            encoding="utf-8"
        )
        project = (templates_dir / "project.html").read_text(
            encoding="utf-8"
        )
        route_paths = [route.path for route in main.app.routes]

        self.assertIn('name="review_due_at"', new_project)
        self.assertIn('name="guest_access"', new_project)
        self.assertIn('name="review_due_at"', project)
        self.assertIn('name="guest_access"', project)
        self.assertIn('name="delivery_items"', project)
        self.assertIn("This review link is view-only", project)
        self.assertIn('placeholder="YYYY-MM-DD"', new_project)
        self.assertIn('placeholder="YYYY-MM-DD"', project)
        self.assertIn("review-plan-card", project)
        self.assertIn("/projects/{project_id}/review-settings", route_paths)
        self.assertIn("/projects/{project_id}/delivery-checklist", route_paths)

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


class CollaborationFeatureTests(unittest.TestCase):
    def test_annotation_payload_is_normalized_and_bounded(self):
        payload = main.normalize_annotation_data(
            '{"strokes":[[{"x":0.2,"y":0.4}]]}'
        )
        self.assertEqual(payload, '{"strokes":[[{"x":0.2,"y":0.4}]]}')
        with self.assertRaises(HTTPException):
            main.normalize_annotation_data('{"strokes":"invalid"}')

    def test_disabled_public_link_is_rejected(self):
        project = {
            "review_token": "token",
            "review_token_expires_at": None,
            "review_link_enabled": False,
        }
        self.assertFalse(main.review_token_is_valid(project, "token"))

    def test_new_collaboration_routes_and_claims_exist(self):
        routes = {route.path for route in main.app.routes}
        self.assertIn("/projects/{project_id}/compare", routes)
        self.assertIn("/projects/{project_id}/review-link/regenerate", routes)
        self.assertIn("/review/{review_token}/unlock", routes)
        self.assertIn("/team", routes)
        landing = (Path(__file__).parents[1] / "app" / "templates" / "landing.html").read_text(encoding="utf-8")
        for claim in (
            "Side-by-side comparison",
            "Protected review links",
            "Frame annotations",
            "Threaded discussion",
            "Shared studio",
        ):
            self.assertIn(claim, landing)


class InfrastructureSafetyTests(unittest.TestCase):
    def test_legacy_attachment_url_becomes_storage_path(self):
        with patch.object(main, "SUPABASE_URL", "https://project.supabase.co"):
            path = main.storage_path_from_url(
                "https://project.supabase.co/storage/v1/object/public/attachments/projects/7/file%20name.pdf",
                "attachments",
            )
        self.assertEqual(path, "projects/7/file name.pdf")

    def test_external_attachment_url_is_not_treated_as_storage_object(self):
        with patch.object(main, "SUPABASE_URL", "https://project.supabase.co"):
            path = main.storage_path_from_url(
                "https://example.com/storage/v1/object/public/attachments/private.pdf",
                "attachments",
            )
        self.assertIsNone(path)

    def test_private_attachment_never_falls_back_to_public_url(self):
        row = {
            "attachment_storage_path": None,
            "attachment_url": "https://project.supabase.co/storage/v1/object/public/attachments/comments/file.pdf",
        }
        with (
            patch.object(main, "SUPABASE_URL", "https://project.supabase.co"),
            patch.object(main, "ATTACHMENTS_BUCKET_PRIVATE", True),
            patch.object(main, "signed_storage_url", return_value="signed") as signer,
        ):
            result = main.hydrate_comment_attachment_urls([row])
        self.assertEqual(result[0]["attachment_url"], "signed")
        signer.assert_called_once_with("attachments", "comments/file.pdf", None)

    def test_shared_rate_limit_is_atomic_and_clearable(self):
        class FakeRedis:
            def __init__(self):
                self.values = {}

            def eval(self, script, key_count, key, window):
                self.values[key] = self.values.get(key, 0) + 1
                return [self.values[key], int(window)]

            def delete(self, key):
                self.values.pop(key, None)

        request = types.SimpleNamespace(
            headers={},
            client=types.SimpleNamespace(host="127.0.0.1"),
        )
        fake_redis = FakeRedis()
        with (
            patch.object(main, "_redis_rate_limit_client", fake_redis),
            patch.object(main, "_redis_rate_limit_retry_at", 0.0),
        ):
            main.enforce_rate_limit(request, "login", "test@example.com", (2, 60))
            main.enforce_rate_limit(request, "login", "test@example.com", (2, 60))
            with self.assertRaises(HTTPException) as error:
                main.enforce_rate_limit(request, "login", "test@example.com", (2, 60))
            self.assertEqual(error.exception.status_code, 429)
            main.clear_rate_limit(request, "login", "test@example.com")
            main.enforce_rate_limit(request, "login", "test@example.com", (2, 60))


if __name__ == "__main__":
    unittest.main()
