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
            callback()
        finally:
            main.resend.Emails.send = original_send
        self.assertEqual(len(payloads), 1)
        return payloads[0]

    def test_invitation_uses_branding_and_escapes_user_content(self):
        payload = self.capture_email(
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
        payload = self.capture_email(
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
        payload = self.capture_email(
            lambda: main.send_verification_email(
                "owner@example.com",
                "https://example.com/verify/token",
            )
        )

        self.assertEqual(payload["from"], "Lumaire <onboarding@resend.dev>")
        self.assertIn("Confirm your email address", payload["html"])
        self.assertIn("https://example.com/verify/token", payload["html"])


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


if __name__ == "__main__":
    unittest.main()
