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
