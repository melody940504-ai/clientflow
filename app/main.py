from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlencode, urlparse

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import os
import resend
import uuid
import httpx
import psycopg2
from psycopg2.extras import RealDictCursor
try:
    import redis
except ImportError:  # Optional until REDIS_URL is configured.
    redis = None
try:
    import stripe
except ImportError:  # Billing remains disabled until the optional SDK is installed.
    stripe = None
from authlib.integrations.starlette_client import OAuth
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "clientflow.db"
IS_PRODUCTION = os.getenv("RENDER", "").strip().lower() in {"1", "true", "yes"}
SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET:
    if IS_PRODUCTION:
        raise RuntimeError("SESSION_SECRET must be configured in production.")
    SESSION_SECRET = secrets.token_urlsafe(32)
serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="clientflow-session")

app = FastAPI(title="Lumaire")
logger = logging.getLogger("uvicorn.error")

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="oauth_session",
    same_site="lax",
    https_only=IS_PRODUCTION,
)

app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")


def format_utc_iso(value: object) -> str:
    if not value:
        return ""

    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


templates.env.filters["utc_iso"] = format_utc_iso

DATABASE_URL = os.environ.get("DATABASE_URL")
DATABASE_SSLMODE = os.getenv("DATABASE_SSLMODE", "require").strip() or "require"
STATUS_OPTIONS = ["Awaiting Review", "In Revision", "Approved", "Published"]
GUEST_ACCESS_OPTIONS = {"view", "comment", "approve"}
DELIVERY_CHECKLIST_ITEMS = (
    ("master", "Master file"),
    ("captions", "Captions"),
    ("thumbnail", "Thumbnail"),
    ("delivery_link", "Delivery link"),
)
CATEGORY_OPTIONS = ["Shorts", "Reels", "TikTok", "Ad", "YouTube", "Other"]
DEFAULT_STUDIO_NAME = "Lumaire Studio"
DEFAULT_BRAND_COLOR = "#9b8cf6"
DEFAULT_EMAIL_SENDER_NAME = "Lumaire"
EMAIL_TEST_RECIPIENT = os.getenv("EMAIL_TEST_RECIPIENT", "").strip()
EMAIL_FROM_ADDRESS = os.getenv(
    "EMAIL_FROM_ADDRESS", "onboarding@resend.dev"
).strip()
if (
    "@" not in EMAIL_FROM_ADDRESS
    or "\r" in EMAIL_FROM_ADDRESS
    or "\n" in EMAIL_FROM_ADDRESS
):
    raise RuntimeError("EMAIL_FROM_ADDRESS must be a valid email address.")
PASSWORD_ITERATIONS = 600_000
MAX_VIDEO_UPLOAD_BYTES = int(os.getenv("MAX_VIDEO_UPLOAD_MB", "250")) * 1024 * 1024
MAX_ATTACHMENT_UPLOAD_BYTES = int(os.getenv("MAX_ATTACHMENT_UPLOAD_MB", "25")) * 1024 * 1024
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7
LOGIN_RATE_LIMIT = (8, 15 * 60)
PASSWORD_RESET_RATE_LIMIT = (4, 60 * 60)
PUBLIC_UNLOCK_RATE_LIMIT = (8, 15 * 60)
DEMO_ENABLED = os.getenv("DEMO_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DEMO_OWNER_EMAIL = os.getenv(
    "DEMO_OWNER_EMAIL",
    "melody940504+demo@gmail.com",
).strip().lower()
DEMO_CLIENT_EMAIL = os.getenv(
    "DEMO_CLIENT_EMAIL",
    "demo.client@example.com",
).strip().lower()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ATTACHMENTS_BUCKET_PRIVATE = os.getenv(
    "ATTACHMENTS_BUCKET_PRIVATE", "true"
).strip().lower() in {"1", "true", "yes", "on"}
REDIS_URL = os.getenv("REDIS_URL", "").strip()
RUN_DB_MIGRATIONS = os.getenv("RUN_DB_MIGRATIONS", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID", "").strip()
STRIPE_BUSINESS_PRICE_ID = os.getenv("STRIPE_BUSINESS_PRICE_ID", "").strip()
BILLING_ENABLED = bool(
    stripe
    and STRIPE_SECRET_KEY
    and STRIPE_WEBHOOK_SECRET
    and STRIPE_PRO_PRICE_ID
    and STRIPE_BUSINESS_PRICE_ID
)
if stripe and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
resend.api_key = os.getenv("RESEND_API_KEY")

PLAN_CATALOG = {
    "free": {
        "name": "Free",
        "active_projects": 3,
        "clients": 3,
        "seats": 1,
        "versions_per_project": 3,
        "features": {"review", "comments", "approvals", "public_links"},
    },
    "pro": {
        "name": "Pro",
        "active_projects": 50,
        "clients": 100,
        "seats": 5,
        "versions_per_project": None,
        "features": {
            "review", "comments", "approvals", "public_links", "analytics",
            "version_compare", "branding", "protected_links", "delivery",
        },
    },
    "business": {
        "name": "Business",
        "active_projects": None,
        "clients": None,
        "seats": 25,
        "versions_per_project": None,
        "features": {
            "review", "comments", "approvals", "public_links", "analytics",
            "version_compare", "branding", "protected_links", "delivery",
            "team_roles", "project_assignments",
        },
    },
}
PAID_ACCESS_STATUSES = {"trialing", "active", "past_due"}

_rate_limit_events: dict[str, deque[float]] = defaultdict(deque)
_rate_limit_lock = threading.Lock()
_redis_rate_limit_client = (
    redis.Redis.from_url(REDIS_URL, decode_responses=True)
    if REDIS_URL and redis is not None
    else None
)
_redis_rate_limit_retry_at = 0.0
_signed_url_cache: dict[tuple[str, str], tuple[str, float]] = {}
_signed_url_cache_lock = threading.Lock()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

oauth = OAuth()

if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid email profile"
        },
    )


def request_client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(
    request: Request,
    scope: str,
    identifier: str,
    limit: tuple[int, int],
) -> None:
    global _redis_rate_limit_retry_at
    max_attempts, window_seconds = limit
    key = f"{scope}:{request_client_key(request)}:{identifier.strip().lower()}"
    if _redis_rate_limit_client is not None and time.monotonic() >= _redis_rate_limit_retry_at:
        redis_key = "lumaire:rate-limit:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        try:
            attempts, ttl = _redis_rate_limit_client.eval(
                """
                local attempts = redis.call('INCR', KEYS[1])
                if attempts == 1 then
                    redis.call('EXPIRE', KEYS[1], ARGV[1])
                end
                return {attempts, redis.call('TTL', KEYS[1])}
                """,
                1,
                redis_key,
                window_seconds,
            )
            attempts = int(attempts)
            if attempts > max_attempts:
                retry_after = max(1, int(ttl))
                raise HTTPException(
                    status_code=429,
                    detail="Too many attempts. Please wait and try again.",
                    headers={"Retry-After": str(retry_after)},
                )
            return
        except HTTPException:
            raise
        except Exception as exc:
            _redis_rate_limit_retry_at = time.monotonic() + 60
            logger.warning("Shared rate limiter unavailable; using memory fallback: %s", exc)

    now = time.monotonic()
    with _rate_limit_lock:
        events = _rate_limit_events[key]
        while events and now - events[0] >= window_seconds:
            events.popleft()
        if len(events) >= max_attempts:
            retry_after = max(1, int(window_seconds - (now - events[0])))
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please wait and try again.",
                headers={"Retry-After": str(retry_after)},
            )
        events.append(now)


def clear_rate_limit(request: Request, scope: str, identifier: str) -> None:
    global _redis_rate_limit_retry_at
    key = f"{scope}:{request_client_key(request)}:{identifier.strip().lower()}"
    if _redis_rate_limit_client is not None and time.monotonic() >= _redis_rate_limit_retry_at:
        redis_key = "lumaire:rate-limit:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        try:
            _redis_rate_limit_client.delete(redis_key)
        except Exception as exc:
            _redis_rate_limit_retry_at = time.monotonic() + 60
            logger.warning("Could not clear shared rate limit: %s", exc)
    with _rate_limit_lock:
        _rate_limit_events.pop(key, None)


def clean_email_sender_name(value: str) -> str:
    cleaned = "".join(
        character
        for character in str(value or "")
        if character not in "\r\n<>"
    ).strip()
    return cleaned[:60] or DEFAULT_EMAIL_SENDER_NAME


def clean_email_subject(value: str) -> str:
    cleaned = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return cleaned[:160] or "Lumaire notification"


def clean_email_brand_color(value: str) -> str:
    value = str(value or "").strip()
    if (
        len(value) == 7
        and value.startswith("#")
        and all(character in "0123456789abcdefABCDEF" for character in value[1:])
    ):
        return value.lower()
    return DEFAULT_BRAND_COLOR


def email_button_text_color(background: str) -> str:
    color = clean_email_brand_color(background)
    red, green, blue = (
        int(color[index:index + 2], 16)
        for index in (1, 3, 5)
    )
    luminance = (0.299 * red) + (0.587 * green) + (0.114 * blue)
    return "#111827" if luminance > 170 else "#ffffff"


def validated_email_url(value: str) -> str:
    value = str(value or "").strip()
    if not value.lower().startswith(("https://", "http://")):
        return ""
    return value


def safe_email_url(value: str) -> str:
    return escape(validated_email_url(value), quote=True) or "#"


def build_email_html(
    *,
    brand_name: str,
    brand_color: str,
    eyebrow: str,
    heading: str,
    body_html: str,
    action_label: str,
    action_url: str,
    footer: str,
    logo_url: str = "",
) -> str:
    safe_brand_name = escape(clean_email_sender_name(brand_name))
    safe_brand_color = clean_email_brand_color(brand_color)
    button_text = email_button_text_color(safe_brand_color)
    safe_action_url = safe_email_url(action_url)
    safe_logo_url = safe_email_url(logo_url)
    brand_header = (
        f'<img src="{safe_logo_url}" alt="{safe_brand_name}" '
        'style="display:block;max-width:180px;max-height:48px;border:0;">'
        if safe_logo_url != "#"
        else f'<div style="font-size:17px;font-weight:700;color:#172033;">{safe_brand_name}</div>'
    )

    return f"""
    <!doctype html>
    <html lang="en">
      <body style="margin:0;background:#f4f6fb;color:#172033;font-family:Arial,sans-serif;">
        <div style="display:none;max-height:0;overflow:hidden;color:transparent;">{escape(heading)}</div>
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f6fb;padding:32px 16px;">
          <tr>
            <td align="center">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;background:#ffffff;border:1px solid #e4e8f0;border-radius:12px;overflow:hidden;">
                <tr>
                  <td style="height:6px;background:{safe_brand_color};font-size:0;line-height:0;">&nbsp;</td>
                </tr>
                <tr>
                  <td style="padding:36px 40px 18px;">
                    {brand_header}
                    <div style="margin-top:34px;font-size:12px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:{safe_brand_color};">{escape(eyebrow)}</div>
                    <h1 style="margin:12px 0 18px;font-size:30px;line-height:1.2;color:#111827;">{escape(heading)}</h1>
                    <div style="font-size:16px;line-height:1.7;color:#526078;">{body_html}</div>
                  </td>
                </tr>
                <tr>
                  <td style="padding:10px 40px 36px;">
                    <a href="{safe_action_url}" style="display:inline-block;padding:13px 20px;border-radius:8px;background:{safe_brand_color};color:{button_text};font-size:15px;font-weight:700;text-decoration:none;">{escape(action_label)}</a>
                    <p style="margin:24px 0 0;font-size:12px;line-height:1.6;color:#7b879d;">{escape(footer)}</p>
                  </td>
                </tr>
              </table>
              <p style="margin:18px 0 0;font-size:11px;color:#8b96a9;">Sent securely through Lumaire</p>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """

# ==========================================
# 📬 Email 自動通知模擬引擎
# ==========================================
def send_activity_email(
    to_email: str,
    subject: str,
    project_name: str,
    action_text: str,
    link_url: str,
    sender_name: str = DEFAULT_EMAIL_SENDER_NAME,
    brand_name: str = DEFAULT_STUDIO_NAME,
    brand_color: str = DEFAULT_BRAND_COLOR,
    logo_url: str = "",
) -> bool:
    try:
        recipient = EMAIL_TEST_RECIPIENT or to_email
        safe_project_name = escape(str(project_name or "Untitled project"))
        safe_action_text = escape(str(action_text or "")).replace("\n", "<br>")
        plain_link = validated_email_url(link_url) or "Link unavailable"

        resend.Emails.send({
            "from": f"{clean_email_sender_name(sender_name)} <{EMAIL_FROM_ADDRESS}>",
            "to": [recipient],
            "subject": clean_email_subject(subject),
            "text": (
                f"{project_name}\n\n{action_text}\n\n"
                f"Open project: {plain_link}"
            ),
            "html": build_email_html(
                brand_name=brand_name,
                brand_color=brand_color,
                eyebrow="Project activity",
                heading=project_name,
                body_html=(
                    f'<p style="margin:0 0 12px;"><strong style="color:#172033;">'
                    f"Project:</strong> {safe_project_name}</p>"
                    f'<p style="margin:0;">{safe_action_text}</p>'
                ),
                action_label="Open project",
                action_url=link_url,
                footer="You received this message because you are part of this project workspace.",
                logo_url=logo_url,
            ),
        })

        logger.info("Activity email sent to %s", recipient)
        return True

    except Exception as e:
        logger.error("Activity email failed: %s", e)
        return False

def send_client_invitation_email(
    to_email: str,
    client_name: str,
    login_email: str,
    temporary_password: str,
    login_url: str,
    sender_name: str = DEFAULT_EMAIL_SENDER_NAME,
    studio_name: str = DEFAULT_STUDIO_NAME,
    brand_color: str = DEFAULT_BRAND_COLOR,
    logo_url: str = "",
) -> bool:
    try:
        recipient = EMAIL_TEST_RECIPIENT or to_email
        safe_client_name = escape(str(client_name or "there"))
        safe_login_email = escape(str(login_email or ""))
        safe_password = escape(str(temporary_password or ""))
        plain_link = validated_email_url(login_url) or "Link unavailable"

        resend.Emails.send({
            "from": f"{clean_email_sender_name(sender_name)} <{EMAIL_FROM_ADDRESS}>",
            "to": [recipient],
            "subject": f"You have been invited by {clean_email_sender_name(studio_name)}",
            "text": (
                f"Hi {client_name},\n\n"
                "Your client review workspace is ready.\n\n"
                f"Login email: {login_email}\n"
                f"Temporary password: {temporary_password}\n\n"
                f"Open client portal: {plain_link}"
            ),
            "html": build_email_html(
                brand_name=studio_name,
                brand_color=brand_color,
                eyebrow="Client invitation",
                heading="Your review workspace is ready",
                body_html=f"""
                  <p style="margin:0 0 16px;">Hi {safe_client_name},</p>
                  <p style="margin:0 0 18px;">You have been invited to review projects and share feedback in Lumaire.</p>
                  <div style="padding:18px;background:#f7f8fb;border:1px solid #e4e8f0;border-radius:8px;color:#172033;">
                    <p style="margin:0 0 8px;"><strong>Login email:</strong> {safe_login_email}</p>
                    <p style="margin:0;"><strong>Temporary password:</strong> {safe_password}</p>
                  </div>
                """,
                action_label="Open client portal",
                action_url=login_url,
                footer="For security, sign in and change your temporary password from Account settings.",
                logo_url=logo_url,
            ),
        })

        logger.info("Client invitation email sent to %s", recipient)
        return True

    except Exception as e:
        logger.error("Client invitation email failed: %s", e)
        return False


class InvitationDeliveryError(RuntimeError):
    pass

# 🎯 取得當前這個 main.py 檔案所在的資料夾絕對路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 🎯 不論是在本機 Windows 還是雲端 Linux，都能精準拼出正確的資料庫絕對路徑
DB_PATH = os.path.join(BASE_DIR, "database.db")

def send_verification_email(to_email: str, verify_url: str) -> bool:
    try:
        recipient = EMAIL_TEST_RECIPIENT or to_email

        resend.Emails.send({
            "from": f"Lumaire <{EMAIL_FROM_ADDRESS}>",
            "to": [recipient],
            "subject": "Verify your Lumaire email",
            "text": (
                "Confirm your email address to finish creating your workspace.\n\n"
                f"Verify email: {validated_email_url(verify_url) or 'Link unavailable'}"
            ),
            "html": build_email_html(
                brand_name="Lumaire",
                brand_color=DEFAULT_BRAND_COLOR,
                eyebrow="Email verification",
                heading="Confirm your email address",
                body_html="<p style=\"margin:0;\">Verify your email to finish creating your workspace.</p>",
                action_label="Verify email",
                action_url=verify_url,
                footer="If you did not create a Lumaire account, you can safely ignore this email.",
            ),
        })

        logger.info("Verification email sent to %s", recipient)
        return True

    except Exception as e:
        logger.error("Verification email failed: %s", e)
        return False


def send_password_reset_email(to_email: str, reset_url: str) -> bool:
    try:
        recipient = EMAIL_TEST_RECIPIENT or to_email

        resend.Emails.send({
            "from": f"Lumaire <{EMAIL_FROM_ADDRESS}>",
            "to": [recipient],
            "subject": "Reset your Lumaire password",
            "text": (
                "We received a request to reset your Lumaire password.\n\n"
                f"Reset password: {validated_email_url(reset_url) or 'Link unavailable'}\n\n"
                "This link expires in 1 hour."
            ),
            "html": build_email_html(
                brand_name="Lumaire",
                brand_color=DEFAULT_BRAND_COLOR,
                eyebrow="Account security",
                heading="Reset your password",
                body_html="<p style=\"margin:0;\">We received a request to reset your Lumaire password.</p>",
                action_label="Reset password",
                action_url=reset_url,
                footer="This link expires in 1 hour. If you did not request a reset, you can safely ignore this email.",
            ),
        })

        logger.info("Password reset email sent to %s", recipient)
        return True

    except Exception as e:
        logger.error("Password reset email failed: %s", e)
        return False

class PostgresDB:
    def __init__(self):
        self.conn = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=RealDictCursor,
            sslmode=DATABASE_SSLMODE,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.conn.close()

    def execute(self, query, params=()):
        query = query.replace("?", "%s")
        cur = self.conn.cursor()
        cur.execute(query, params)
        return cur

    def commit(self):
        self.conn.commit()

def get_db():
    return PostgresDB()

def init_db() -> None:
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'owner',
                client_reference_id INTEGER,
                is_verified BOOLEAN NOT NULL DEFAULT FALSE,
                verification_token TEXT,
                reset_token TEXT,
                reset_token_expires_at TEXT,
                studio_name TEXT DEFAULT 'Lumaire Studio',
                brand_color TEXT DEFAULT '#9b8cf6',
                logo_url TEXT,
                email_sender_name TEXT DEFAULT 'Lumaire',
                display_name TEXT,
                workspace_owner_id INTEGER,
                setup_completed BOOLEAN NOT NULL DEFAULT FALSE,
                session_version INTEGER NOT NULL DEFAULT 1,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
                invitation_token TEXT,
                invitation_expires_at TEXT,
                subscription_plan TEXT NOT NULL DEFAULT 'free',
                subscription_status TEXT NOT NULL DEFAULT 'free',
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                stripe_price_id TEXT,
                subscription_current_period_end TEXT,
                billing_updated_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
        try:
            db.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified BOOLEAN NOT NULL DEFAULT FALSE"
            )
            db.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token TEXT"
            )
            db.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token TEXT"
            )
            db.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_expires_at TEXT"
            )
            db.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS studio_name TEXT DEFAULT 'Lumaire Studio'"
            )
            db.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS brand_color TEXT DEFAULT '#9b8cf6'"
            )
            db.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS logo_url TEXT"
            )
            db.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_sender_name TEXT DEFAULT 'Lumaire'"
            )
            db.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS setup_completed BOOLEAN NOT NULL DEFAULT FALSE"
            )
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT")
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS workspace_owner_id INTEGER")
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 1")
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE")
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE")
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS invitation_token TEXT")
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS invitation_expires_at TEXT")
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_plan TEXT NOT NULL DEFAULT 'free'")
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status TEXT NOT NULL DEFAULT 'free'")
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT")
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT")
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_price_id TEXT")
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_current_period_end TEXT")
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS billing_updated_at TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_stripe_customer_idx ON users(stripe_customer_id) WHERE stripe_customer_id IS NOT NULL")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_stripe_subscription_idx ON users(stripe_subscription_id) WHERE stripe_subscription_id IS NOT NULL")
        except Exception:
            logger.exception("User schema migration failed")
            raise

        db.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                email TEXT,
                contact TEXT,
                notes TEXT,
                archived_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        db.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS archived_at TEXT")

        db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                client_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Shorts',
                status TEXT NOT NULL DEFAULT 'Awaiting Review',
                notes TEXT,
                review_token TEXT,
                review_token_expires_at TEXT,
                review_due_at TEXT,
                guest_access TEXT NOT NULL DEFAULT 'comment',
                review_password_hash TEXT,
                review_link_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                review_allow_download BOOLEAN NOT NULL DEFAULT FALSE,
                review_allow_versions BOOLEAN NOT NULL DEFAULT TRUE,
                review_visit_count INTEGER NOT NULL DEFAULT 0,
                review_last_visited_at TEXT,
                delivery_checklist TEXT NOT NULL DEFAULT '',
                archived_at TEXT,
                reopened_at TEXT,
                reopened_by TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(client_id) REFERENCES clients(id)
            )
        """)
        db.execute(
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_token TEXT"
        )
        db.execute(
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_token_expires_at TEXT"
        )
        db.execute(
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_due_at TEXT"
        )
        db.execute(
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS guest_access TEXT NOT NULL DEFAULT 'comment'"
        )
        db.execute("ALTER TABLE projects ALTER COLUMN guest_access SET DEFAULT 'comment'")
        db.execute(
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS delivery_checklist TEXT NOT NULL DEFAULT ''"
        )
        db.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_password_hash TEXT")
        db.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_link_enabled BOOLEAN NOT NULL DEFAULT TRUE")
        db.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_allow_download BOOLEAN NOT NULL DEFAULT FALSE")
        db.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_allow_versions BOOLEAN NOT NULL DEFAULT TRUE")
        db.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_visit_count INTEGER NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_last_visited_at TEXT")
        db.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived_at TEXT")
        db.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS reopened_at TEXT")
        db.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS reopened_by TEXT")
        projects_without_tokens = db.execute(
            "SELECT id FROM projects WHERE review_token IS NULL OR review_token = ''"
        ).fetchall()
        for project in projects_without_tokens:
            db.execute(
                "UPDATE projects SET review_token = ? WHERE id = ?",
                (secrets.token_urlsafe(32), project["id"]),
            )
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS projects_review_token_idx
            ON projects(review_token)
            """
        )

        db.execute("""
            CREATE TABLE IF NOT EXISTS video_versions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                project_id INTEGER NOT NULL,
                version_label TEXT NOT NULL,
                video_url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Awaiting Review',
                notes TEXT,
                created_by_name TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            )
        """)
        db.execute("ALTER TABLE video_versions ADD COLUMN IF NOT EXISTS created_by_name TEXT")

        db.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                video_version_id INTEGER NOT NULL,
                author_role TEXT NOT NULL,
                author_name TEXT NOT NULL,
                body TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'comment',
                is_resolved BOOLEAN NOT NULL DEFAULT FALSE,
                resolved_at TEXT,
                parent_comment_id INTEGER,
                is_internal BOOLEAN NOT NULL DEFAULT FALSE,
                attachment_url TEXT,
                attachment_name TEXT,
                attachment_storage_path TEXT,
                annotation_data TEXT,
                author_email TEXT,
                identity_verified BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TEXT NOT NULL,
                FOREIGN KEY(video_version_id) REFERENCES video_versions(id)
            )
        """)
        db.execute(
            "ALTER TABLE comments ADD COLUMN IF NOT EXISTS is_resolved BOOLEAN NOT NULL DEFAULT FALSE"
        )
        db.execute(
            "ALTER TABLE comments ADD COLUMN IF NOT EXISTS resolved_at TEXT"
        )
        db.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS parent_comment_id INTEGER")
        db.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS is_internal BOOLEAN NOT NULL DEFAULT FALSE")
        db.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS attachment_url TEXT")
        db.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS attachment_name TEXT")
        db.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS attachment_storage_path TEXT")
        db.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS annotation_data TEXT")
        db.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS author_email TEXT")
        db.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS identity_verified BOOLEAN NOT NULL DEFAULT FALSE")
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS projects_user_created_idx
            ON projects(user_id, created_at)
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS projects_client_created_idx
            ON projects(client_id, created_at)
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS video_versions_project_created_idx
            ON video_versions(project_id, created_at)
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS comments_version_created_idx
            ON comments(video_version_id, created_at)
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS comments_open_feedback_idx
            ON comments(video_version_id, author_role, is_resolved, type)
            """
        )

        db.execute("""
            CREATE TABLE IF NOT EXISTS project_notification_reads (
                user_id INTEGER NOT NULL,
                project_id INTEGER NOT NULL,
                last_read_at TEXT NOT NULL,
                PRIMARY KEY(user_id, project_id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(project_id) REFERENCES projects(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS project_members (
                project_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                assigned_at TEXT NOT NULL,
                PRIMARY KEY(project_id, user_id),
                FOREIGN KEY(project_id) REFERENCES projects(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS project_members_user_idx ON project_members(user_id)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS project_attachments (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                original_name TEXT NOT NULL,
                storage_path TEXT,
                public_url TEXT,
                content_type TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                created_by_user_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id),
                FOREIGN KEY(created_by_user_id) REFERENCES users(id)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS project_attachments_project_idx ON project_attachments(project_id, created_at)")
        legacy_attachment_projects = db.execute(
            "SELECT id, notes, created_at FROM projects WHERE notes LIKE ?",
            ("%||%",),
        ).fetchall()
        for legacy_project in legacy_attachment_projects:
            parts = (legacy_project["notes"] or "").split("||")
            for legacy_attachment in parts[1:]:
                if "::" not in legacy_attachment:
                    continue
                title, public_url = legacy_attachment.split("::", 1)
                title = title.strip()
                public_url = public_url.strip()
                if not title or not public_url:
                    continue
                existing_attachment = db.execute(
                    """
                    SELECT id FROM project_attachments
                    WHERE project_id = ? AND title = ? AND public_url = ?
                    """,
                    (legacy_project["id"], title, public_url),
                ).fetchone()
                if not existing_attachment:
                    db.execute(
                        """
                        INSERT INTO project_attachments
                        (project_id, title, original_name, public_url, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            legacy_project["id"],
                            title,
                            title,
                            public_url,
                            legacy_project["created_at"],
                        ),
                    )
            db.execute(
                "UPDATE projects SET notes = ? WHERE id = ?",
                (parts[0].strip(), legacy_project["id"]),
            )
        db.execute("""
            CREATE TABLE IF NOT EXISTS project_lifecycle_events (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                actor_user_id INTEGER,
                actor_name TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id),
                FOREIGN KEY(actor_user_id) REFERENCES users(id)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS project_lifecycle_project_idx ON project_lifecycle_events(project_id, created_at)")


def mark_project_notifications_read(db, user_id: int, project_id: int) -> None:
    db.execute(
        """
        INSERT INTO project_notification_reads
        (user_id, project_id, last_read_at)
        VALUES (?, ?, ?)
        ON CONFLICT (user_id, project_id)
        DO UPDATE SET last_read_at = EXCLUDED.last_read_at
        """,
        (
            user_id,
            project_id,
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        ),
    )


def get_owner_notifications(db, user_id: int, limit: int = 8, reader_id: Optional[int] = None):
    reader_id = reader_id or user_id
    return db.execute(
        """
        WITH events AS (
            SELECT
                'comment-' || CAST(cm.id AS TEXT) AS notification_id,
                cm.author_name,
                cm.author_role,
                cm.body,
                cm.type,
                cm.created_at,
                vv.version_label,
                p.id AS project_id,
                p.name AS project_name,
                c.name AS client_name
            FROM comments cm
            JOIN video_versions vv ON cm.video_version_id = vv.id
            JOIN projects p ON vv.project_id = p.id
            JOIN clients c ON p.client_id = c.id
            WHERE p.user_id = ?

            UNION ALL

            SELECT
                'upload-' || CAST(vv.id AS TEXT) AS notification_id,
                COALESCE(created_by_name, 'Studio') AS author_name,
                'studio' AS author_role,
                'Uploaded ' || vv.version_label AS body,
                'upload' AS type,
                vv.created_at,
                vv.version_label,
                p.id AS project_id,
                p.name AS project_name,
                c.name AS client_name
            FROM video_versions vv
            JOIN projects p ON vv.project_id = p.id
            JOIN clients c ON p.client_id = c.id
            WHERE p.user_id = ?

            UNION ALL

            SELECT
                'project-' || CAST(p.id AS TEXT) AS notification_id,
                'System' AS author_name,
                'system' AS author_role,
                'Project Created' AS body,
                'create' AS type,
                p.created_at,
                '' AS version_label,
                p.id AS project_id,
                p.name AS project_name,
                c.name AS client_name
            FROM projects p
            JOIN clients c ON p.client_id = c.id
            WHERE p.user_id = ?
        )
        SELECT
            events.*,
            CASE
                WHEN reads.last_read_at IS NOT NULL
                    AND events.created_at <= reads.last_read_at
                THEN TRUE
                ELSE FALSE
            END AS is_read
        FROM events
        LEFT JOIN project_notification_reads reads
            ON reads.user_id = ?
            AND reads.project_id = events.project_id
        ORDER BY events.created_at DESC
        LIMIT ?
        """,
        (user_id, user_id, user_id, reader_id, limit),
    ).fetchall()


def seed_demo_review_history() -> None:
    if not DEMO_ENABLED:
        return

    now = datetime.utcnow()

    def add_comment(
        db,
        version_id: int,
        body: str,
        action_type: str,
        minutes_ago: int,
    ) -> None:
        existing = db.execute(
            """
            SELECT id
            FROM comments
            WHERE video_version_id = ?
              AND author_role = 'client'
              AND body = ?
              AND type = ?
            """,
            (version_id, body, action_type),
        ).fetchone()
        if existing:
            return

        db.execute(
            """
            INSERT INTO comments
            (video_version_id, author_role, author_name, body, type, created_at)
            VALUES (?, 'client', 'Acme Studio', ?, ?, ?)
            """,
            (
                version_id,
                body,
                action_type,
                (now - timedelta(minutes=minutes_ago)).isoformat(),
            ),
        )

    def add_lifecycle_event(
        db,
        project_id: int,
        owner_id: int,
        event_type: str,
        note: str,
        minutes_ago: int,
    ) -> None:
        existing = db.execute(
            """
            SELECT id FROM project_lifecycle_events
            WHERE project_id = ? AND event_type = ? AND note = ?
            """,
            (project_id, event_type, note),
        ).fetchone()
        if existing:
            return
        db.execute(
            """
            INSERT INTO project_lifecycle_events
            (project_id, event_type, actor_user_id, actor_name, note, created_at)
            VALUES (?, ?, ?, 'Lumaire Studio', ?, ?)
            """,
            (
                project_id,
                event_type,
                owner_id,
                note,
                (now - timedelta(minutes=minutes_ago)).isoformat(),
            ),
        )

    with get_db() as db:
        owner = db.execute(
            "SELECT id FROM users WHERE email = ? AND role = 'owner'",
            (DEMO_OWNER_EMAIL,),
        ).fetchone()
        client_user = db.execute(
            """
            SELECT id, client_reference_id
            FROM users
            WHERE email = ? AND role = 'client'
            """,
            (DEMO_CLIENT_EMAIL,),
        ).fetchone()
        if not owner or not client_user or not client_user["client_reference_id"]:
            return

        db.execute(
            "UPDATE users SET brand_color = ? WHERE id = ?",
            (DEFAULT_BRAND_COLOR, owner["id"]),
        )

        client = db.execute(
            """
            SELECT id
            FROM clients
            WHERE id = ? AND user_id = ?
            """,
            (client_user["client_reference_id"], owner["id"]),
        ).fetchone()
        if not client:
            return

        projects = db.execute(
            """
            SELECT id, name
            FROM projects
            WHERE user_id = ? AND client_id = ?
            """,
            (owner["id"], client["id"]),
        ).fetchall()
        projects_by_name = {row["name"]: row for row in projects}

        def versions_for(project_name: str):
            project = projects_by_name.get(project_name)
            if not project:
                return []
            return db.execute(
                """
                SELECT id, video_url
                FROM video_versions
                WHERE project_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (project["id"],),
            ).fetchall()

        launch_project = projects_by_name.get("Launch Reels Package")
        launch_versions = versions_for("Launch Reels Package")
        if launch_project and launch_versions:
            first_version = launch_versions[0]
            latest_version = launch_versions[-1]
            add_comment(
                db,
                first_version["id"],
                "Could we tighten the opening and bring the product shot in sooner?",
                "comment",
                520,
            )
            add_comment(
                db,
                first_version["id"],
                "Please revise the first cut with a faster opening and shorter end card.",
                "reject",
                500,
            )
            add_comment(
                db,
                latest_version["id"],
                "The revised opening is much stronger. Please keep the warmer grade.",
                "comment",
                150,
            )
            add_comment(
                db,
                latest_version["id"],
                "One final change: trim the end card by one second before approval.",
                "reject",
                130,
            )
            db.execute(
                """
                UPDATE comments
                SET is_resolved = TRUE, resolved_at = ?
                WHERE video_version_id = ? AND type = 'comment' AND body = ?
                """,
                (
                    (now - timedelta(minutes=430)).isoformat(),
                    first_version["id"],
                    "Could we tighten the opening and bring the product shot in sooner?",
                ),
            )
            db.execute(
                "UPDATE video_versions SET status = 'Revision Requested' WHERE id IN (?, ?)",
                (first_version["id"], latest_version["id"]),
            )
            db.execute(
                """
                UPDATE projects
                SET status = 'In Revision', review_due_at = ?,
                    guest_access = 'approve', review_link_enabled = TRUE,
                    review_allow_versions = TRUE, review_allow_download = TRUE,
                    review_token_expires_at = ?,
                    delivery_checklist = 'captions,thumbnail'
                WHERE id = ?
                """,
                (
                    (now + timedelta(days=3)).date().isoformat(),
                    (now + timedelta(days=14)).isoformat(),
                    launch_project["id"],
                ),
            )
            add_lifecycle_event(
                db,
                launch_project["id"],
                owner["id"],
                "reopened",
                "Review reopened after the first round of client feedback.",
                470,
            )

        completed_projects = [
            (
                "Product Teaser",
                "The pacing and product close-up both look good now.",
                "Approved for launch. Please use this cut as the final social master.",
                "Approved",
                310,
            ),
            (
                "Brand Film Master",
                "The new music balance works well and the logo timing feels right.",
                "Approved. This version is ready for final delivery.",
                "Published",
                390,
            ),
        ]
        for project_name, note, approval, project_status, minutes_ago in completed_projects:
            project = projects_by_name.get(project_name)
            versions = versions_for(project_name)
            if not project or not versions:
                continue
            latest_version = versions[-1]
            add_comment(db, latest_version["id"], note, "comment", minutes_ago)
            add_comment(
                db,
                latest_version["id"],
                approval,
                "approve",
                minutes_ago - 20,
            )
            db.execute(
                "UPDATE video_versions SET status = 'Approved' WHERE id = ?",
                (latest_version["id"],),
            )
            db.execute(
                "UPDATE projects SET status = ?, delivery_checklist = ? WHERE id = ?",
                (
                    project_status,
                    "master,captions,thumbnail,delivery_link"
                    if project_status == "Published"
                    else "master,captions,thumbnail",
                    project["id"],
                ),
            )
            if project_status == "Published":
                add_lifecycle_event(
                    db,
                    project["id"],
                    owner["id"],
                    "archived",
                    "Final delivery recorded.",
                    minutes_ago - 35,
                )

        summer_project = projects_by_name.get("Summer Campaign Cutdowns")
        summer_versions = versions_for("Summer Campaign Cutdowns")
        if summer_project and not summer_versions:
            reference_version = db.execute(
                """
                SELECT video_url
                FROM video_versions
                WHERE user_id = ? AND video_url <> ''
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (owner["id"],),
            ).fetchone()
            if reference_version:
                created = db.execute(
                    """
                    INSERT INTO video_versions
                    (user_id, project_id, version_label, video_url, status, notes, created_at)
                    VALUES (?, ?, 'V1', ?, 'Approved', ?, ?)
                    RETURNING id, video_url
                    """,
                    (
                        owner["id"],
                        summer_project["id"],
                        reference_version["video_url"],
                        "Final cutdowns with updated captions and safe-area spacing.",
                        (now - timedelta(hours=7)).isoformat(),
                    ),
                ).fetchone()
                summer_versions = [created]

        if summer_project and summer_versions:
            latest_version = summer_versions[-1]
            add_comment(
                db,
                latest_version["id"],
                "Captions and framing look correct across all cutdowns.",
                "comment",
                240,
            )
            add_comment(
                db,
                latest_version["id"],
                "Approved for delivery.",
                "approve",
                220,
            )
            db.execute(
                "UPDATE video_versions SET status = 'Approved' WHERE id = ?",
                (latest_version["id"],),
            )
            db.execute(
                """
                UPDATE projects
                SET status = 'Published',
                    delivery_checklist = 'master,captions,thumbnail,delivery_link'
                WHERE id = ?
                """,
                (summer_project["id"],),
            )
            add_lifecycle_event(
                db,
                summer_project["id"],
                owner["id"],
                "archived",
                "Approved package delivered with every checklist item complete.",
                200,
            )


def run_db_migrations() -> None:
    if not RUN_DB_MIGRATIONS:
        logger.warning("Automatic database migrations are disabled.")
        return
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required for database migrations.")

    from alembic import command
    from alembic.config import Config

    lock_connection = psycopg2.connect(DATABASE_URL, sslmode=DATABASE_SSLMODE)
    lock_connection.autocommit = True
    lock_cursor = lock_connection.cursor()
    try:
        lock_cursor.execute("SELECT pg_advisory_lock(1280134173)")
        project_root = Path(__file__).resolve().parent.parent
        config = Config(str(project_root / "alembic.ini"))
        config.set_main_option("script_location", str(project_root / "migrations"))
        config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        command.upgrade(config, "head")
    finally:
        lock_cursor.execute("SELECT pg_advisory_unlock(1280134173)")
        lock_cursor.close()
        lock_connection.close()


@app.on_event("startup")
def startup() -> None:
    run_db_migrations()
    init_db()
    try:
        seed_demo_review_history()
    except Exception as exc:
        logger.warning("Demo history seed skipped: %s", exc)

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return (
        f"pbkdf2_sha256${PASSWORD_ITERATIONS}$"
        f"{salt.hex()}${digest.hex()}"
    )


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False

    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iterations, salt_hex, expected_hex = stored.split("$", 3)
            actual = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                bytes.fromhex(salt_hex),
                int(iterations),
            )
            return hmac.compare_digest(actual.hex(), expected_hex)
        except (TypeError, ValueError):
            return False

    # Legacy salted SHA-256 hashes remain valid until the next successful login.
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    actual = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return hmac.compare_digest(actual, expected)


def password_needs_upgrade(stored: str) -> bool:
    if not stored.startswith("pbkdf2_sha256$"):
        return True
    try:
        return int(stored.split("$", 3)[1]) < PASSWORD_ITERATIONS
    except (IndexError, ValueError):
        return True


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def validate_csrf(request: Request, submitted_token: str) -> None:
    expected = request.session.get("csrf_token")
    if (
        not expected
        or not submitted_token
        or not hmac.compare_digest(expected, submitted_token)
    ):
        raise HTTPException(status_code=403, detail="Invalid or expired form token.")


def set_session_cookie(
    response: Response,
    request: Request,
    user_id: int,
    session_version: Optional[int] = None,
) -> None:
    if session_version is None:
        with get_db() as db:
            row = db.execute(
                "SELECT session_version FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        session_version = int(row["session_version"] or 1) if row else 1
    response.set_cookie(
        "session",
        serializer.dumps({"user_id": user_id, "version": session_version}),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=IS_PRODUCTION or request.url.scheme == "https",
    )


def review_token_is_valid(project: object, submitted_token: str) -> bool:
    try:
        if project["review_link_enabled"] is False:
            return False
    except (KeyError, IndexError):
        pass
    stored_token = project["review_token"]
    if (
        not stored_token
        or not submitted_token
        or not hmac.compare_digest(stored_token, submitted_token)
    ):
        return False

    expires_at = project["review_token_expires_at"]
    if not expires_at:
        return True
    try:
        expiration = datetime.fromisoformat(expires_at)
        now_utc = datetime.now(timezone.utc)
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=timezone.utc)
        return expiration >= now_utc
    except (TypeError, ValueError):
        return False


def has_review_password_grant(request: Request, review_token: str) -> bool:
    return review_token in request.session.get("review_password_grants", [])


def get_review_actor(
    user: Optional[object],
    project: object,
    submitted_token: str,
    action_type: str,
) -> tuple[str, str]:
    if not user:
        if not review_token_is_valid(project, submitted_token):
            raise HTTPException(
                status_code=403,
                detail="Review link invalid or expired.",
            )
        return "guest", "Guest reviewer"

    if user["role"] == "owner":
        owner_access = user["id"] == project["user_id"]
    elif user["role"] == "admin":
        owner_access = workspace_id_for(user) == project["user_id"]
    elif user["role"] == "member":
        with get_db() as db:
            owner_access = can_access_project(db, user, project)
    else:
        owner_access = False
    client_access = (
        user["role"] == "client"
        and user["client_reference_id"] == project["client_id"]
    )
    if not owner_access and not client_access:
        raise HTTPException(status_code=403, detail="Project access denied.")
    if is_studio_user(user) and owner_access and action_type in {"approve", "reject"}:
        raise HTTPException(
            status_code=403,
            detail="Only the assigned client can approve or reject a version.",
        )
    if client_access:
        return "client", project["client_name"]
    return "studio", studio_display_name(user)


async def read_upload_with_limit(file: UploadFile, max_bytes: int) -> bytes:
    content = bytearray()
    while True:
        chunk = await file.read(min(1024 * 1024, max_bytes + 1 - len(content)))
        if not chunk:
            return bytes(content)
        content.extend(chunk)
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail="Uploaded file is too large.")


def upload_signature_matches(content: bytes, extension: str) -> bool:
    if not content:
        return False
    signatures = {
        ".pdf": lambda data: data.startswith(b"%PDF-"),
        ".png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
        ".jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
        ".gif": lambda data: data.startswith((b"GIF87a", b"GIF89a")),
        ".webp": lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        ".zip": lambda data: data.startswith((b"PK\x03\x04", b"PK\x05\x06")),
        ".docx": lambda data: data.startswith((b"PK\x03\x04", b"PK\x05\x06")),
        ".doc": lambda data: data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
        ".txt": lambda data: b"\x00" not in data[:4096],
        ".mp4": lambda data: b"ftyp" in data[4:16],
        ".mov": lambda data: b"ftyp" in data[4:16],
        ".webm": lambda data: data.startswith(b"\x1a\x45\xdf\xa3"),
    }
    validator = signatures.get(extension.lower())
    return bool(validator and validator(content))


def storage_path_from_url(url: Optional[str], bucket: str) -> Optional[str]:
    """Recover an object path from a legacy Supabase public or signed URL."""
    if not url:
        return None
    try:
        parsed = urlparse(str(url).strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if SUPABASE_URL:
        expected = urlparse(SUPABASE_URL)
        if expected.netloc and parsed.netloc.lower() != expected.netloc.lower():
            return None
    markers = (
        f"/storage/v1/object/public/{bucket}/",
        f"/storage/v1/object/sign/{bucket}/",
        f"/storage/v1/object/authenticated/{bucket}/",
    )
    for marker in markers:
        if marker not in parsed.path:
            continue
        storage_path = unquote(parsed.path.split(marker, 1)[1]).strip("/")
        if storage_path and ".." not in storage_path.split("/"):
            return storage_path
    return None


def signed_storage_url(
    bucket: str,
    storage_path: Optional[str],
    fallback_url: Optional[str] = None,
    expires_in: int = 3600,
) -> str:
    if not storage_path or not SUPABASE_URL or not SUPABASE_KEY:
        return fallback_url or ""
    cache_key = (bucket, storage_path)
    now = time.monotonic()
    with _signed_url_cache_lock:
        cached = _signed_url_cache.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]
        if cached:
            _signed_url_cache.pop(cache_key, None)
    try:
        response = httpx.post(
            f"{SUPABASE_URL}/storage/v1/object/sign/{bucket}/{storage_path}",
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "apikey": SUPABASE_KEY,
            },
            json={"expiresIn": expires_in},
            timeout=10,
        )
        response.raise_for_status()
        signed_path = response.json().get("signedURL", "")
        if signed_path.startswith("http://") or signed_path.startswith("https://"):
            signed_url = signed_path
        elif signed_path:
            signed_url = f"{SUPABASE_URL}/storage/v1{signed_path}"
        else:
            signed_url = ""
        if signed_url:
            with _signed_url_cache_lock:
                if len(_signed_url_cache) >= 2_000:
                    expired_keys = [
                        key for key, value in _signed_url_cache.items()
                        if value[1] <= now
                    ]
                    for key in expired_keys:
                        _signed_url_cache.pop(key, None)
                    if len(_signed_url_cache) >= 2_000:
                        _signed_url_cache.pop(next(iter(_signed_url_cache)))
                _signed_url_cache[cache_key] = (
                    signed_url,
                    now + max(60, expires_in - 300),
                )
            return signed_url
    except Exception as exc:
        logger.warning("Could not sign storage object %s/%s: %s", bucket, storage_path, exc)
    return fallback_url or ""


def hydrate_comment_attachment_urls(comments: list[object]) -> list[dict]:
    hydrated: list[dict] = []
    for row in comments:
        item = dict(row)
        storage_path = item.get("attachment_storage_path") or storage_path_from_url(
            item.get("attachment_url"),
            "attachments",
        )
        item["attachment_url"] = signed_storage_url(
            "attachments",
            storage_path,
            None if ATTACHMENTS_BUCKET_PRIVATE else item.get("attachment_url"),
        )
        hydrated.append(item)
    return hydrated


def load_project_attachments(db, project: object) -> tuple[str, list[dict]]:
    raw_notes = project["notes"] or ""
    display_notes = raw_notes.split("||", 1)[0].strip()
    rows = db.execute(
        """
        SELECT id, title, original_name, storage_path, public_url,
               content_type, size_bytes, created_at
        FROM project_attachments
        WHERE project_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (project["id"],),
    ).fetchall()
    attachments = []
    for row in rows:
        item = dict(row)
        storage_path = item.get("storage_path") or storage_path_from_url(
            item.get("public_url"),
            "attachments",
        )
        item["url"] = signed_storage_url(
            "attachments",
            storage_path,
            None if ATTACHMENTS_BUCKET_PRIVATE else item.get("public_url"),
        )
        attachments.append(item)
    return display_notes, attachments


templates.env.globals["csrf_token"] = get_csrf_token

def get_user_from_session_token(token: Optional[str]) -> Optional[sqlite3.Row]:
    if not token:
        return None
    try:
        payload = serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if isinstance(payload, int):
        user_id = payload
        token_version = 1
    elif isinstance(payload, dict):
        user_id = payload.get("user_id")
        token_version = int(payload.get("version") or 1)
    else:
        return None
    if not isinstance(user_id, int):
        return None
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if (
        not user
        or not bool(user["is_active"])
        or int(user["session_version"] or 1) != token_version
    ):
        return None
    return user


def get_current_user(request: Request) -> Optional[sqlite3.Row]:
    return get_user_from_session_token(request.cookies.get("session"))


def require_user(request: Request) -> sqlite3.Row:
    user = get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=303,
            headers={"Location": "/login?error=session-expired"},
        )
    if bool(user["must_change_password"]) and request.url.path not in {
        "/account",
        "/account/password",
        "/logout",
    }:
        raise HTTPException(
            status_code=303,
            headers={"Location": "/account?required=password"},
        )
    return user


STUDIO_ROLES = {"owner", "admin", "member"}


def is_studio_user(user: Optional[object]) -> bool:
    return bool(user and user["role"] in STUDIO_ROLES)


def workspace_id_for(user: object) -> int:
    if user["role"] == "owner":
        return int(user["id"])
    return int(user["workspace_owner_id"] or user["id"])


def row_value(row: object, key: str, default=None):
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def effective_plan_for(owner: object, demo: bool = False) -> str:
    if demo:
        return "business"
    plan = str(row_value(owner, "subscription_plan", "free")).lower()
    status = str(row_value(owner, "subscription_status", "free")).lower()
    if plan not in {"pro", "business"} or status not in PAID_ACCESS_STATUSES:
        return "free"
    return plan


def workspace_subscription(db, user: object) -> dict:
    workspace_id = workspace_id_for(user)
    owner = db.execute("SELECT * FROM users WHERE id = ?", (workspace_id,)).fetchone()
    demo = is_demo_user(user)
    # Keep existing deployments fully usable until Stripe is intentionally enabled.
    plan_key = (
        effective_plan_for(owner, demo)
        if BILLING_ENABLED or demo
        else "business"
    )
    return {
        "workspace_id": workspace_id,
        "owner": owner,
        "plan_key": plan_key,
        "plan": PLAN_CATALOG[plan_key],
        "status": (
            "demo"
            if demo
            else str(row_value(owner, "subscription_status", "free"))
            if BILLING_ENABLED
            else "billing preview"
        ),
        "billing_enabled": BILLING_ENABLED,
        "stripe_customer_id": row_value(owner, "stripe_customer_id", ""),
        "stripe_subscription_id": row_value(owner, "stripe_subscription_id", ""),
        "has_paid_subscription": bool(
            row_value(owner, "stripe_subscription_id", "")
            and str(row_value(owner, "subscription_status", "")).lower()
            in PAID_ACCESS_STATUSES
        ),
        "current_period_end": row_value(owner, "subscription_current_period_end", ""),
    }


def workspace_usage(db, workspace_id: int) -> dict[str, int]:
    row = db.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM projects WHERE user_id = ? AND archived_at IS NULL) AS active_projects,
          (SELECT COUNT(*) FROM clients WHERE user_id = ? AND archived_at IS NULL) AS clients,
          (SELECT COUNT(*) FROM users WHERE is_active = TRUE AND (id = ? OR workspace_owner_id = ?)) AS seats
        """,
        (workspace_id, workspace_id, workspace_id, workspace_id),
    ).fetchone()
    return {
        "active_projects": int(row_value(row, "active_projects", 0)),
        "clients": int(row_value(row, "clients", 0)),
        "seats": int(row_value(row, "seats", 0)),
    }


def plan_allows_more(subscription: dict, resource: str, current: int) -> bool:
    limit = subscription["plan"].get(resource)
    return limit is None or current < int(limit)


def plan_limit_message(subscription: dict, resource_label: str) -> str:
    return (
        f"Your+{subscription['plan']['name']}+plan+has+reached+its+"
        f"{resource_label}+limit.+Review+plans+in+Billing."
    )


def plan_has_feature(subscription: dict, feature: str) -> bool:
    return feature in subscription["plan"]["features"]


def stripe_value(value: object, key: str, default=None):
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def plan_for_price_id(price_id: str) -> str:
    if price_id and price_id == STRIPE_BUSINESS_PRICE_ID:
        return "business"
    if price_id and price_id == STRIPE_PRO_PRICE_ID:
        return "pro"
    return "free"


def studio_display_name(user: object) -> str:
    try:
        display_name = user["display_name"]
    except (KeyError, IndexError):
        display_name = ""
    try:
        email_name = user["email"].split("@", 1)[0]
    except (KeyError, IndexError):
        try:
            email_name = user["studio_name"] or "Studio"
        except (KeyError, IndexError):
            email_name = "Studio"
    return (display_name or email_name).strip()


def can_access_project(db, user: object, project: object, manage: bool = False) -> bool:
    if user["role"] == "client":
        return not manage and user["client_reference_id"] == project["client_id"]
    if not is_studio_user(user) or workspace_id_for(user) != project["user_id"]:
        return False
    if user["role"] in {"owner", "admin"}:
        return True
    if manage:
        return False
    return bool(
        db.execute(
            "SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?",
            (project["id"], user["id"]),
        ).fetchone()
    )


def is_demo_email(email: Optional[str]) -> bool:
    if not DEMO_ENABLED or not email:
        return False
    return email.strip().lower() in {DEMO_OWNER_EMAIL, DEMO_CLIENT_EMAIL}


def is_demo_user(user: Optional[sqlite3.Row]) -> bool:
    return bool(user and is_demo_email(user["email"]))


def is_valid_email(value: str) -> bool:
    if (
        not value
        or len(value) > 254
        or value != value.strip()
        or " " in value
        or value.count("@") != 1
    ):
        return False
    local, separator, domain = value.partition("@")
    return bool(
        separator
        and local
        and domain
        and "." in domain
        and not domain.startswith(".")
        and not domain.endswith(".")
    )


def is_valid_hex_color(value: str) -> bool:
    if len(value) != 7 or not value.startswith("#"):
        return False
    return all(char in "0123456789abcdefABCDEF" for char in value[1:])


def normalize_guest_access(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in GUEST_ACCESS_OPTIONS else "comment"


def guest_action_is_allowed(access: str, action_type: str) -> bool:
    allowed_actions = {
        "view": set(),
        "comment": {"comment"},
        "approve": {"comment", "approve", "reject"},
    }
    return action_type in allowed_actions[normalize_guest_access(access)]


def normalize_annotation_data(value: str) -> Optional[str]:
    if not value or not value.strip():
        return None
    if len(value) > 100_000:
        raise HTTPException(status_code=400, detail="Annotation is too large.")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid annotation data.")
    strokes = payload.get("strokes") if isinstance(payload, dict) else None
    if not isinstance(strokes, list) or len(strokes) > 200:
        raise HTTPException(status_code=400, detail="Invalid annotation data.")
    normalized_strokes = []
    for stroke in strokes:
        if not isinstance(stroke, list) or len(stroke) > 2_000:
            raise HTTPException(status_code=400, detail="Invalid annotation data.")
        normalized_stroke = []
        for point in stroke:
            if not isinstance(point, dict):
                raise HTTPException(status_code=400, detail="Invalid annotation data.")
            x, y = point.get("x"), point.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                raise HTTPException(status_code=400, detail="Invalid annotation data.")
            normalized_stroke.append({"x": max(0, min(1, x)), "y": max(0, min(1, y))})
        normalized_strokes.append(normalized_stroke)
    return json.dumps({"strokes": normalized_strokes}, separators=(",", ":"))


def normalize_review_due_at(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return ""
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid review due date.")


def parse_delivery_checklist(value: str) -> set[str]:
    valid_keys = {key for key, _ in DELIVERY_CHECKLIST_ITEMS}
    return {
        item.strip()
        for item in (value or "").split(",")
        if item.strip() in valid_keys
    }


def record_project_lifecycle(
    db,
    project_id: int,
    event_type: str,
    user: object,
    note: str = "",
) -> None:
    db.execute(
        """
        INSERT INTO project_lifecycle_events
        (project_id, event_type, actor_user_id, actor_name, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            event_type,
            user["id"],
            studio_display_name(user),
            note.strip()[:500],
            datetime.utcnow().isoformat(),
        ),
    )


def normalize_branding(row: Optional[sqlite3.Row]) -> dict:
    studio_name = DEFAULT_STUDIO_NAME
    brand_color = DEFAULT_BRAND_COLOR
    logo_url = ""
    email_sender_name = DEFAULT_EMAIL_SENDER_NAME
    setup_completed = False

    if row:
        studio_name = (row["studio_name"] or DEFAULT_STUDIO_NAME).strip() or DEFAULT_STUDIO_NAME
        brand_color = (row["brand_color"] or DEFAULT_BRAND_COLOR).strip()
        logo_url = (row["logo_url"] or "").strip()
        email_sender_name = (row["email_sender_name"] or studio_name or DEFAULT_EMAIL_SENDER_NAME).strip()
        setup_completed = bool(row["setup_completed"])

    if not is_valid_hex_color(brand_color):
        brand_color = DEFAULT_BRAND_COLOR

    return {
        "studio_name": studio_name,
        "brand_color": brand_color,
        "logo_url": logo_url,
        "email_sender_name": email_sender_name,
        "setup_completed": setup_completed,
    }

def get_owner_branding(db, owner_id: int) -> dict:
    row = db.execute(
        "SELECT studio_name, brand_color, logo_url, email_sender_name, setup_completed FROM users WHERE id = ?",
        (owner_id,),
    ).fetchone()
    return normalize_branding(row)

def get_branding_for_user(db, user: sqlite3.Row) -> dict:
    if user["role"] == "client":
        owner = db.execute(
            """
            SELECT u.studio_name, u.brand_color, u.logo_url, u.email_sender_name, u.setup_completed
            FROM clients c
            JOIN users u ON c.user_id = u.id
            WHERE c.id = ?
            """,
            (user["client_reference_id"],),
        ).fetchone()
        return normalize_branding(owner)

    if user["role"] in {"admin", "member"}:
        return get_owner_branding(db, workspace_id_for(user))
    return normalize_branding(user)

def owner_needs_setup(user: sqlite3.Row) -> bool:
    return user["role"] == "owner" and not bool(user["setup_completed"])

def post_login_path(user: sqlite3.Row) -> str:
    if bool(user["must_change_password"]):
        return "/account?required=password"
    return "/setup" if owner_needs_setup(user) else "/dashboard"

def sanitize_optional_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("https://") or value.startswith("http://"):
        return value[:500]
    return ""

def save_studio_settings(
    db,
    user_id: int,
    studio_name: str,
    brand_color: str,
    logo_url: str,
    email_sender_name: str,
    setup_completed: bool,
) -> Optional[str]:
    studio_name = studio_name.strip()[:60] or DEFAULT_STUDIO_NAME
    brand_color = brand_color.strip()
    logo_url = sanitize_optional_url(logo_url)
    email_sender_name = (
        email_sender_name.strip().replace("<", "").replace(">", "")[:60]
        or studio_name
    )

    if not is_valid_hex_color(brand_color):
        return "Brand color must be a valid hex color."

    db.execute(
        """
        UPDATE users
        SET studio_name = ?,
            brand_color = ?,
            logo_url = ?,
            email_sender_name = ?,
            setup_completed = ?
        WHERE id = ?
        """,
        (studio_name, brand_color, logo_url, email_sender_name, setup_completed, user_id),
    )

    return None

def redirect(path: str):
    return RedirectResponse(path, status_code=303)


def demo_read_only_redirect(path: str):
    separator = "&" if "?" in path else "?"
    return redirect(f"{path}{separator}error=Shared+demo+is+read-only.")


ERROR_PAGE_CONTENT = {
    400: (
        "Request needs another look",
        "We could not complete that request. Check the information and try again.",
    ),
    401: (
        "Sign in required",
        "Your session may have expired. Sign in again to continue.",
    ),
    403: (
        "This area is out of reach",
        "You do not have access to this workspace or action.",
    ),
    404: (
        "That page is not here",
        "The link may be outdated, or the item may no longer be available.",
    ),
    410: (
        "This link has expired",
        "Request a new link from the studio and try again.",
    ),
    413: (
        "That file is too large",
        "Choose a smaller file and upload it again.",
    ),
    500: (
        "Something interrupted the flow",
        "The workspace hit an unexpected problem. Please try again shortly.",
    ),
}


def request_prefers_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "").lower()


def render_error_page(request: Request, status_code: int):
    headline, message = ERROR_PAGE_CONTENT.get(
        status_code,
        ERROR_PAGE_CONTENT[500 if status_code >= 500 else 400],
    )
    try:
        user = get_current_user(request)
    except Exception:
        user = None

    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "user": user,
            "is_demo": is_demo_user(user),
            "demo_enabled": DEMO_ENABLED,
            "status_code": status_code,
            "headline": headline,
            "message": message,
            "primary_href": "/dashboard" if user else "/",
            "primary_label": "Back to dashboard" if user else "Return home",
        },
        status_code=status_code,
    )


@app.exception_handler(StarletteHTTPException)
async def browser_http_exception(
    request: Request,
    exc: StarletteHTTPException,
):
    location = (exc.headers or {}).get("Location")
    if 300 <= exc.status_code < 400 and location:
        return RedirectResponse(location, status_code=exc.status_code)
    if request_prefers_html(request):
        return render_error_page(request, exc.status_code)
    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def browser_server_exception(request: Request, exc: Exception):
    logger.error(
        "Unhandled request error",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    if request_prefers_html(request):
        return render_error_page(request, 500)
    return JSONResponse(
        {"detail": "Internal Server Error"},
        status_code=500,
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = get_current_user(request)
    if user:
        return redirect("/dashboard")
    return templates.TemplateResponse(
        "landing.html",
        {"request": request, "user": None, "demo_enabled": DEMO_ENABLED},
    )

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return redirect("/dashboard")
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "mode": "login",
            "error": None,
            "user": None,
            "demo_enabled": DEMO_ENABLED,
        },
    )

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "mode": "register",
            "error": None,
            "user": None,
            "demo_enabled": DEMO_ENABLED,
        },
    )

@app.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    if len(password) < 8:
        return redirect("/register?error=password-too-short")
    try:
        with get_db() as db:
            verification_token = secrets.token_urlsafe(32)

            cur = db.execute(
                """
                INSERT INTO users
                (
                    email,
                    password_hash,
                    role,
                    is_verified,
                    verification_token,
                    created_at
                )
                VALUES (?, ?, 'owner', FALSE, ?, ?)
                RETURNING id
                """,
                (
                    email.strip().lower(),
                    hash_password(password),
                    verification_token,
                    datetime.utcnow().isoformat()
                ),
            )
            user_id = cur.fetchone()["id"]
            verify_url = f"{request.base_url}verify-email/{verification_token}"
            if not send_verification_email(email.strip().lower(), verify_url):
                raise InvitationDeliveryError
    except psycopg2.IntegrityError:
        return redirect("/register?error=email-exists")
    except InvitationDeliveryError:
        return redirect("/register?error=verification-email-failed")

    return redirect("/login?success=verification-sent")


@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    email_clean = email.strip().lower()
    enforce_rate_limit(request, "login", email_clean, LOGIN_RATE_LIMIT)
    logger.info("Login attempt")
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE email = ?", (email_clean,)).fetchone()
    
    if not user or not bool(user["is_active"]):
        return RedirectResponse(url="/login?error=no_account", status_code=303)
    
    if not user["is_verified"]:
        return RedirectResponse(
            url="/login?error=email-not-verified",
            status_code=303
        )   
        
    if not verify_password(password, user["password_hash"]):
        return RedirectResponse(url="/login?error=wrong_password", status_code=303)

    if password_needs_upgrade(user["password_hash"]):
        with get_db() as db:
            db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(password), user["id"]),
            )

    clear_rate_limit(request, "login", email_clean)
    
    response = RedirectResponse(url=post_login_path(user), status_code=303)
    set_session_cookie(response, request, user["id"])
    response.delete_cookie("return_session")
    return response


@app.post("/demo-login/{role}")
def demo_login(request: Request, role: str, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    if not DEMO_ENABLED:
        raise HTTPException(status_code=404)

    demo_accounts = {
        "owner": DEMO_OWNER_EMAIL,
        "client": DEMO_CLIENT_EMAIL,
    }
    email = demo_accounts.get(role)
    if not email:
        raise HTTPException(status_code=404)

    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE email = ? AND role = ?",
            (email, role),
        ).fetchone()

    if not user or not bool(user["is_active"]) or not user["is_verified"]:
        return redirect("/login?error=demo-unavailable")

    current_user = get_current_user(request)
    current_session = request.cookies.get("session")
    response = redirect(post_login_path(user))
    if current_user and not is_demo_user(current_user) and current_session:
        response.set_cookie(
            "return_session",
            current_session,
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=IS_PRODUCTION or request.url.scheme == "https",
        )
    set_session_cookie(response, request, user["id"])
    return response

@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return templates.TemplateResponse(
        "forgot_password.html",
        {"request": request, "user": None, "demo_enabled": DEMO_ENABLED},
    )

@app.post("/forgot-password")
def forgot_password(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    email_clean = email.strip().lower()
    enforce_rate_limit(
        request,
        "forgot-password",
        email_clean,
        PASSWORD_RESET_RATE_LIMIT,
    )
    if is_demo_email(email_clean):
        return redirect("/forgot-password?success=reset-link-sent")

    reset_token = secrets.token_urlsafe(32)
    expires_at = (datetime.utcnow() + timedelta(hours=1)).isoformat()

    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE email = ?",
            (email_clean,),
        ).fetchone()

        if user and bool(user["is_active"]):
            db.execute(
                """
                UPDATE users
                SET reset_token = ?, reset_token_expires_at = ?
                WHERE id = ?
                """,
                (reset_token, expires_at, user["id"]),
            )

            reset_url = f"{request.base_url}reset-password/{reset_token}"
            background_tasks.add_task(
                send_password_reset_email,
                email_clean,
                reset_url,
            )

    return redirect("/forgot-password?success=reset-link-sent")

@app.get("/reset-password/{token}", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str):
    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE reset_token = ?",
            (token,),
        ).fetchone()

    token_valid = False
    if user and bool(user["is_active"]) and user["reset_token_expires_at"]:
        try:
            expires_at = datetime.fromisoformat(user["reset_token_expires_at"])
            token_valid = expires_at >= datetime.utcnow()
        except ValueError:
            token_valid = False

    if not token_valid:
        return templates.TemplateResponse(
            "reset_password.html",
            {
                "request": request,
                "token": token,
                "token_valid": False,
                "user": None,
                "demo_enabled": DEMO_ENABLED,
            },
        )

    return templates.TemplateResponse(
        "reset_password.html",
        {
            "request": request,
            "token": token,
            "token_valid": True,
            "user": None,
            "demo_enabled": DEMO_ENABLED,
        },
    )

@app.post("/reset-password/{token}")
def reset_password(
    request: Request,
    token: str,
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    if len(password) < 8:
        return redirect(f"/reset-password/{token}?error=password-too-short")

    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE reset_token = ?",
            (token,),
        ).fetchone()

        if (
            not user
            or not bool(user["is_active"])
            or not user["reset_token_expires_at"]
        ):
            return redirect(f"/reset-password/{token}?error=invalid-or-expired")

        if is_demo_email(user["email"]):
            return redirect(f"/reset-password/{token}?error=invalid-or-expired")

        try:
            expires_at = datetime.fromisoformat(user["reset_token_expires_at"])
        except ValueError:
            return redirect(f"/reset-password/{token}?error=invalid-or-expired")

        if expires_at < datetime.utcnow():
            return redirect(f"/reset-password/{token}?error=invalid-or-expired")

        db.execute(
            """
            UPDATE users
            SET password_hash = ?,
                reset_token = NULL,
                reset_token_expires_at = NULL,
                is_verified = TRUE,
                must_change_password = FALSE,
                session_version = session_version + 1
            WHERE id = ?
            """,
            (hash_password(password), user["id"]),
        )

    return redirect("/login?success=password-reset")

@app.get("/login/google")
async def login_google(request: Request):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return redirect("/login?error=google-unavailable")

    redirect_uri = request.url_for("auth_google_callback")

    return await oauth.google.authorize_redirect(
        request,
        redirect_uri
    )

@app.get("/auth/google/callback")
async def auth_google_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as exc:
        logger.warning("Google sign-in callback failed: %s", exc)
        return RedirectResponse(
            url="/login?error=google_login_failed",
            status_code=303,
        )
    user_info = token.get("userinfo")

    if not user_info or not user_info.get("email"):
        return RedirectResponse(url="/login?error=google_login_failed", status_code=303)

    email = user_info["email"].strip().lower()

    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        if user and not bool(user["is_active"]):
            return RedirectResponse(url="/login?error=no_account", status_code=303)

        if not user:
            db.execute(
                """
                INSERT INTO users
                (
                    email,
                    password_hash,
                    role,
                    is_verified,
                    created_at
                )
                VALUES (?, '', 'owner', TRUE, ?)
                """,
                (email, datetime.utcnow().isoformat())
            )

            user = db.execute(
                "SELECT * FROM users WHERE email = ?",
                (email,)
            ).fetchone()

    response = RedirectResponse(url=post_login_path(user), status_code=303)
    set_session_cookie(response, request, user["id"])
    response.delete_cookie("return_session")

    return response

@app.get("/verify-email/{token}")
def verify_email(token: str):

    with get_db() as db:

        user = db.execute(
            """
            SELECT *
            FROM users
            WHERE verification_token = ?
            """,
            (token,)
        ).fetchone()

        if not user or not bool(user["is_active"]):
            return redirect("/login?error=invalid-verification-link")

        db.execute(
            """
            UPDATE users
            SET
                is_verified = TRUE,
                verification_token = NULL
            WHERE id = ?
            """,
            (user["id"],)
        )

    return redirect("/login?success=email-verified")

@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    current_user = get_current_user(request)
    return_token = request.cookies.get("return_session")
    return_user = get_user_from_session_token(return_token)

    if (
        is_demo_user(current_user)
        and return_user
        and not is_demo_user(return_user)
        and return_token
    ):
        response = redirect(post_login_path(return_user))
        set_session_cookie(response, request, return_user["id"])
        response.delete_cookie("return_session")
        return response

    response = redirect("/")
    response.delete_cookie("session")
    response.delete_cookie("return_session")
    return response

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "all", category: str = "all", client_id: str = "all"):
    user = require_user(request)

    if owner_needs_setup(user):
        return redirect("/setup")
    
    with get_db() as db:
        if user["role"] == "client":
            # 客戶視角：只能看自己的專案
            clients = db.execute("SELECT * FROM clients WHERE id = ?", (user["client_reference_id"],)).fetchall()
            query = """
                SELECT p.*, c.name AS client_name,
                    (SELECT COUNT(*) FROM comments cm JOIN video_versions vv ON cm.video_version_id = vv.id
                     WHERE vv.project_id = p.id
                     AND p.status NOT IN ('Approved', 'Published')
                     AND cm.author_role IN ('client', 'guest')
                     AND cm.is_resolved = FALSE
                     AND (cm.type = 'comment' OR cm.type LIKE 'timestamp_%%')) AS unresolved_count
                FROM projects p
                JOIN clients c ON p.client_id = c.id
                WHERE p.client_id = ? AND p.archived_at IS NULL
            """
            params = [user["client_reference_id"]]
        else:
            # 工作室老闆視角：看所有
            workspace_id = workspace_id_for(user)
            clients = db.execute("SELECT * FROM clients WHERE user_id = ? AND archived_at IS NULL ORDER BY created_at DESC", (workspace_id,)).fetchall()
            query = """
                SELECT p.*, c.name AS client_name,
                    (SELECT COUNT(*) FROM comments cm JOIN video_versions vv ON cm.video_version_id = vv.id
                     WHERE vv.project_id = p.id
                     AND p.status NOT IN ('Approved', 'Published')
                     AND cm.author_role IN ('client', 'guest')
                     AND cm.is_resolved = FALSE
                     AND (cm.type = 'comment' OR cm.type LIKE 'timestamp_%%')) AS unresolved_count
                FROM projects p
                JOIN clients c ON p.client_id = c.id
                WHERE p.user_id = ?
            """
            params = [workspace_id]
            if user["role"] == "member":
                query += " AND EXISTS (SELECT 1 FROM project_members pm WHERE pm.project_id = p.id AND pm.user_id = ?)"
                params.append(user["id"])

        if status != "all":
            query += " AND p.status = ?"
            params.append(status)
        if category != "all":
            query += " AND p.category = ?"
            params.append(category)
        if client_id != "all" and user["role"] != "client":
            query += " AND p.client_id = ?"
            params.append(client_id)
            
        query += " ORDER BY p.created_at DESC"
        projects = db.execute(query, params).fetchall()

        if user["role"] == "client":
            notifications = []
        elif user["role"] == "member":
            assigned_ids = {
                row["project_id"]
                for row in db.execute(
                    "SELECT project_id FROM project_members WHERE user_id = ?",
                    (user["id"],),
                ).fetchall()
            }
            notifications = [
                item
                for item in get_owner_notifications(
                    db,
                    workspace_id_for(user),
                    limit=80,
                    reader_id=user["id"],
                )
                if item["project_id"] in assigned_ids
            ][:8]
        else:
            notifications = get_owner_notifications(
                db,
                workspace_id_for(user),
                reader_id=user["id"],
            )
        
        # 統計數據卡片
        target_id = user["client_reference_id"] if user["role"] == "client" else workspace_id_for(user)
        col = "client_id" if user["role"] == "client" else "user_id"
        stats_member_filter = ""
        stats_params = [target_id]
        if user["role"] == "member":
            stats_member_filter = " AND EXISTS (SELECT 1 FROM project_members pm WHERE pm.project_id = projects.id AND pm.user_id = ?)"
            stats_params.append(user["id"])
        stats = db.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='Awaiting Review' THEN 1 ELSE 0 END) AS awaiting,
                SUM(CASE WHEN status='In Revision' THEN 1 ELSE 0 END) AS revision,
                SUM(CASE WHEN status='Approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN status='Published' THEN 1 ELSE 0 END) AS published
            FROM projects WHERE {col} = ? AND archived_at IS NULL {stats_member_filter}
            """,
            stats_params,
        ).fetchone()
        branding = get_branding_for_user(db, user)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "clients": clients,
            "projects": projects,
            "stats": stats,
            "branding": branding,
            "notifications": notifications,
            "status_options": STATUS_OPTIONS,
            "category_options": CATEGORY_OPTIONS,
            "selected_status": status,
            "selected_category": category,
            "selected_client_id": client_id,
            "is_demo": is_demo_user(user),
        },
    )


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request):
    user = require_user(request)

    if owner_needs_setup(user):
        return redirect("/setup")

    if user["role"] not in {"owner", "admin"}:
        return redirect("/dashboard")

    user_id = workspace_id_for(user)

    with get_db() as db:
        totals = db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM clients WHERE user_id = ? AND archived_at IS NULL) AS total_clients,
                (SELECT COUNT(*) FROM projects WHERE user_id = ? AND archived_at IS NULL) AS total_projects,
                (
                    SELECT COUNT(*) FROM video_versions vv
                    JOIN projects p ON vv.project_id = p.id
                    WHERE vv.user_id = ? AND p.archived_at IS NULL
                ) AS total_versions,
                (
                    SELECT COUNT(*)
                    FROM comments cm
                    JOIN video_versions vv ON cm.video_version_id = vv.id
                    JOIN projects p ON vv.project_id = p.id
                    WHERE p.user_id = ? AND p.archived_at IS NULL
                ) AS total_comments,
                (
                    SELECT COUNT(*)
                    FROM projects
                    WHERE user_id = ? AND archived_at IS NULL AND status IN ('Approved', 'Published')
                ) AS completed_projects,
                (
                    SELECT COUNT(*)
                    FROM comments cm
                    JOIN video_versions vv ON cm.video_version_id = vv.id
                    JOIN projects p ON vv.project_id = p.id
                    WHERE p.user_id = ? AND p.archived_at IS NULL AND cm.type IN ('approve', 'reject')
                ) AS decision_count
            """,
            (user_id, user_id, user_id, user_id, user_id, user_id),
        ).fetchone()

        status_rows = db.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM projects
            WHERE user_id = ? AND archived_at IS NULL
            GROUP BY status
            ORDER BY count DESC, status ASC
            """,
            (user_id,),
        ).fetchall()

        category_rows = db.execute(
            """
            SELECT category, COUNT(*) AS count
            FROM projects
            WHERE user_id = ? AND archived_at IS NULL
            GROUP BY category
            ORDER BY count DESC, category ASC
            """,
            (user_id,),
        ).fetchall()

        top_clients = db.execute(
            """
            SELECT
                c.id,
                c.name,
                COUNT(DISTINCT p.id) AS project_count,
                COUNT(DISTINCT vv.id) AS version_count,
                COUNT(DISTINCT cm.id) AS comment_count
            FROM clients c
            LEFT JOIN projects p ON p.client_id = c.id
            LEFT JOIN video_versions vv ON vv.project_id = p.id
            LEFT JOIN comments cm ON cm.video_version_id = vv.id
            WHERE c.user_id = ? AND c.archived_at IS NULL
            GROUP BY c.id, c.name
            ORDER BY project_count DESC, comment_count DESC, c.name ASC
            LIMIT 5
            """,
            (user_id,),
        ).fetchall()

        recent_projects = db.execute(
            """
            SELECT
                p.id,
                p.name,
                p.status,
                p.category,
                p.created_at,
                c.name AS client_name,
                COUNT(DISTINCT vv.id) AS version_count,
                COUNT(DISTINCT cm.id) AS comment_count
            FROM projects p
            JOIN clients c ON p.client_id = c.id
            LEFT JOIN video_versions vv ON vv.project_id = p.id
            LEFT JOIN comments cm ON cm.video_version_id = vv.id
            WHERE p.user_id = ? AND p.archived_at IS NULL
            GROUP BY p.id, p.name, p.status, p.category, p.created_at, c.name
            ORDER BY p.created_at DESC
            LIMIT 6
            """,
            (user_id,),
        ).fetchall()

        branding = get_branding_for_user(db, user)

    total_projects = totals["total_projects"] or 0
    total_versions = totals["total_versions"] or 0
    total_comments = totals["total_comments"] or 0
    completed_projects = totals["completed_projects"] or 0

    analytics = {
        "total_clients": totals["total_clients"] or 0,
        "total_projects": total_projects,
        "total_versions": total_versions,
        "total_comments": total_comments,
        "completed_projects": completed_projects,
        "decision_count": totals["decision_count"] or 0,
        "approval_rate": round((completed_projects / total_projects) * 100) if total_projects else 0,
        "avg_versions_per_project": round(total_versions / total_projects, 1) if total_projects else 0,
        "avg_comments_per_project": round(total_comments / total_projects, 1) if total_projects else 0,
    }

    return templates.TemplateResponse(
        "analytics.html",
        {
            "request": request,
            "user": user,
            "branding": branding,
            "analytics": analytics,
            "status_rows": status_rows,
            "category_rows": category_rows,
            "top_clients": top_clients,
            "recent_projects": recent_projects,
            "is_demo": is_demo_user(user),
        },
    )


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    user = require_user(request)

    if user["role"] not in {"owner", "admin"}:
        return redirect("/dashboard")

    return templates.TemplateResponse(
        "setup.html",
        {
            "request": request,
            "user": user,
            "branding": normalize_branding(user),
            "error": request.query_params.get("error"),
            "is_demo": is_demo_user(user),
        },
    )


@app.post("/setup")
def complete_setup(
    request: Request,
    studio_name: str = Form(""),
    brand_color: str = Form(DEFAULT_BRAND_COLOR),
    logo_url: str = Form(""),
    email_sender_name: str = Form(""),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)

    if user["role"] != "owner":
        return redirect("/dashboard")

    if is_demo_user(user):
        return demo_read_only_redirect("/setup")

    with get_db() as db:
        error = save_studio_settings(
            db,
            user["id"],
            studio_name,
            brand_color,
            logo_url,
            email_sender_name,
            True,
        )

    if error:
        return redirect(f"/setup?error={error.replace(' ', '+')}")

    return redirect("/dashboard?success=Studio+setup+complete.")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user = require_user(request)

    if user["role"] != "owner":
        return redirect("/dashboard")

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": user,
            "branding": normalize_branding(user),
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success"),
            "is_demo": is_demo_user(user),
            "email_test_mode": bool(EMAIL_TEST_RECIPIENT) or EMAIL_FROM_ADDRESS == "onboarding@resend.dev",
            "email_test_recipient": EMAIL_TEST_RECIPIENT,
        },
    )


@app.post("/settings")
def update_settings(
    request: Request,
    studio_name: str = Form(""),
    brand_color: str = Form(DEFAULT_BRAND_COLOR),
    logo_url: str = Form(""),
    email_sender_name: str = Form(""),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)

    if user["role"] != "owner":
        raise HTTPException(status_code=403, detail="Only studio owners can update settings.")

    if is_demo_user(user):
        return demo_read_only_redirect("/settings")

    with get_db() as db:
        error = save_studio_settings(
            db,
            user["id"],
            studio_name,
            brand_color,
            logo_url,
            email_sender_name,
            True,
        )

    if error:
        return redirect(f"/settings?error={error.replace(' ', '+')}")

    return redirect("/settings?success=Settings+updated.")


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    user = require_user(request)
    if is_demo_user(user):
        return redirect("/dashboard")

    errors = {
        "invalid-email": "Enter a valid email address.",
        "email-in-use": "That email address is already connected to another account.",
        "current-password": "Your current password is incorrect.",
        "password-length": "Your new password must contain at least 8 characters.",
        "password-mismatch": "The new passwords do not match.",
        "password-unavailable": "Password changes are unavailable for this sign-in method.",
    }
    successes = {
        "email-updated": "Account email updated.",
        "password-updated": "Password updated.",
        "sessions-revoked": "Other signed-in sessions were revoked.",
    }

    with get_db() as db:
        branding = get_branding_for_user(db, user)

    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "user": user,
            "branding": branding,
            "is_demo": False,
            "error": errors.get(request.query_params.get("error", "")),
            "success": successes.get(request.query_params.get("success", "")),
            "has_password": bool(user["password_hash"]),
        },
    )


@app.post("/account/email")
def update_account_email(
    request: Request,
    email: str = Form(""),
    current_password: str = Form(""),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if is_demo_user(user):
        return demo_read_only_redirect("/dashboard")
    if not user["password_hash"] or not verify_password(
        current_password,
        user["password_hash"],
    ):
        return redirect("/account?error=current-password")

    email_clean = email.strip().lower()
    if not is_valid_email(email_clean):
        return redirect("/account?error=invalid-email")
    if email_clean == user["email"].strip().lower():
        return redirect("/account?success=email-updated")

    with get_db() as db:
        existing = db.execute(
            "SELECT id FROM users WHERE email = ? AND id <> ?",
            (email_clean, user["id"]),
        ).fetchone()
        if existing:
            return redirect("/account?error=email-in-use")

        db.execute(
            "UPDATE users SET email = ?, session_version = session_version + 1 WHERE id = ?",
            (email_clean, user["id"]),
        )
        if user["role"] == "client" and user["client_reference_id"]:
            db.execute(
                "UPDATE clients SET email = ? WHERE id = ?",
                (email_clean, user["client_reference_id"]),
            )
        refreshed = db.execute(
            "SELECT session_version FROM users WHERE id = ?", (user["id"],)
        ).fetchone()

    response = redirect("/account?success=email-updated")
    set_session_cookie(
        response,
        request,
        user["id"],
        int(refreshed["session_version"]),
    )
    return response


@app.post("/account/password")
def update_account_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if is_demo_user(user):
        return demo_read_only_redirect("/dashboard")
    if not user["password_hash"]:
        return redirect("/account?error=password-unavailable")
    if not verify_password(current_password, user["password_hash"]):
        return redirect("/account?error=current-password")
    if len(new_password) < 8:
        return redirect("/account?error=password-length")
    if new_password != confirm_password:
        return redirect("/account?error=password-mismatch")

    with get_db() as db:
        db.execute(
            """
            UPDATE users
            SET password_hash = ?,
                reset_token = NULL,
                reset_token_expires_at = NULL,
                must_change_password = FALSE,
                session_version = session_version + 1
            WHERE id = ?
            """,
            (hash_password(new_password), user["id"]),
        )
        refreshed = db.execute(
            "SELECT session_version FROM users WHERE id = ?", (user["id"],)
        ).fetchone()

    response = redirect("/account?success=password-updated")
    set_session_cookie(
        response,
        request,
        user["id"],
        int(refreshed["session_version"]),
    )
    return response


@app.post("/account/sessions/revoke")
def revoke_account_sessions(request: Request, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if is_demo_user(user):
        return demo_read_only_redirect("/dashboard")

    with get_db() as db:
        db.execute(
            "UPDATE users SET session_version = session_version + 1 WHERE id = ?",
            (user["id"],),
        )
        refreshed = db.execute(
            "SELECT session_version FROM users WHERE id = ?", (user["id"],)
        ).fetchone()

    response = redirect("/account?success=sessions-revoked")
    set_session_cookie(
        response,
        request,
        user["id"],
        int(refreshed["session_version"]),
    )
    return response


def sync_stripe_subscription(subscription: object) -> None:
    subscription_id = str(stripe_value(subscription, "id", ""))
    customer = stripe_value(subscription, "customer", "")
    customer_id = str(stripe_value(customer, "id", customer) or "")
    status = str(stripe_value(subscription, "status", "canceled") or "canceled")
    metadata = stripe_value(subscription, "metadata", {}) or {}
    workspace_id = stripe_value(metadata, "workspace_id", "")
    items = stripe_value(stripe_value(subscription, "items", {}), "data", []) or []
    first_item = items[0] if items else {}
    price = stripe_value(first_item, "price", {})
    price_id = str(stripe_value(price, "id", "") or "")
    plan_key = plan_for_price_id(price_id)
    period_end = stripe_value(subscription, "current_period_end", None)
    period_end_iso = (
        datetime.fromtimestamp(int(period_end), tz=timezone.utc).isoformat()
        if period_end
        else None
    )

    with get_db() as db:
        owner = None
        if str(workspace_id).isdigit():
            owner = db.execute(
                "SELECT id, subscription_plan FROM users WHERE id = ? AND role = 'owner'",
                (int(workspace_id),),
            ).fetchone()
        if not owner and customer_id:
            owner = db.execute(
                "SELECT id, subscription_plan FROM users WHERE stripe_customer_id = ? AND role = 'owner'",
                (customer_id,),
            ).fetchone()
        if not owner:
            logger.warning("Stripe subscription could not be matched to a workspace: %s", subscription_id)
            return
        if plan_key == "free" and status != "canceled":
            plan_key = str(row_value(owner, "subscription_plan", "free"))
        db.execute(
            """
            UPDATE users
            SET subscription_plan = ?, subscription_status = ?,
                stripe_customer_id = ?, stripe_subscription_id = ?,
                stripe_price_id = ?, subscription_current_period_end = ?,
                billing_updated_at = ?
            WHERE id = ?
            """,
            (
                plan_key, status, customer_id or None, subscription_id or None,
                price_id or None, period_end_iso, datetime.utcnow().isoformat(),
                owner["id"],
            ),
        )


@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    user = require_user(request)
    if user["role"] != "owner":
        return redirect("/dashboard")
    with get_db() as db:
        subscription = workspace_subscription(db, user)
        usage = workspace_usage(db, subscription["workspace_id"])
        branding = get_branding_for_user(db, user)
    return templates.TemplateResponse(
        "billing.html",
        {
            "request": request,
            "user": user,
            "branding": branding,
            "is_demo": is_demo_user(user),
            "subscription": subscription,
            "usage": usage,
            "plans": PLAN_CATALOG,
            "checkout_result": request.query_params.get("checkout", ""),
        },
    )


@app.post("/billing/checkout")
def create_billing_checkout(
    request: Request,
    plan: str = Form(...),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] != "owner" or is_demo_user(user):
        raise HTTPException(status_code=403)
    if plan not in {"pro", "business"}:
        raise HTTPException(status_code=400, detail="Invalid billing plan.")
    if not BILLING_ENABLED:
        return redirect("/billing?error=Stripe+billing+is+not+configured+for+this+deployment.")

    price_id = STRIPE_PRO_PRICE_ID if plan == "pro" else STRIPE_BUSINESS_PRICE_ID
    workspace_id = workspace_id_for(user)
    with get_db() as db:
        owner = db.execute("SELECT * FROM users WHERE id = ?", (workspace_id,)).fetchone()
        if (
            row_value(owner, "stripe_subscription_id", "")
            and str(row_value(owner, "subscription_status", "")).lower()
            in PAID_ACCESS_STATUSES
        ):
            return redirect(
                "/billing?error=Use+Manage+billing+to+change+an+existing+subscription."
            )
        customer_id = str(row_value(owner, "stripe_customer_id", ""))
        if not customer_id:
            customer = stripe.Customer.create(
                email=owner["email"],
                name=row_value(owner, "studio_name", "Lumaire Studio"),
                metadata={"workspace_id": str(workspace_id)},
            )
            customer_id = str(stripe_value(customer, "id", ""))
            db.execute(
                "UPDATE users SET stripe_customer_id = ?, billing_updated_at = ? WHERE id = ?",
                (customer_id, datetime.utcnow().isoformat(), workspace_id),
            )

    billing_url = f"{request.base_url}billing"
    checkout = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        allow_promotion_codes=True,
        client_reference_id=str(workspace_id),
        success_url=f"{billing_url}?checkout=success",
        cancel_url=f"{billing_url}?checkout=canceled",
        metadata={"workspace_id": str(workspace_id), "plan": plan},
        subscription_data={"metadata": {"workspace_id": str(workspace_id), "plan": plan}},
    )
    checkout_url = str(stripe_value(checkout, "url", ""))
    if not checkout_url:
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL.")
    return redirect(checkout_url)


@app.post("/billing/portal")
def create_billing_portal(request: Request, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] != "owner" or is_demo_user(user):
        raise HTTPException(status_code=403)
    if not BILLING_ENABLED:
        return redirect("/billing?error=Stripe+billing+is+not+configured+for+this+deployment.")
    with get_db() as db:
        subscription = workspace_subscription(db, user)
    customer_id = subscription["stripe_customer_id"]
    if not customer_id:
        return redirect("/billing?error=No+billing+account+is+connected+yet.")
    portal = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{request.base_url}billing",
    )
    portal_url = str(stripe_value(portal, "url", ""))
    if not portal_url:
        raise HTTPException(status_code=502, detail="Stripe did not return a portal URL.")
    return redirect(portal_url)


@app.post("/billing/webhook")
async def stripe_billing_webhook(request: Request):
    if not stripe or not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Stripe webhook is not configured.")
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except Exception as exc:
        logger.warning("Rejected Stripe webhook: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook.") from exc

    event_type = str(stripe_value(event, "type", ""))
    data_object = stripe_value(stripe_value(event, "data", {}), "object", {})
    if event_type in {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }:
        sync_stripe_subscription(data_object)
    elif event_type == "checkout.session.completed":
        subscription_id = stripe_value(data_object, "subscription", "")
        if subscription_id:
            sync_stripe_subscription(stripe.Subscription.retrieve(subscription_id))
    return JSONResponse({"received": True})


@app.post("/clients")
def create_client(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    contact: str = Form(""),
    notes: str = Form(""),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)

    if user["role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only studio managers can create clients.")

    if is_demo_user(user):
        return demo_read_only_redirect("/clients")

    if not name or not name.strip():
        return redirect("/clients?error=Client+name+is+required.")

    if not email or not email.strip():
        return redirect("/clients?error=Client+email+is+required.")

    email_clean = email.strip().lower()
    client_password = secrets.token_urlsafe(9)
    workspace_id = workspace_id_for(user)

    try:
        with get_db() as db:
            subscription = workspace_subscription(db, user)
            usage = workspace_usage(db, workspace_id)
            if not plan_allows_more(subscription, "clients", usage["clients"]):
                return redirect(
                    "/clients?error=" + plan_limit_message(subscription, "active+client")
                )
            existing_user = db.execute(
                "SELECT id FROM users WHERE email = ?",
                (email_clean,)
            ).fetchone()

            if existing_user:
                return redirect("/clients?error=This+email+is+already+registered.")

            cur = db.execute(
                "INSERT INTO clients (user_id, name, email, contact, notes, created_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    workspace_id,
                    name.strip(),
                    email_clean,
                    contact.strip(),
                    notes.strip(),
                    datetime.utcnow().isoformat(),
                ),
            )
            client_id = cur.fetchone()["id"]

            db.execute(
                """
                INSERT INTO users
                (
                    email,
                    password_hash,
                    role,
                    client_reference_id,
                    is_verified,
                    must_change_password,
                    created_at
                )
                VALUES (?, ?, 'client', ?, TRUE, TRUE, ?)
                """,
                (
                    email_clean,
                    hash_password(client_password),
                    client_id,
                    datetime.utcnow().isoformat(),
                ),
            )

            branding = get_branding_for_user(db, user)
            sent = send_client_invitation_email(
                to_email=email_clean,
                client_name=name.strip(),
                login_email=email_clean,
                temporary_password=client_password,
                login_url=str(request.base_url),
                sender_name=branding["email_sender_name"],
                studio_name=branding["studio_name"],
                brand_color=branding["brand_color"],
                logo_url=branding["logo_url"],
            )
            if not sent:
                raise InvitationDeliveryError
    except InvitationDeliveryError:
        return redirect(
            "/clients?error=Client+was+not+created+because+the+invitation+could+not+be+sent."
        )

    return redirect("/clients?success=Client+created+and+invitation+sent.")


@app.post("/clients/{client_id}/resend-invitation")
def resend_client_invitation(
    request: Request,
    client_id: int,
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)

    if user["role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only studio managers can resend invitations.")

    if is_demo_user(user):
        return demo_read_only_redirect("/clients")

    temporary_password = secrets.token_urlsafe(9)

    try:
        with get_db() as db:
            client = db.execute(
                """
                SELECT
                    c.id,
                    c.name,
                    c.email,
                    c.archived_at,
                    u.id AS client_user_id
                FROM clients c
                JOIN users u
                    ON u.client_reference_id = c.id
                   AND u.role = 'client'
                WHERE c.id = ? AND c.user_id = ?
                """,
                (client_id, workspace_id_for(user)),
            ).fetchone()

            if not client:
                raise HTTPException(status_code=404, detail="Client not found.")
            if client["archived_at"]:
                return redirect("/clients?error=Restore+this+client+before+resending+an+invitation.")

            if not client["email"]:
                return redirect("/clients?error=This+client+does+not+have+an+email+address.")

            db.execute(
                """
                UPDATE users
                SET password_hash = ?,
                    reset_token = NULL,
                    reset_token_expires_at = NULL,
                    must_change_password = TRUE,
                    session_version = session_version + 1
                WHERE id = ?
                """,
                (hash_password(temporary_password), client["client_user_id"]),
            )

            branding = get_branding_for_user(db, user)
            sent = send_client_invitation_email(
                to_email=client["email"],
                client_name=client["name"],
                login_email=client["email"],
                temporary_password=temporary_password,
                login_url=str(request.base_url),
                sender_name=branding["email_sender_name"],
                studio_name=branding["studio_name"],
                brand_color=branding["brand_color"],
                logo_url=branding["logo_url"],
            )
            if not sent:
                raise InvitationDeliveryError
    except InvitationDeliveryError:
        return redirect(
            "/clients?error=Invitation+could+not+be+sent.+The+current+password+was+not+changed."
        )

    return redirect(
        "/clients?success=Invitation+resent+with+a+new+temporary+password."
    )


@app.get("/clients", response_class=HTMLResponse)
def clients_page(
    request: Request,
    success: str = "",
    error: str = "",
):
    user = require_user(request)

    if owner_needs_setup(user):
        return redirect("/setup")

    if user["role"] not in {"owner", "admin"}:
        return redirect("/dashboard")

    with get_db() as db:
        clients = db.execute(
            """
            SELECT
                c.*,
                COUNT(p.id) AS project_count,
                MAX(p.created_at) AS latest_project_at
            FROM clients c
            LEFT JOIN projects p ON p.client_id = c.id
            WHERE c.user_id = ?
            GROUP BY c.id
            ORDER BY c.created_at DESC
            """,
            (workspace_id_for(user),),
        ).fetchall()
        branding = get_branding_for_user(db, user)

    active_clients = [client for client in clients if not client["archived_at"]]
    archived_clients = [client for client in clients if client["archived_at"]]

    return templates.TemplateResponse(
        "clients.html",
        {
            "request": request,
            "user": user,
            "clients": active_clients,
            "archived_clients": archived_clients,
            "branding": branding,
            "is_demo": is_demo_user(user),
            "success": success,
            "error": error,
        },
    )


@app.post("/clients/{client_id}/archive")
def archive_client(request: Request, client_id: int, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] not in {"owner", "admin"} or is_demo_user(user):
        raise HTTPException(status_code=403)
    with get_db() as db:
        client = db.execute(
            "SELECT * FROM clients WHERE id = ? AND user_id = ?",
            (client_id, workspace_id_for(user)),
        ).fetchone()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")
        active_project = db.execute(
            "SELECT id FROM projects WHERE client_id = ? AND archived_at IS NULL LIMIT 1",
            (client_id,),
        ).fetchone()
        if active_project:
            return redirect("/clients?error=Archive+the+client's+projects+before+archiving+the+client.")
        db.execute(
            "UPDATE clients SET archived_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), client_id),
        )
        db.execute(
            """UPDATE users
               SET is_active = FALSE, session_version = session_version + 1,
                   reset_token = NULL, reset_token_expires_at = NULL
               WHERE role = 'client' AND client_reference_id = ?""",
            (client_id,),
        )
    return redirect("/clients?success=Client+archived.")


@app.post("/clients/{client_id}/restore")
def restore_client(request: Request, client_id: int, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] not in {"owner", "admin"} or is_demo_user(user):
        raise HTTPException(status_code=403)
    with get_db() as db:
        subscription = workspace_subscription(db, user)
        usage = workspace_usage(db, workspace_id_for(user))
        if not plan_allows_more(subscription, "clients", usage["clients"]):
            return redirect(
                "/clients?error=" + plan_limit_message(subscription, "active+client")
            )
        client = db.execute(
            "SELECT id FROM clients WHERE id = ? AND user_id = ?",
            (client_id, workspace_id_for(user)),
        ).fetchone()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")
        db.execute("UPDATE clients SET archived_at = NULL WHERE id = ?", (client_id,))
        db.execute(
            """UPDATE users
               SET is_active = TRUE, session_version = session_version + 1
               WHERE role = 'client' AND client_reference_id = ?""",
            (client_id,),
        )
    return redirect("/clients?success=Client+restored.")


@app.get("/team", response_class=HTMLResponse)
def team_page(request: Request):
    user = require_user(request)
    if user["role"] not in {"owner", "admin"}:
        return redirect("/dashboard")
    workspace_id = workspace_id_for(user)
    with get_db() as db:
        members = db.execute(
            """SELECT id, email, display_name, role, created_at
               FROM users WHERE is_active = TRUE AND (id = ? OR workspace_owner_id = ?)
               ORDER BY CASE WHEN role = 'owner' THEN 0 ELSE 1 END, created_at""",
            (workspace_id, workspace_id),
        ).fetchall()
        projects = db.execute(
            "SELECT id, name, status FROM projects WHERE user_id = ? AND archived_at IS NULL ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
        assignments = db.execute(
            """SELECT pm.user_id, pm.project_id FROM project_members pm
               JOIN projects p ON pm.project_id = p.id WHERE p.user_id = ?""",
            (workspace_id,),
        ).fetchall()
        assigned_by_user = {}
        for row in assignments:
            assigned_by_user.setdefault(row["user_id"], set()).add(row["project_id"])
        if is_demo_user(user):
            demo_member_id = -1
            members = list(members) + [
                {
                    "id": demo_member_id,
                    "email": "editor@demo.lumaire.app",
                    "display_name": "Maya Chen",
                    "role": "member",
                    "created_at": datetime.utcnow().isoformat(),
                }
            ]
            assigned_by_user[demo_member_id] = {
                project["id"] for project in projects[:2]
            }
        branding = get_owner_branding(db, workspace_id)
    return templates.TemplateResponse(
        "team.html",
        {
            "request": request,
            "user": user,
            "members": members,
            "projects": projects,
            "assigned_by_user": assigned_by_user,
            "branding": branding,
            "is_demo": is_demo_user(user),
        },
    )


@app.post("/team/invite")
def invite_team_member(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    role: str = Form("member"),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] != "owner" or is_demo_user(user):
        raise HTTPException(status_code=403)
    email = email.strip().lower()
    if role not in {"admin", "member"} or not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Invalid team invitation.")
    temporary_password = secrets.token_urlsafe(10)
    workspace_id = workspace_id_for(user)
    try:
        with get_db() as db:
            existing = db.execute(
                "SELECT id, workspace_owner_id, is_active FROM users WHERE email = ?",
                (email,),
            ).fetchone()
            subscription = workspace_subscription(db, user)
            usage = workspace_usage(db, workspace_id)
            needs_seat = not existing or not bool(existing["is_active"])
            if needs_seat and not plan_allows_more(subscription, "seats", usage["seats"]):
                return redirect(
                    "/team?error=" + plan_limit_message(subscription, "studio+seat")
                )
            member_name = display_name.strip() or email.split("@", 1)[0]
            if existing:
                if bool(existing["is_active"]) or existing["workspace_owner_id"] != workspace_id:
                    return redirect("/team?error=That+email+already+has+an+account.")
                db.execute(
                    """UPDATE users
                       SET password_hash = ?, role = ?, is_verified = TRUE,
                           display_name = ?, is_active = TRUE,
                           must_change_password = TRUE,
                           session_version = session_version + 1
                       WHERE id = ?""",
                    (
                        hash_password(temporary_password), role, member_name,
                        existing["id"],
                    ),
                )
            else:
                db.execute(
                    """INSERT INTO users
                       (email, password_hash, role, is_verified, display_name,
                        workspace_owner_id, setup_completed, must_change_password,
                        is_active, created_at)
                       VALUES (?, ?, ?, TRUE, ?, ?, TRUE, TRUE, TRUE, ?)""",
                    (
                        email, hash_password(temporary_password), role,
                        member_name, workspace_id, datetime.utcnow().isoformat(),
                    ),
                )
            branding = get_owner_branding(db, workspace_id)
            sent = send_activity_email(
                to_email=email,
                subject=f"[{branding['studio_name']}] You were invited to the studio",
                project_name=branding["studio_name"],
                action_text=f"Your temporary password is: {temporary_password}",
                link_url=f"{request.base_url}login",
                sender_name=branding["email_sender_name"],
                brand_name=branding["studio_name"],
                brand_color=branding["brand_color"],
                logo_url=branding["logo_url"],
            )
            if not sent:
                raise InvitationDeliveryError
    except InvitationDeliveryError:
        return redirect("/team?error=Member+was+not+created+because+the+invitation+could+not+be+sent.")
    return redirect("/team?success=Invitation+sent.+The+member+must+change+their+temporary+password+after+sign-in.")


@app.post("/team/{member_id}/assignments")
def update_team_assignments(
    request: Request,
    member_id: int,
    project_ids: list[int] = Form([]),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] not in {"owner", "admin"} or is_demo_user(user):
        raise HTTPException(status_code=403)
    workspace_id = workspace_id_for(user)
    with get_db() as db:
        member = db.execute(
            "SELECT id, role FROM users WHERE id = ? AND workspace_owner_id = ? AND is_active = TRUE",
            (member_id, workspace_id),
        ).fetchone()
        if not member or member["role"] != "member":
            raise HTTPException(status_code=404)
        valid_rows = db.execute(
            "SELECT id FROM projects WHERE user_id = ? AND archived_at IS NULL",
            (workspace_id,),
        ).fetchall()
        valid_ids = {row["id"] for row in valid_rows}
        selected_ids = set(project_ids) & valid_ids
        db.execute("DELETE FROM project_members WHERE user_id = ?", (member_id,))
        for project_id in selected_ids:
            db.execute(
                "INSERT INTO project_members (project_id, user_id, assigned_at) VALUES (?, ?, ?)",
                (project_id, member_id, datetime.utcnow().isoformat()),
            )
    return redirect("/team?success=Project+access+updated.")


@app.post("/team/{member_id}/remove")
def remove_team_member(request: Request, member_id: int, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] != "owner" or is_demo_user(user):
        raise HTTPException(status_code=403)
    workspace_id = workspace_id_for(user)
    with get_db() as db:
        member = db.execute(
            "SELECT id FROM users WHERE id = ? AND workspace_owner_id = ? AND is_active = TRUE",
            (member_id, workspace_id),
        ).fetchone()
        if not member:
            raise HTTPException(status_code=404)
        db.execute("DELETE FROM project_members WHERE user_id = ?", (member_id,))
        db.execute("DELETE FROM project_notification_reads WHERE user_id = ?", (member_id,))
        db.execute(
            """UPDATE users
               SET is_active = FALSE,
                   session_version = session_version + 1,
                   reset_token = NULL,
                   reset_token_expires_at = NULL
               WHERE id = ?""",
            (member_id,),
        )
    return redirect("/team?success=Team+member+removed.")


@app.get("/projects/new", response_class=HTMLResponse)
def new_project_page(
    request: Request,
    error: str = "",
):
    user = require_user(request)

    if owner_needs_setup(user):
        return redirect("/setup")

    if user["role"] not in {"owner", "admin"}:
        return redirect("/dashboard")

    if is_demo_user(user):
        return demo_read_only_redirect("/dashboard")

    with get_db() as db:
        clients = db.execute(
            "SELECT id, name, email FROM clients WHERE user_id = ? AND archived_at IS NULL ORDER BY name",
            (workspace_id_for(user),),
        ).fetchall()
        branding = get_branding_for_user(db, user)

    return templates.TemplateResponse(
        "new_project.html",
        {
            "request": request,
            "user": user,
            "clients": clients,
            "branding": branding,
            "is_demo": False,
            "error": error,
            "category_options": CATEGORY_OPTIONS,
        },
    )


@app.post("/projects")
def create_project(
    request: Request,
    client_id: str = Form(""),
    name: str = Form(""),
    category: str = Form("Shorts"),
    status: str = Form("Awaiting Review"),
    notes: str = Form(""),
    review_due_at: str = Form(""),
    guest_access: str = Form("comment"),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)

    if user["role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403)

    if is_demo_user(user):
        return demo_read_only_redirect("/projects/new")

    if not client_id or not client_id.strip():
        return redirect("/projects/new?error=Please+select+a+client.")

    try:
        client_id_int = int(client_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid client selected.")

    if not name or not name.strip():
        return redirect("/projects/new?error=Project+title+is+required.")

    if (guest_access or "").strip().lower() not in GUEST_ACCESS_OPTIONS:
        raise HTTPException(status_code=400, detail="Invalid public review access.")
    normalized_due_at = normalize_review_due_at(review_due_at)
    normalized_guest_access = normalize_guest_access(guest_access)
    workspace_id = workspace_id_for(user)

    with get_db() as db:
        subscription = workspace_subscription(db, user)
        usage = workspace_usage(db, workspace_id)
        if not plan_allows_more(
            subscription, "active_projects", usage["active_projects"]
        ):
            return redirect(
                "/projects/new?error="
                + plan_limit_message(subscription, "active+project")
            )
        client = db.execute(
            "SELECT id FROM clients WHERE id = ? AND user_id = ?",
            (client_id_int, workspace_id),
        ).fetchone()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        db.execute(
            """
            INSERT INTO projects
            (user_id, client_id, name, category, status, notes, review_token,
             review_due_at, guest_access, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                client_id_int,
                name.strip(),
                category,
                status,
                notes.strip(),
                secrets.token_urlsafe(32),
                normalized_due_at or None,
                normalized_guest_access,
                datetime.utcnow().isoformat(),
            ),
        )

    return redirect("/dashboard?success=Project+created+successfully.")


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: int):
    user = require_user(request)
    with get_db() as db:
        project = db.execute(
            "SELECT p.*, c.name AS client_name FROM projects p JOIN clients c ON p.client_id=c.id WHERE p.id=?",
            (project_id,),
        ).fetchone()
            
        if not project or not can_access_project(db, user, project):
            return redirect(
                "/dashboard?error=This+project+is+not+available+for+the+active+account."
            )
        if project["archived_at"] and user["role"] not in {"owner", "admin"}:
            return redirect(
                "/dashboard?error=This+archived+project+is+only+available+to+studio+managers."
            )

        if is_studio_user(user):
            mark_project_notifications_read(db, user["id"], project_id)

        # 撈取版本時間軸（由新到舊）
        versions = db.execute(
            "SELECT * FROM video_versions WHERE project_id=? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        
        # 撈取所有歷史決策、上傳紀錄與留言 (聯集查詢：實現完整事件流)
        comments = db.execute(
            """
            -- 1. 撈取客戶的審核與留言
            SELECT 
                cm.id,
                cm.video_version_id,
                cm.author_name, 
                cm.author_role, 
                cm.body, 
                cm.type, 
                cm.is_resolved,
                cm.resolved_at,
                cm.parent_comment_id,
                cm.is_internal,
                cm.attachment_url,
                cm.attachment_name,
                cm.attachment_storage_path,
                cm.annotation_data,
                cm.created_at, 
                vv.version_label,
                vv.video_url
            FROM comments cm 
            JOIN video_versions vv ON cm.video_version_id = vv.id
            WHERE vv.project_id = ? AND (cm.is_internal = FALSE OR ? = TRUE)
            
            UNION ALL
            
            -- 2. 撈取工作室上傳新影片版本的事件
            SELECT 
                NULL AS id,
                NULL AS video_version_id,
                'Studio' AS author_name,
                'studio' AS author_role,
                'Uploaded ' || version_label AS body,
                'upload' AS type,
                FALSE AS is_resolved,
                NULL AS resolved_at,
                NULL AS parent_comment_id,
                FALSE AS is_internal,
                NULL AS attachment_url,
                NULL AS attachment_name,
                NULL AS attachment_storage_path,
                NULL AS annotation_data,
                created_at,
                version_label,
                video_url
            FROM video_versions
            WHERE project_id = ?
            
            UNION ALL
            
            -- 3. 撈取專案最初建立的事件
            SELECT 
                NULL AS id,
                NULL AS video_version_id,
                'System' AS author_name,
                'system' AS author_role,
                'Project Created' AS body,
                'create' AS type,
                FALSE AS is_resolved,
                NULL AS resolved_at,
                NULL AS parent_comment_id,
                FALSE AS is_internal,
                NULL AS attachment_url,
                NULL AS attachment_name,
                NULL AS attachment_storage_path,
                NULL AS annotation_data,
                created_at,
                '' AS version_label,
                NULL AS video_url
            FROM projects
            WHERE id = ?

            UNION ALL

            SELECT
                NULL AS id,
                NULL AS video_version_id,
                actor_name AS author_name,
                'studio' AS author_role,
                CASE event_type
                    WHEN 'reopened' THEN 'Review cycle reopened' || CASE WHEN note <> '' THEN ': ' || note ELSE '' END
                    WHEN 'archived' THEN 'Project archived'
                    WHEN 'restored' THEN 'Project restored'
                    ELSE 'Project updated'
                END AS body,
                event_type AS type,
                FALSE AS is_resolved,
                NULL AS resolved_at,
                NULL AS parent_comment_id,
                FALSE AS is_internal,
                NULL AS attachment_url,
                NULL AS attachment_name,
                NULL AS attachment_storage_path,
                NULL AS annotation_data,
                created_at,
                '' AS version_label,
                NULL AS video_url
            FROM project_lifecycle_events
            WHERE project_id = ?
            
            ORDER BY created_at DESC
            """,
            (project_id, is_studio_user(user), project_id, project_id, project_id),
        ).fetchall()
        comments = hydrate_comment_attachment_urls(comments)
        open_feedback_count = sum(
            1
            for comment in comments
            if (
                (
                    comment["author_role"] in {"client", "guest"}
                    and (
                        comment["type"] == "comment"
                        or comment["type"].startswith("timestamp_")
                    )
                )
                and not comment["is_resolved"]
            )
        )
        if project["status"] in {"Approved", "Published"}:
            open_feedback_count = 0
        
        display_notes, attachments = load_project_attachments(db, project)
        branding = get_owner_branding(db, project["user_id"])
        delivery_completed = parse_delivery_checklist(
            project["delivery_checklist"]
        )
        
    return templates.TemplateResponse(
        "project.html",
        {
            "request": request,
            "user": user,
            "project": project,
            "branding": branding,
            "versions": versions,
            "display_notes": display_notes,  # 傳遞乾淨的備註文字給前端
            "attachments": attachments,      # 傳遞解析好的附件清單給前端
            "comments": comments,
            "open_feedback_count": open_feedback_count,
            "status_options": STATUS_OPTIONS,
            "guest_access": normalize_guest_access(project["guest_access"]),
            "delivery_checklist_items": DELIVERY_CHECKLIST_ITEMS,
            "delivery_completed": delivery_completed,
            "is_public_link": False,
            "is_demo": is_demo_user(user),
        },
    )


@app.get("/projects/{project_id}/compare", response_class=HTMLResponse)
def compare_versions_page(
    request: Request,
    project_id: int,
    left: Optional[int] = None,
    right: Optional[int] = None,
):
    user = require_user(request)
    with get_db() as db:
        project = db.execute(
            """SELECT p.*, c.name AS client_name FROM projects p
               JOIN clients c ON p.client_id = c.id WHERE p.id = ?""",
            (project_id,),
        ).fetchone()
        if not project or not can_access_project(db, user, project):
            raise HTTPException(status_code=404, detail="Project not found.")
        if project["archived_at"] and user["role"] not in {"owner", "admin"}:
            raise HTTPException(status_code=404, detail="Project not found.")
        versions = db.execute(
            "SELECT * FROM video_versions WHERE project_id = ? ORDER BY created_at DESC, id DESC",
            (project_id,),
        ).fetchall()
        if len(versions) < 2:
            return redirect(f"/projects/{project_id}?warning=Upload+at+least+two+versions+to+compare.")
        by_id = {row["id"]: row for row in versions}
        left_version = by_id.get(left) if left else versions[1]
        right_version = by_id.get(right) if right else versions[0]
        if not left_version or not right_version:
            raise HTTPException(status_code=400, detail="Invalid comparison selection.")
        comparison_comments = db.execute(
            """SELECT video_version_id, author_name, body, type, created_at
               FROM comments WHERE video_version_id IN (?, ?)
                 AND (is_internal = FALSE OR ? = TRUE)
               ORDER BY created_at ASC""",
            (left_version["id"], right_version["id"], is_studio_user(user)),
        ).fetchall()
        comments_by_version = {left_version["id"]: [], right_version["id"]: []}
        for comment in comparison_comments:
            comments_by_version.setdefault(comment["video_version_id"], []).append(comment)
        branding = get_owner_branding(db, project["user_id"])
    return templates.TemplateResponse(
        "compare.html",
        {
            "request": request,
            "user": user,
            "project": project,
            "versions": versions,
            "left_version": left_version,
            "right_version": right_version,
            "comments_by_version": comments_by_version,
            "branding": branding,
            "is_demo": is_demo_user(user),
        },
    )


@app.post("/projects/{project_id}/review-settings")
def update_project_review_settings(
    request: Request,
    project_id: int,
    review_due_at: str = Form(""),
    guest_access: str = Form("comment"),
    review_expires_at: str = Form(""),
    review_timezone_offset: int = Form(0),
    review_password: str = Form(""),
    clear_review_password: Optional[str] = Form(None),
    review_link_enabled: Optional[str] = Form(None),
    review_allow_download: Optional[str] = Form(None),
    review_allow_versions: Optional[str] = Form(None),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only studio managers can update review settings.")
    if is_demo_user(user):
        return demo_read_only_redirect(f"/projects/{project_id}")

    if (guest_access or "").strip().lower() not in GUEST_ACCESS_OPTIONS:
        raise HTTPException(status_code=400, detail="Invalid public review access.")
    normalized_due_at = normalize_review_due_at(review_due_at)
    normalized_guest_access = normalize_guest_access(guest_access)
    normalized_expiry = None
    if review_expires_at.strip():
        try:
            local_expiry = datetime.fromisoformat(review_expires_at.strip())
            bounded_offset = max(-840, min(840, review_timezone_offset))
            normalized_expiry = (local_expiry + timedelta(minutes=bounded_offset)).isoformat()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid review link expiration.")

    with get_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not project or not can_access_project(db, user, project, manage=True):
            raise HTTPException(status_code=404, detail="Project not found.")
        if project["archived_at"]:
            return redirect(f"/projects/{project_id}?error=Restore+this+project+before+changing+review+settings.")
        password_hash = project["review_password_hash"]
        if clear_review_password:
            password_hash = None
        elif review_password.strip():
            if len(review_password) < 6:
                raise HTTPException(status_code=400, detail="Review password must be at least 6 characters.")
            password_hash = hash_password(review_password.strip())
        db.execute(
            """
            UPDATE projects
            SET review_due_at = ?, guest_access = ?, review_token_expires_at = ?,
                review_password_hash = ?, review_link_enabled = ?,
                review_allow_download = ?, review_allow_versions = ?
            WHERE id = ?
            """,
            (
                normalized_due_at or None,
                normalized_guest_access,
                normalized_expiry,
                password_hash,
                bool(review_link_enabled),
                bool(review_allow_download),
                bool(review_allow_versions),
                project_id,
            ),
        )

    return redirect(f"/projects/{project_id}?success=Review+settings+updated.")


@app.post("/projects/{project_id}/review-link/regenerate")
def regenerate_review_link(request: Request, project_id: int, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403)
    if is_demo_user(user):
        return demo_read_only_redirect(f"/projects/{project_id}")
    with get_db() as db:
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project or not can_access_project(db, user, project, manage=True):
            raise HTTPException(status_code=404)
        if project["archived_at"]:
            return redirect(f"/projects/{project_id}?error=Restore+this+project+before+regenerating+its+review+link.")
        db.execute(
            "UPDATE projects SET review_token = ?, review_visit_count = 0, review_last_visited_at = NULL WHERE id = ?",
            (secrets.token_urlsafe(32), project_id),
        )
    return redirect(f"/projects/{project_id}?success=Public+review+link+regenerated.")


@app.post("/projects/{project_id}/delivery-checklist")
def update_delivery_checklist(
    request: Request,
    project_id: int,
    delivery_items: list[str] = Form([]),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only studio managers can update delivery readiness.")
    if is_demo_user(user):
        return demo_read_only_redirect(f"/projects/{project_id}")

    valid_keys = {key for key, _ in DELIVERY_CHECKLIST_ITEMS}
    completed = sorted(set(delivery_items) & valid_keys)
    with get_db() as db:
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project or not can_access_project(db, user, project, manage=True):
            raise HTTPException(status_code=404, detail="Project not found.")
        if project["archived_at"]:
            return redirect(f"/projects/{project_id}?error=Restore+this+project+before+updating+delivery+readiness.")
        if project["status"] == "Published":
            raise HTTPException(status_code=400, detail="Delivered projects are read-only.")
        db.execute(
            "UPDATE projects SET delivery_checklist = ? WHERE id = ?",
            (",".join(completed), project_id),
        )

    return redirect(f"/projects/{project_id}?success=Delivery+readiness+saved.")


@app.post("/projects/{project_id}/reopen")
def reopen_project(
    request: Request,
    project_id: int,
    reason: str = Form(""),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403)
    if is_demo_user(user):
        return demo_read_only_redirect(f"/projects/{project_id}")
    with get_db() as db:
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project or not can_access_project(db, user, project, manage=True):
            raise HTTPException(status_code=404, detail="Project not found.")
        if project["status"] not in {"Approved", "Published"}:
            return redirect(f"/projects/{project_id}?error=Only+approved+or+delivered+projects+can+be+reopened.")
        now = datetime.utcnow().isoformat()
        db.execute(
            """
            UPDATE projects
            SET status = 'Awaiting Review', archived_at = NULL,
                reopened_at = ?, reopened_by = ?, delivery_checklist = '',
                review_link_enabled = TRUE
            WHERE id = ?
            """,
            (now, studio_display_name(user), project_id),
        )
        record_project_lifecycle(db, project_id, "reopened", user, reason)
    return redirect(f"/projects/{project_id}?success=Project+reopened+for+a+new+review+cycle.")


@app.post("/projects/{project_id}/archive")
def archive_project(request: Request, project_id: int, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403)
    if is_demo_user(user):
        return demo_read_only_redirect(f"/projects/{project_id}")
    with get_db() as db:
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project or not can_access_project(db, user, project, manage=True):
            raise HTTPException(status_code=404, detail="Project not found.")
        if project["archived_at"]:
            return redirect(f"/projects/{project_id}?success=Project+is+already+archived.")
        db.execute(
            "UPDATE projects SET archived_at = ?, review_link_enabled = FALSE WHERE id = ?",
            (datetime.utcnow().isoformat(), project_id),
        )
        record_project_lifecycle(db, project_id, "archived", user)
    return redirect("/dashboard?success=Project+archived.")


@app.post("/projects/{project_id}/restore")
def restore_project(request: Request, project_id: int, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403)
    if is_demo_user(user):
        return demo_read_only_redirect(f"/projects/{project_id}")
    with get_db() as db:
        subscription = workspace_subscription(db, user)
        usage = workspace_usage(db, workspace_id_for(user))
        if not plan_allows_more(
            subscription, "active_projects", usage["active_projects"]
        ):
            return redirect(
                f"/projects/{project_id}?error="
                + plan_limit_message(subscription, "active+project")
            )
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project or not can_access_project(db, user, project, manage=True):
            raise HTTPException(status_code=404, detail="Project not found.")
        db.execute("UPDATE projects SET archived_at = NULL WHERE id = ?", (project_id,))
        record_project_lifecycle(db, project_id, "restored", user)
    return redirect(f"/projects/{project_id}?success=Project+restored.+Public+link+remains+disabled.")


@app.post("/projects/{project_id}/versions")
async def create_version(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    version_label: str = Form(""),
    video_url: str = Form(""),
    video_file: UploadFile = File(None),
    notes: str = Form(""),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    if not version_label or not version_label.strip():
        return redirect(f"/projects/{project_id}?warning=Version+label+is+required.")

    user = require_user(request)
    if not is_studio_user(user):
        raise HTTPException(status_code=403, detail="Clients cannot upload versions.")

    if is_demo_user(user):
        return demo_read_only_redirect(f"/projects/{project_id}")

    with get_db() as db:
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project or not can_access_project(db, user, project):
            raise HTTPException(status_code=404, detail="Project not found.")
        if project["archived_at"]:
            return redirect(f"/projects/{project_id}?error=Restore+this+project+before+uploading+a+version.")
        if project["status"] in {"Approved", "Published"}:
            return redirect(f"/projects/{project_id}?error=Reopen+this+project+before+uploading+a+new+version.")
        subscription = workspace_subscription(db, user)
        version_count = db.execute(
            "SELECT COUNT(*) AS count FROM video_versions WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if not plan_allows_more(
            subscription,
            "versions_per_project",
            int(row_value(version_count, "count", 0)),
        ):
            return redirect(
                f"/projects/{project_id}?error="
                + plan_limit_message(subscription, "version+per+project")
            )

    final_video_url = video_url.strip() if video_url else ""

    if video_file and video_file.filename:
        if not SUPABASE_URL or not SUPABASE_KEY:
            return redirect(f"/projects/{project_id}?error=Video+storage+is+not+configured.")

        original_name = video_file.filename or "video"
        extension = os.path.splitext(original_name)[1].lower()

        allowed_extensions = {".mp4", ".webm", ".mov"}
        if extension not in allowed_extensions:
            return redirect(f"/projects/{project_id}?warning=Supported+video+formats:+MP4,+WEBM,+MOV.")

        storage_path = f"projects/{project_id}/{uuid.uuid4().hex}{extension}"
        upload_url = f"{SUPABASE_URL}/storage/v1/object/videos/{storage_path}"

        try:
            video_bytes = await read_upload_with_limit(
                video_file,
                MAX_VIDEO_UPLOAD_BYTES,
            )
        except HTTPException:
            max_mb = MAX_VIDEO_UPLOAD_BYTES // (1024 * 1024)
            return redirect(
                f"/projects/{project_id}?error=Video+must+be+smaller+than+{max_mb}+MB."
            )
        if not upload_signature_matches(video_bytes, extension):
            return redirect(f"/projects/{project_id}?error=Video+content+does+not+match+its+file+type.")

        headers = {
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "apikey": SUPABASE_KEY,
            "Content-Type": video_file.content_type or "application/octet-stream",
            "x-upsert": "true",
        }

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(upload_url, headers=headers, content=video_bytes)

        if response.status_code not in (200, 201):
            return redirect(f"/projects/{project_id}?error=Video+upload+failed.")
        
        final_video_url = f"{SUPABASE_URL}/storage/v1/object/public/videos/{storage_path}"

    if not final_video_url:
        return redirect(f"/projects/{project_id}?warning=Please+provide+a+video+URL+or+upload+a+video+file.")

    with get_db() as db:
        db.execute(
            """
            INSERT INTO video_versions
            (user_id, project_id, version_label, video_url, status, notes, created_by_name, created_at)
            VALUES (?, ?, ?, ?, 'Awaiting Review', ?, ?, ?)
            """,
            (
                project["user_id"],
                project_id,
                version_label.strip(),
                final_video_url,
                notes.strip(),
                studio_display_name(user),
                datetime.utcnow().isoformat(),
            ),
        )

        db.execute(
            "UPDATE projects SET status='Awaiting Review' WHERE id=?",
            (project_id,),
        )

        project_info = db.execute(
            """
            SELECT p.name AS p_name, p.review_token, c.email AS c_email,
                   u.studio_name, u.brand_color, u.email_sender_name, u.logo_url
            FROM projects p
            JOIN clients c ON p.client_id=c.id
            JOIN users u ON p.user_id=u.id
            WHERE p.id=?
            """,
            (project_id,),
        ).fetchone()

        if project_info and project_info["c_email"]:
            public_review_url = (
                f"{request.base_url}review/{project_info['review_token']}"
            )

            background_tasks.add_task(
                send_activity_email,
                to_email=project_info["c_email"],
                subject=(
                    f"[{clean_email_sender_name(project_info['studio_name'])}] "
                    f"New version {version_label.strip()} uploaded"
                ),
                project_name=project_info["p_name"],
                action_text=f"Studio uploaded a new version ({version_label.strip()}). Please review it when available.",
                link_url=public_review_url,
                sender_name=project_info["email_sender_name"],
                brand_name=project_info["studio_name"],
                brand_color=project_info["brand_color"],
                logo_url=project_info["logo_url"],
            )

    return redirect(f"/projects/{project_id}?success=Version+uploaded+successfully.")


@app.post("/versions/{version_id}/action")
async def version_decision(
    request: Request,
    version_id: int,
    background_tasks: BackgroundTasks,
    action_type: str = Form(...),
    body: str = Form(...),
    video_time: Optional[str] = Form(None),
    time_str: Optional[str] = Form(None),
    parent_comment_id: Optional[int] = Form(None),
    internal_note: Optional[str] = Form(None),
    annotation_data: str = Form(""),
    comment_attachment: UploadFile = File(None),
    review_token: str = Form(""),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = get_current_user(request)

    if action_type not in {"comment", "approve", "reject"}:
        raise HTTPException(status_code=400, detail="Invalid review action.")

    with get_db() as db:
        version = db.execute(
            "SELECT * FROM video_versions WHERE id = ?",
            (version_id,)
        ).fetchone()

        if not version:
            raise HTTPException(status_code=404)

        project = db.execute(
            """
            SELECT p.*, c.name AS client_name, u.email AS owner_email,
                   u.studio_name, u.brand_color, u.email_sender_name, u.logo_url
            FROM projects p
            JOIN clients c ON p.client_id = c.id
            JOIN users u ON p.user_id = u.id
            WHERE p.id = ?
            """,
            (version["project_id"],),
        ).fetchone()

        if not project:
            raise HTTPException(status_code=404, detail="Project not found.")
        if project["archived_at"]:
            raise HTTPException(status_code=400, detail="Archived projects are read-only.")

        if not user and project["review_password_hash"] and not has_review_password_grant(request, review_token):
            raise HTTPException(status_code=403, detail="Review password required.")
        if not user and not project["review_allow_versions"]:
            latest_version = db.execute(
                "SELECT id FROM video_versions WHERE project_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (version["project_id"],),
            ).fetchone()
            if not latest_version or latest_version["id"] != version_id:
                raise HTTPException(status_code=403, detail="Version history is hidden for this link.")

        author_role, author_name = get_review_actor(
            user,
            project,
            review_token,
            action_type,
        )
        if not user:
            if not guest_action_is_allowed(
                project["guest_access"],
                action_type,
            ):
                raise HTTPException(
                    status_code=403,
                    detail="This public review link does not allow that action.",
                )
        is_internal = bool(internal_note)
        if is_internal and (not user or not is_studio_user(user)):
            raise HTTPException(status_code=403, detail="Only studio members can add internal notes.")
        if parent_comment_id:
            parent = db.execute(
                """SELECT cm.id, cm.is_internal FROM comments cm JOIN video_versions vv
                   ON cm.video_version_id = vv.id
                   WHERE cm.id = ? AND vv.project_id = ?""",
                (parent_comment_id, version["project_id"]),
            ).fetchone()
            if not parent:
                raise HTTPException(status_code=400, detail="Invalid reply target.")
            if parent["is_internal"] and (not user or not is_studio_user(user)):
                raise HTTPException(status_code=403, detail="Reply target is not available.")
        target_path = (
            f"/projects/{version['project_id']}"
            if user
            else f"/review/{project['review_token']}"
        )

        if is_demo_email(project["owner_email"]):
            target_path = (
                f"/projects/{version['project_id']}"
                if user
                else f"/review/{project['review_token']}"
            )
            return demo_read_only_redirect(target_path)

        if not body or not body.strip():
            return redirect(f"{target_path}?error=Comment+cannot+be+empty.")

        if version["status"] == "Approved":
            raise HTTPException(status_code=400, detail="This version has been approved and locked.")

        final_body = body.strip()
        final_type = action_type
        final_annotation = normalize_annotation_data(annotation_data)
        attachment_url = None
        attachment_name = None
        attachment_storage_path = None

        if comment_attachment and comment_attachment.filename:
            if not SUPABASE_URL or not SUPABASE_KEY:
                return redirect(f"{target_path}?error=Attachment+storage+is+not+configured.")
            attachment_name = os.path.basename(comment_attachment.filename)[:180]
            extension = os.path.splitext(attachment_name)[1].lower()
            allowed_extensions = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".txt", ".doc", ".docx"}
            if extension not in allowed_extensions:
                return redirect(f"{target_path}?error=Unsupported+comment+attachment.")
            try:
                attachment_bytes = await read_upload_with_limit(comment_attachment, MAX_ATTACHMENT_UPLOAD_BYTES)
            except HTTPException:
                return redirect(f"{target_path}?error=Comment+attachment+is+too+large.")
            if not upload_signature_matches(attachment_bytes, extension):
                return redirect(f"{target_path}?error=Attachment+content+does+not+match+its+file+type.")
            storage_path = f"comments/{version['project_id']}/{uuid.uuid4().hex}{extension}"
            upload_url = f"{SUPABASE_URL}/storage/v1/object/attachments/{storage_path}"
            headers = {
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "apikey": SUPABASE_KEY,
                "Content-Type": comment_attachment.content_type or "application/octet-stream",
                "x-upsert": "false",
            }
            async with httpx.AsyncClient(timeout=45) as client:
                upload_response = await client.post(upload_url, headers=headers, content=attachment_bytes)
            if upload_response.status_code not in (200, 201):
                return redirect(f"{target_path}?error=Comment+attachment+upload+failed.")
            attachment_storage_path = storage_path

        if time_str and time_str.strip() and video_time:
            final_body = f"⏱️ [{time_str.strip()}] {final_body}"
            if action_type == "comment":
                final_type = f"timestamp_{video_time}"

        now = datetime.utcnow().isoformat()

        db.execute(
            """
            INSERT INTO comments
            (video_version_id, author_role, author_name, body, type,
             parent_comment_id, is_internal, attachment_url, attachment_name,
             attachment_storage_path, annotation_data, author_email,
             identity_verified, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id, author_role, author_name, final_body, final_type,
                parent_comment_id, is_internal, attachment_url, attachment_name,
                attachment_storage_path, final_annotation,
                user["email"] if user else None,
                bool(user),
                now,
            ),
        )

        if action_type == "approve":
            db.execute(
                "UPDATE video_versions SET status = 'Approved' WHERE id = ?",
                (version_id,)
            )
            db.execute(
                "UPDATE projects SET status = 'Approved' WHERE id = ?",
                (version["project_id"],)
            )
            db.execute(
                """
                UPDATE comments
                SET is_resolved = TRUE, resolved_at = ?
                WHERE author_role IN ('client', 'guest')
                  AND is_resolved = FALSE
                  AND video_version_id IN (
                      SELECT id
                      FROM video_versions
                      WHERE project_id = ?
                  )
                  AND (
                      type = 'comment'
                      OR type LIKE 'timestamp_%%'
                  )
                """,
                (
                    datetime.now(timezone.utc)
                    .replace(tzinfo=None)
                    .isoformat(),
                    version["project_id"],
                ),
            )

        elif action_type == "reject":
            db.execute(
                "UPDATE video_versions SET status = 'Revision Requested' WHERE id = ?",
                (version_id,)
            )
            db.execute(
                "UPDATE projects SET status = 'In Revision' WHERE id = ?",
                (version["project_id"],)
            )

        if project["owner_email"] and author_role in {"client", "guest"}:
            project_url = f"{request.base_url}projects/{version['project_id']}"
            status_emojis = {
                "approve": "✅ Approved",
                "reject": "❌ Change Requested",
                "comment": "💬 New Comment",
            }
            action_display = status_emojis.get(action_type, action_type)

            background_tasks.add_task(
                send_activity_email,
                to_email=project["owner_email"],
                subject=(
                    f"[{clean_email_sender_name(project['studio_name'])}] "
                    f"Project activity: {action_display}"
                ),
                project_name=project["name"],
                action_text=(
                    f"Reviewer ({author_name}) has submitted an action "
                    f"[{action_display}] on {version['version_label']}.\n"
                    f"Feedback: \"{final_body}\""
                ),
                link_url=project_url,
                sender_name=project["email_sender_name"],
                brand_name=project["studio_name"],
                brand_color=project["brand_color"],
                logo_url=project["logo_url"],
            )

    return redirect(target_path)


@app.post("/comments/{comment_id}/resolve")
def resolve_comment(
    request: Request,
    comment_id: int,
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if not is_studio_user(user):
        raise HTTPException(
            status_code=403,
            detail="Only studio collaborators can update feedback status.",
        )
    if is_demo_user(user):
        return demo_read_only_redirect("/dashboard")

    with get_db() as db:
        member_filter = "" if user["role"] != "member" else " AND EXISTS (SELECT 1 FROM project_members pm WHERE pm.project_id = p.id AND pm.user_id = ?)"
        params = [comment_id, workspace_id_for(user)]
        if user["role"] == "member":
            params.append(user["id"])
        comment = db.execute(
            """
            SELECT cm.id, cm.type, cm.author_role, cm.is_resolved,
                   p.id AS project_id, p.status AS project_status,
                   p.archived_at AS project_archived_at,
                   vv.status AS version_status
            FROM comments cm
            JOIN video_versions vv ON cm.video_version_id = vv.id
            JOIN projects p ON vv.project_id = p.id
            WHERE cm.id = ? AND p.user_id = ?
            """ + member_filter,
            params,
        ).fetchone()
        if not comment:
            raise HTTPException(status_code=404, detail="Feedback not found.")
        if comment["project_archived_at"]:
            raise HTTPException(status_code=400, detail="Archived projects are read-only.")
        if comment["project_status"] in {"Approved", "Published"}:
            raise HTTPException(
                status_code=400,
                detail="Approved projects are read-only.",
            )
        if comment["version_status"] == "Approved":
            raise HTTPException(
                status_code=400,
                detail="Approved versions are read-only.",
            )
        if (
            comment["author_role"] not in {"client", "guest"}
            or (
                comment["type"] != "comment"
                and not comment["type"].startswith("timestamp_")
            )
        ):
            raise HTTPException(
                status_code=400,
                detail="Only feedback notes can be resolved.",
            )

        next_state = not bool(comment["is_resolved"])
        db.execute(
            """
            UPDATE comments
            SET is_resolved = ?, resolved_at = ?
            WHERE id = ?
            """,
            (
                next_state,
                (
                    datetime.now(timezone.utc)
                    .replace(tzinfo=None)
                    .isoformat()
                    if next_state
                    else None
                ),
                comment_id,
            ),
        )

    message = (
        "Feedback+marked+as+resolved."
        if next_state
        else "Feedback+reopened."
    )
    return redirect(f"/projects/{comment['project_id']}?success={message}")


# ==========================================
# 客戶免登入公開審片連結路由 (Frame.io 模式)
# ==========================================

@app.post("/review/{review_token}/unlock")
def unlock_public_review(
    request: Request,
    review_token: str,
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    enforce_rate_limit(
        request,
        "review-unlock",
        review_token,
        PUBLIC_UNLOCK_RATE_LIMIT,
    )
    with get_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE review_token = ?", (review_token,)
        ).fetchone()
        if not project or not review_token_is_valid(project, review_token):
            raise HTTPException(status_code=404, detail="Review link invalid or expired")
        if not project["review_password_hash"] or not verify_password(password, project["review_password_hash"]):
            return redirect(f"/review/{review_token}?error=Incorrect+review+password.")
    clear_rate_limit(request, "review-unlock", review_token)
    grants = list(request.session.get("review_password_grants", []))
    if review_token not in grants:
        grants.append(review_token)
    request.session["review_password_grants"] = grants[-10:]
    return redirect(f"/review/{review_token}")


@app.get("/review/{review_token}", response_class=HTMLResponse)
def public_review_page(request: Request, review_token: str):
    with get_db() as db:
        project = db.execute(
            """
            SELECT p.*, c.name AS client_name, u.email AS owner_email
            FROM projects p
            JOIN clients c ON p.client_id = c.id
            JOIN users u ON p.user_id = u.id
            WHERE p.review_token = ?
            """,
            (review_token,),
        ).fetchone()
        if not project or not review_token_is_valid(project, review_token):
            raise HTTPException(
                status_code=404,
                detail="Review link invalid or expired",
            )
        if project["review_password_hash"] and not has_review_password_grant(request, review_token):
            return templates.TemplateResponse(
                "review_unlock.html",
                {
                    "request": request,
                    "project": project,
                    "review_token": review_token,
                    "branding": get_owner_branding(db, project["user_id"]),
                    "error": request.query_params.get("error", ""),
                },
            )
        project_id = project["id"]
        visit_key = "review_visit_" + hashlib.sha256(review_token.encode("utf-8")).hexdigest()[:16]
        if not request.session.get(visit_key):
            db.execute(
                "UPDATE projects SET review_visit_count = review_visit_count + 1, review_last_visited_at = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), project_id),
            )
            request.session[visit_key] = True
        version_limit = "" if project["review_allow_versions"] else " LIMIT 1"
        versions = db.execute(
            "SELECT * FROM video_versions WHERE project_id=? ORDER BY created_at DESC" + version_limit,
            (project_id,),
        ).fetchall()
        comments = db.execute(
            """SELECT cm.id, cm.video_version_id, cm.author_name, cm.author_role, cm.body, cm.type,
                      cm.is_resolved, cm.resolved_at, cm.parent_comment_id,
                      cm.is_internal, cm.attachment_url, cm.attachment_name,
                      cm.annotation_data, cm.attachment_storage_path, cm.created_at, vv.version_label, vv.video_url
               FROM comments cm JOIN video_versions vv ON cm.video_version_id = vv.id
               WHERE vv.project_id = ? AND cm.is_internal = FALSE
               UNION ALL
                SELECT NULL AS id, id AS video_version_id, COALESCE(created_by_name, 'Studio') AS author_name, 'studio' AS author_role,
                      'Uploaded ' || version_label AS body, 'upload' AS type,
                      FALSE AS is_resolved, NULL AS resolved_at, NULL AS parent_comment_id,
                      FALSE AS is_internal, NULL AS attachment_url, NULL AS attachment_name,
                      NULL AS annotation_data, NULL AS attachment_storage_path, created_at, version_label, video_url
               FROM video_versions WHERE project_id = ?
               UNION ALL
               SELECT NULL AS id, NULL AS video_version_id, 'System' AS author_name, 'system' AS author_role,
                      'Project Created' AS body, 'create' AS type,
                      FALSE AS is_resolved, NULL AS resolved_at, NULL AS parent_comment_id,
                      FALSE AS is_internal, NULL AS attachment_url, NULL AS attachment_name,
                      NULL AS annotation_data, NULL AS attachment_storage_path, created_at, '' AS version_label, NULL AS video_url
               FROM projects WHERE id = ?
               ORDER BY created_at DESC""", (project_id, project_id, project_id)
        ).fetchall()
        comments = hydrate_comment_attachment_urls(comments)
        if not project["review_allow_versions"]:
            visible_version_ids = {version["id"] for version in versions}
            comments = [
                comment
                for comment in comments
                if comment["video_version_id"] is None
                or comment["video_version_id"] in visible_version_ids
            ]
        open_feedback_count = sum(
            1
            for comment in comments
            if (
                (
                    comment["author_role"] in {"client", "guest"}
                    and (
                        comment["type"] == "comment"
                        or comment["type"].startswith("timestamp_")
                    )
                )
                and not comment["is_resolved"]
            )
        )
        if project["status"] in {"Approved", "Published"}:
            open_feedback_count = 0

        display_notes, attachments = load_project_attachments(db, project)
        if not project["review_allow_download"]:
            attachments = []
        branding = get_owner_branding(db, project["user_id"])
        delivery_completed = parse_delivery_checklist(
            project["delivery_checklist"]
        )

    return templates.TemplateResponse(
        "project.html",
        {
            "request": request,
            "user": {"role": "guest", "email": "Public Reviewer"},
            "project": project,
            "branding": branding,
            "versions": versions,
            "display_notes": display_notes,
            "attachments": attachments,
            "comments": comments,
            "open_feedback_count": open_feedback_count,
            "status_options": STATUS_OPTIONS,
            "guest_access": normalize_guest_access(project["guest_access"]),
            "delivery_checklist_items": DELIVERY_CHECKLIST_ITEMS,
            "delivery_completed": delivery_completed,
            "is_public_link": True,
            "review_token": review_token,
            "is_demo": is_demo_email(project["owner_email"]),
        },
    )


# ==========================================
# 專案最終交付結案路由
# ==========================================
@app.post("/projects/{project_id}/deliver")
def deliver_project(
    project_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only studio managers can mark final delivery.")

    if is_demo_user(user):
        return demo_read_only_redirect(f"/projects/{project_id}")
        
    with get_db() as db:
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project or not can_access_project(db, user, project, manage=True):
            raise HTTPException(status_code=404)
        if project["archived_at"]:
            return redirect(f"/projects/{project_id}?error=Restore+this+project+before+final+delivery.")
            
        # 將專案狀態更新為 Published (代表最終交付結案)
        if project["status"] != "Approved":
            raise HTTPException(
                status_code=400,
                detail="Only approved projects can be marked for final delivery.",
            )

        required_delivery_items = {key for key, _ in DELIVERY_CHECKLIST_ITEMS}
        completed_delivery_items = parse_delivery_checklist(
            project["delivery_checklist"]
        )
        if completed_delivery_items != required_delivery_items:
            return redirect(
                f"/projects/{project_id}?error=Complete+the+delivery+checklist+before+final+delivery."
            )

        db.execute("UPDATE projects SET status='Published' WHERE id=?", (project_id,))
        
        # 📬 【加分功能】：同時自動觸發一封結案信通知客戶前來下載最終成片！
        branding = get_owner_branding(db, project["user_id"])
        client_info = db.execute("SELECT email FROM clients WHERE id=?", (project["client_id"],)).fetchone()
        if client_info and client_info["email"]:
            background_tasks.add_task(
                send_activity_email,
                to_email=client_info["email"],
                subject=(
                    f"[{clean_email_sender_name(branding['studio_name'])}] "
                    f"Final delivery completed for '{project['name']}'"
                ),
                project_name=project["name"],
                action_text="Studio has marked this project as Final Delivered! All approved master files have been successfully dispatched and archived.",
                link_url=f"{request.base_url}review/{project['review_token']}",
                sender_name=branding["email_sender_name"],
                brand_name=branding["studio_name"],
                brand_color=branding["brand_color"],
                logo_url=branding["logo_url"],
            )
            
    return redirect(f"/projects/{project_id}")


# ==========================================
# 📂 專案附件上傳路由
# ==========================================
@app.post("/projects/{project_id}/attachments")
async def add_project_attachment(
    project_id: int,
    request: Request,
    file_title: str = Form(...),
    file: UploadFile = File(...),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    if not file_title or not file_title.strip():
        return redirect(f"/projects/{project_id}?error=File+name+cannot+be+empty.")

    user = require_user(request)
    if not is_studio_user(user):
        raise HTTPException(status_code=403)

    if is_demo_user(user):
        return demo_read_only_redirect(f"/projects/{project_id}")

    with get_db() as db:
        owned_project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not owned_project or not can_access_project(db, user, owned_project):
            raise HTTPException(status_code=404, detail="Project not found.")
        if owned_project["archived_at"]:
            return redirect(f"/projects/{project_id}?error=Restore+this+project+before+adding+attachments.")

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=500, detail="Supabase storage is not configured.")

    original_name = file.filename or "attachment"
    safe_title = file_title.strip()
    extension = os.path.splitext(original_name)[1].lower()

    allowed_extensions = {".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".zip"}
    if extension not in allowed_extensions:
        return redirect(f"/projects/{project_id}?error=Supported+formats:+PDF,+DOCX,+PNG,+JPG,+ZIP.")

    storage_path = f"projects/{project_id}/{uuid.uuid4().hex}{extension}"
    upload_url = f"{SUPABASE_URL}/storage/v1/object/attachments/{storage_path}"

    try:
        file_bytes = await read_upload_with_limit(
            file,
            MAX_ATTACHMENT_UPLOAD_BYTES,
        )
    except HTTPException:
        max_mb = MAX_ATTACHMENT_UPLOAD_BYTES // (1024 * 1024)
        return redirect(
            f"/projects/{project_id}?error=Attachment+must+be+smaller+than+{max_mb}+MB."
        )
    if not upload_signature_matches(file_bytes, extension):
        return redirect(f"/projects/{project_id}?error=Attachment+content+does+not+match+its+file+type.")

    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey": SUPABASE_KEY,
        "Content-Type": file.content_type or "application/octet-stream",
        "x-upsert": "true",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(upload_url, headers=headers, content=file_bytes)

    if response.status_code not in (200, 201):
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload attachment: {response.text}",
        )

    with get_db() as db:
        project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project or not can_access_project(db, user, project):
            raise HTTPException(status_code=404, detail="Project not found.")
        if project["archived_at"]:
            return redirect(f"/projects/{project_id}?error=Restore+this+project+before+adding+attachments.")
        db.execute(
            """
            INSERT INTO project_attachments
            (project_id, title, original_name, storage_path, content_type,
             size_bytes, created_by_user_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                safe_title,
                os.path.basename(original_name)[:180],
                storage_path,
                file.content_type or "application/octet-stream",
                len(file_bytes),
                user["id"],
                datetime.utcnow().isoformat(),
            ),
        )

    return redirect(f"/projects/{project_id}?success=Attachment+uploaded.")


@app.post("/projects/{project_id}/attachments/{attachment_id}/delete")
async def delete_project_attachment(
    project_id: int,
    attachment_id: int,
    request: Request,
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)
    if user["role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403)
    if is_demo_user(user):
        return demo_read_only_redirect(f"/projects/{project_id}")
    with get_db() as db:
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project or not can_access_project(db, user, project, manage=True):
            raise HTTPException(status_code=404, detail="Project not found.")
        if project["archived_at"]:
            return redirect(f"/projects/{project_id}?error=Restore+this+project+before+deleting+attachments.")
        attachment = db.execute(
            "SELECT * FROM project_attachments WHERE id = ? AND project_id = ?",
            (attachment_id, project_id),
        ).fetchone()
        if not attachment:
            raise HTTPException(status_code=404, detail="Attachment not found.")
        if attachment["storage_path"] and SUPABASE_URL and SUPABASE_KEY:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.delete(
                    f"{SUPABASE_URL}/storage/v1/object/attachments/{attachment['storage_path']}",
                    headers={
                        "Authorization": f"Bearer {SUPABASE_KEY}",
                        "apikey": SUPABASE_KEY,
                    },
                )
            if response.status_code not in {200, 204, 404}:
                return redirect(f"/projects/{project_id}?error=Attachment+could+not+be+removed+from+storage.")
            with _signed_url_cache_lock:
                _signed_url_cache.pop(
                    ("attachments", attachment["storage_path"]),
                    None,
                )
        db.execute("DELETE FROM project_attachments WHERE id = ?", (attachment_id,))
    return redirect(f"/projects/{project_id}?success=Attachment+deleted.")

@app.get("/db-test")
def db_test():
    if os.getenv("ENABLE_DB_TEST") != "1":
        raise HTTPException(status_code=404)

    if not DATABASE_URL:
        return {"ok": False, "error": "DATABASE_URL not set"}

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    cur = conn.cursor()
    cur.execute("SELECT NOW() AS now")
    row = cur.fetchone()
    cur.close()
    conn.close()

    return {"ok": True, "now": str(row["now"])}
