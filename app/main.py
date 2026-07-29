from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Optional

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
from itsdangerous import BadSignature, URLSafeSerializer

import os
import resend
import uuid
import httpx
import psycopg2
from psycopg2.extras import RealDictCursor
from authlib.integrations.starlette_client import OAuth
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "clientflow.db"
IS_PRODUCTION = os.getenv("RENDER", "").strip().lower() in {"1", "true", "yes"}
SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET:
    if IS_PRODUCTION:
        raise RuntimeError("SESSION_SECRET must be configured in production.")
    SESSION_SECRET = secrets.token_urlsafe(32)
serializer = URLSafeSerializer(SESSION_SECRET, salt="clientflow-session")

app = FastAPI(title="Lumaire")
logger = logging.getLogger(__name__)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="oauth_session",
    same_site="lax",
    https_only=IS_PRODUCTION,
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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
STATUS_OPTIONS = ["Awaiting Review", "In Revision", "Approved", "Published"]
CATEGORY_OPTIONS = ["Shorts", "Reels", "TikTok", "Ad", "YouTube", "Other"]
DEFAULT_STUDIO_NAME = "Lumaire Studio"
DEFAULT_BRAND_COLOR = "#9b8cf6"
DEFAULT_EMAIL_SENDER_NAME = "Lumaire"
EMAIL_TEST_RECIPIENT = os.getenv("EMAIL_TEST_RECIPIENT", "").strip()
PASSWORD_ITERATIONS = 600_000
MAX_VIDEO_UPLOAD_BYTES = int(os.getenv("MAX_VIDEO_UPLOAD_MB", "250")) * 1024 * 1024
MAX_ATTACHMENT_UPLOAD_BYTES = int(os.getenv("MAX_ATTACHMENT_UPLOAD_MB", "25")) * 1024 * 1024
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7
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
resend.api_key = os.getenv("RESEND_API_KEY")

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
) -> None:
    try:
        recipient = EMAIL_TEST_RECIPIENT or to_email
        safe_project_name = escape(str(project_name or "Untitled project"))
        safe_action_text = escape(str(action_text or "")).replace("\n", "<br>")
        plain_link = validated_email_url(link_url) or "Link unavailable"

        resend.Emails.send({
            "from": f"{clean_email_sender_name(sender_name)} <onboarding@resend.dev>",
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

        print(f"Email sent successfully to {recipient}")

    except Exception as e:
        print(f"❌ Email failed: {e}")

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
            "from": f"{clean_email_sender_name(sender_name)} <onboarding@resend.dev>",
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

        print(f"Client invitation email sent successfully to {recipient}")
        return True

    except Exception as e:
        print(f"Client invitation email failed: {e}")
        return False


class InvitationDeliveryError(RuntimeError):
    pass

# 🎯 取得當前這個 main.py 檔案所在的資料夾絕對路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 🎯 不論是在本機 Windows 還是雲端 Linux，都能精準拼出正確的資料庫絕對路徑
DB_PATH = os.path.join(BASE_DIR, "database.db")

def send_verification_email(to_email: str, verify_url: str) -> None:
    try:
        recipient = EMAIL_TEST_RECIPIENT or to_email

        resend.Emails.send({
            "from": "Lumaire <onboarding@resend.dev>",
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

        print(f"Verification email sent to {recipient}")

    except Exception as e:
        print(f"Verification email failed: {e}")


def send_password_reset_email(to_email: str, reset_url: str) -> None:
    try:
        recipient = EMAIL_TEST_RECIPIENT or to_email

        resend.Emails.send({
            "from": "Lumaire <onboarding@resend.dev>",
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

        print(f"Password reset email sent to {recipient}")

    except Exception as e:
        print(f"Password reset email failed: {e}")

class PostgresDB:
    def __init__(self):
        self.conn = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=RealDictCursor,
            sslmode="require"
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
                setup_completed BOOLEAN NOT NULL DEFAULT FALSE,
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
        except Exception as e:
            print(f"User verification migration skipped: {e}")

        db.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                email TEXT,
                contact TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)

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
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            )
        """)

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


def get_owner_notifications(db, user_id: int, limit: int = 8):
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
                'Studio' AS author_name,
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
        (user_id, user_id, user_id, user_id, limit),
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
                "UPDATE video_versions SET status = 'Revision Requested' WHERE id IN (?, ?)",
                (first_version["id"], latest_version["id"]),
            )
            db.execute(
                "UPDATE projects SET status = 'In Revision' WHERE id = ?",
                (launch_project["id"],),
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
                "UPDATE projects SET status = ? WHERE id = ?",
                (project_status, project["id"]),
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
                "UPDATE projects SET status = 'Published' WHERE id = ?",
                (summer_project["id"],),
            )


@app.on_event("startup")
def startup() -> None:
    init_db()
    try:
        seed_demo_review_history()
    except Exception as exc:
        print(f"Demo history seed skipped: {exc}")

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


def set_session_cookie(response: Response, request: Request, user_id: int) -> None:
    response.set_cookie(
        "session",
        serializer.dumps(user_id),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=IS_PRODUCTION or request.url.scheme == "https",
    )


def review_token_is_valid(project: object, submitted_token: str) -> bool:
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
        return "client", project["client_name"]

    owner_access = (
        user["role"] == "owner" and user["id"] == project["user_id"]
    )
    client_access = (
        user["role"] == "client"
        and user["client_reference_id"] == project["client_id"]
    )
    if not owner_access and not client_access:
        raise HTTPException(status_code=403, detail="Project access denied.")
    if owner_access and action_type in {"approve", "reject"}:
        raise HTTPException(
            status_code=403,
            detail="Only the assigned client can approve or reject a version.",
        )
    if client_access:
        return "client", project["client_name"]
    return "studio", normalize_branding(user)["studio_name"]


async def read_upload_with_limit(file: UploadFile, max_bytes: int) -> bytes:
    content = bytearray()
    while True:
        chunk = await file.read(min(1024 * 1024, max_bytes + 1 - len(content)))
        if not chunk:
            return bytes(content)
        content.extend(chunk)
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail="Uploaded file is too large.")


templates.env.globals["csrf_token"] = get_csrf_token

def get_user_from_session_token(token: Optional[str]) -> Optional[sqlite3.Row]:
    if not token:
        return None
    try:
        user_id = serializer.loads(token)
    except BadSignature:
        return None
    with get_db() as db:
        return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_current_user(request: Request) -> Optional[sqlite3.Row]:
    return get_user_from_session_token(request.cookies.get("session"))


def require_user(request: Request) -> sqlite3.Row:
    user = get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=303,
            headers={"Location": "/login?error=session-expired"},
        )
    return user


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

    return normalize_branding(user)

def owner_needs_setup(user: sqlite3.Row) -> bool:
    return user["role"] == "owner" and not bool(user["setup_completed"])

def post_login_path(user: sqlite3.Row) -> str:
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
    background_tasks: BackgroundTasks,
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
    except psycopg2.IntegrityError:
        return redirect("/register?error=email-exists")
    
    verify_url = f"{request.base_url}verify-email/{verification_token}"

    background_tasks.add_task(
        send_verification_email,
        email.strip().lower(),
        verify_url,
    )

    return redirect("/login?success=verification-sent")


import logging

# 加入這行來設定記錄器，這樣我們能在 Render 的 Logs 看到後端發生什麼
logger = logging.getLogger("uvicorn.error")

@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    logger.info(f"Login attempt for: {email}") # 這行會出現在 Logs
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    
    if not user:
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

    if not user or not user["is_verified"]:
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
    if is_demo_email(email_clean):
        return redirect("/forgot-password?success=reset-link-sent")

    reset_token = secrets.token_urlsafe(32)
    expires_at = (datetime.utcnow() + timedelta(hours=1)).isoformat()

    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE email = ?",
            (email_clean,),
        ).fetchone()

        if user:
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
    if user and user["reset_token_expires_at"]:
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

        if not user or not user["reset_token_expires_at"]:
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
                is_verified = TRUE
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
    token = await oauth.google.authorize_access_token(request)
    user_info = token.get("userinfo")

    if not user_info or not user_info.get("email"):
        return RedirectResponse(url="/login?error=google_login_failed", status_code=303)

    email = user_info["email"].strip().lower()

    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE email = ?",
            (email,)
        ).fetchone()

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

        if not user:
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
                     AND cm.author_role = 'client'
                     AND cm.is_resolved = FALSE
                     AND (cm.type = 'comment' OR cm.type LIKE 'timestamp_%%')) AS unresolved_count
                FROM projects p
                JOIN clients c ON p.client_id = c.id
                WHERE p.client_id = ?
            """
            params = [user["client_reference_id"]]
        else:
            # 工作室老闆視角：看所有
            clients = db.execute("SELECT * FROM clients WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()
            query = """
                SELECT p.*, c.name AS client_name,
                    (SELECT COUNT(*) FROM comments cm JOIN video_versions vv ON cm.video_version_id = vv.id
                     WHERE vv.project_id = p.id
                     AND p.status NOT IN ('Approved', 'Published')
                     AND cm.author_role = 'client'
                     AND cm.is_resolved = FALSE
                     AND (cm.type = 'comment' OR cm.type LIKE 'timestamp_%%')) AS unresolved_count
                FROM projects p
                JOIN clients c ON p.client_id = c.id
                WHERE p.user_id = ?
            """
            params = [user["id"]]

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

        notifications = (
            []
            if user["role"] == "client"
            else get_owner_notifications(db, user["id"])
        )
        
        # 統計數據卡片
        target_id = user["client_reference_id"] if user["role"] == "client" else user["id"]
        col = "client_id" if user["role"] == "client" else "user_id"
        stats = db.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='Awaiting Review' THEN 1 ELSE 0 END) AS awaiting,
                SUM(CASE WHEN status='In Revision' THEN 1 ELSE 0 END) AS revision,
                SUM(CASE WHEN status='Approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN status='Published' THEN 1 ELSE 0 END) AS published
            FROM projects WHERE {col} = ?
            """,
            (target_id,),
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

    if user["role"] != "owner":
        return redirect("/dashboard")

    user_id = user["id"]

    with get_db() as db:
        totals = db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM clients WHERE user_id = ?) AS total_clients,
                (SELECT COUNT(*) FROM projects WHERE user_id = ?) AS total_projects,
                (SELECT COUNT(*) FROM video_versions WHERE user_id = ?) AS total_versions,
                (
                    SELECT COUNT(*)
                    FROM comments cm
                    JOIN video_versions vv ON cm.video_version_id = vv.id
                    JOIN projects p ON vv.project_id = p.id
                    WHERE p.user_id = ?
                ) AS total_comments,
                (
                    SELECT COUNT(*)
                    FROM projects
                    WHERE user_id = ? AND status IN ('Approved', 'Published')
                ) AS completed_projects,
                (
                    SELECT COUNT(*)
                    FROM comments cm
                    JOIN video_versions vv ON cm.video_version_id = vv.id
                    JOIN projects p ON vv.project_id = p.id
                    WHERE p.user_id = ? AND cm.type IN ('approve', 'reject')
                ) AS decision_count
            """,
            (user_id, user_id, user_id, user_id, user_id, user_id),
        ).fetchone()

        status_rows = db.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM projects
            WHERE user_id = ?
            GROUP BY status
            ORDER BY count DESC, status ASC
            """,
            (user_id,),
        ).fetchall()

        category_rows = db.execute(
            """
            SELECT category, COUNT(*) AS count
            FROM projects
            WHERE user_id = ?
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
            WHERE c.user_id = ?
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
            WHERE p.user_id = ?
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

    if user["role"] != "owner":
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
            "UPDATE users SET email = ? WHERE id = ?",
            (email_clean, user["id"]),
        )
        if user["role"] == "client" and user["client_reference_id"]:
            db.execute(
                "UPDATE clients SET email = ? WHERE id = ?",
                (email_clean, user["client_reference_id"]),
            )

    return redirect("/account?success=email-updated")


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
                reset_token_expires_at = NULL
            WHERE id = ?
            """,
            (hash_password(new_password), user["id"]),
        )

    return redirect("/account?success=password-updated")


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

    if user["role"] != "owner":
        raise HTTPException(status_code=403, detail="Only studio owners can create clients.")

    if is_demo_user(user):
        return demo_read_only_redirect("/clients")

    if not name or not name.strip():
        return redirect("/clients?error=Client+name+is+required.")

    if not email or not email.strip():
        return redirect("/clients?error=Client+email+is+required.")

    email_clean = email.strip().lower()
    client_password = secrets.token_urlsafe(9)

    try:
        with get_db() as db:
            existing_user = db.execute(
                "SELECT id FROM users WHERE email = ?",
                (email_clean,)
            ).fetchone()

            if existing_user:
                return redirect("/clients?error=This+email+is+already+registered.")

            cur = db.execute(
                "INSERT INTO clients (user_id, name, email, contact, notes, created_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    user["id"],
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
                    created_at
                )
                VALUES (?, ?, 'client', ?, TRUE, ?)
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

    if user["role"] != "owner":
        raise HTTPException(status_code=403, detail="Only studio owners can resend invitations.")

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
                    u.id AS client_user_id
                FROM clients c
                JOIN users u
                    ON u.client_reference_id = c.id
                   AND u.role = 'client'
                WHERE c.id = ? AND c.user_id = ?
                """,
                (client_id, user["id"]),
            ).fetchone()

            if not client:
                raise HTTPException(status_code=404, detail="Client not found.")

            if not client["email"]:
                return redirect("/clients?error=This+client+does+not+have+an+email+address.")

            db.execute(
                """
                UPDATE users
                SET password_hash = ?,
                    reset_token = NULL,
                    reset_token_expires_at = NULL
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

    if user["role"] != "owner":
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
            (user["id"],),
        ).fetchall()
        branding = get_branding_for_user(db, user)

    return templates.TemplateResponse(
        "clients.html",
        {
            "request": request,
            "user": user,
            "clients": clients,
            "branding": branding,
            "is_demo": is_demo_user(user),
            "success": success,
            "error": error,
        },
    )


@app.get("/projects/new", response_class=HTMLResponse)
def new_project_page(
    request: Request,
    error: str = "",
):
    user = require_user(request)

    if owner_needs_setup(user):
        return redirect("/setup")

    if user["role"] != "owner":
        return redirect("/dashboard")

    if is_demo_user(user):
        return demo_read_only_redirect("/dashboard")

    with get_db() as db:
        clients = db.execute(
            "SELECT id, name, email FROM clients WHERE user_id = ? ORDER BY name",
            (user["id"],),
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
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    user = require_user(request)

    if user["role"] != "owner":
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

    with get_db() as db:
        client = db.execute(
            "SELECT id FROM clients WHERE id = ? AND user_id = ?",
            (client_id_int, user["id"]),
        ).fetchone()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        db.execute(
            """
            INSERT INTO projects
            (user_id, client_id, name, category, status, notes, review_token, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                client_id_int,
                name.strip(),
                category,
                status,
                notes.strip(),
                secrets.token_urlsafe(32),
                datetime.utcnow().isoformat(),
            ),
        )

    return redirect("/dashboard?success=Project+created+successfully.")


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: int):
    user = require_user(request)
    with get_db() as db:
        if user["role"] == "client":
            project = db.execute(
                "SELECT p.*, c.name AS client_name FROM projects p JOIN clients c ON p.client_id=c.id WHERE p.id=? AND p.client_id=?",
                (project_id, user["client_reference_id"]),
            ).fetchone()
        else:
            project = db.execute(
                "SELECT p.*, c.name AS client_name FROM projects p JOIN clients c ON p.client_id=c.id WHERE p.id=? AND p.user_id=?",
                (project_id, user["id"]),
            ).fetchone()
            
        if not project:
            return redirect(
                "/dashboard?error=This+project+is+not+available+for+the+active+account."
            )

        if user["role"] == "owner":
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
                cm.author_name, 
                cm.author_role, 
                cm.body, 
                cm.type, 
                cm.is_resolved,
                cm.resolved_at,
                cm.created_at, 
                vv.version_label
            FROM comments cm 
            JOIN video_versions vv ON cm.video_version_id = vv.id
            WHERE vv.project_id = ?
            
            UNION ALL
            
            -- 2. 撈取工作室上傳新影片版本的事件
            SELECT 
                NULL AS id,
                'Studio' AS author_name,
                'studio' AS author_role,
                'Uploaded ' || version_label AS body,
                'upload' AS type,
                FALSE AS is_resolved,
                NULL AS resolved_at,
                created_at,
                version_label
            FROM video_versions
            WHERE project_id = ?
            
            UNION ALL
            
            -- 3. 撈取專案最初建立的事件
            SELECT 
                NULL AS id,
                'System' AS author_name,
                'system' AS author_role,
                'Project Created' AS body,
                'create' AS type,
                FALSE AS is_resolved,
                NULL AS resolved_at,
                created_at,
                '' AS version_label
            FROM projects
            WHERE id = ?
            
            ORDER BY created_at DESC
            """,
            (project_id, project_id, project_id),
        ).fetchall()
        open_feedback_count = sum(
            1
            for comment in comments
            if (
                (
                    comment["author_role"] == "client"
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
        
        # 📂 【新增附件與 Brief 解析邏輯】
        attachments = []
        raw_notes = project["notes"] or ""
        display_notes = raw_notes
        
        if "||" in raw_notes:
            parts = raw_notes.split("||")
            display_notes = parts[0].strip()  # 第一部分是原本的備註文字
            for att in parts[1:]:
                if "::" in att:
                    title, url = att.split("::", 1)
                    attachments.append({"title": title.strip(), "url": url.strip()})
        branding = get_owner_branding(db, project["user_id"])
        
    return templates.TemplateResponse(
        "project.html",
        {
            "request": request,
            "user": user,
            "project": project,
            "branding": branding,
            "display_notes": display_notes,  # 傳遞乾淨的備註文字給前端
            "attachments": attachments,      # 傳遞解析好的附件清單給前端
            "versions": versions,
            "comments": comments,
            "open_feedback_count": open_feedback_count,
            "status_options": STATUS_OPTIONS,
            "is_public_link": False,
            "is_demo": is_demo_user(user),
        },
    )


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
    if user["role"] != "owner":
        raise HTTPException(status_code=403, detail="Clients cannot upload versions.")

    if is_demo_user(user):
        return demo_read_only_redirect(f"/projects/{project_id}")

    with get_db() as db:
        project = db.execute(
            "SELECT id FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found.")

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
            (user_id, project_id, version_label, video_url, status, notes, created_at)
            VALUES (?, ?, ?, ?, 'Awaiting Review', ?, ?)
            """,
            (
                user["id"],
                project_id,
                version_label.strip(),
                final_video_url,
                notes.strip(),
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
def version_decision(
    request: Request,
    version_id: int,
    background_tasks: BackgroundTasks,
    action_type: str = Form(...),
    body: str = Form(...),
    video_time: Optional[str] = Form(None),
    time_str: Optional[str] = Form(None),
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

        author_role, author_name = get_review_actor(
            user,
            project,
            review_token,
            action_type,
        )
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

        if time_str and time_str.strip() and video_time:
            final_body = f"⏱️ [{time_str.strip()}] {final_body}"
            if action_type == "comment":
                final_type = f"timestamp_{video_time}"

        now = datetime.utcnow().isoformat()

        db.execute(
            """
            INSERT INTO comments
            (video_version_id, author_role, author_name, body, type, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (version_id, author_role, author_name, final_body, final_type, now),
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
                WHERE author_role = 'client'
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

        if project["owner_email"] and author_role == "client":
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
                    f"Client ({author_name}) has submitted an action "
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
    if user["role"] != "owner":
        raise HTTPException(
            status_code=403,
            detail="Only owners can update feedback status.",
        )
    if is_demo_user(user):
        return demo_read_only_redirect("/dashboard")

    with get_db() as db:
        comment = db.execute(
            """
            SELECT cm.id, cm.type, cm.author_role, cm.is_resolved,
                   p.id AS project_id, p.status AS project_status
            FROM comments cm
            JOIN video_versions vv ON cm.video_version_id = vv.id
            JOIN projects p ON vv.project_id = p.id
            WHERE cm.id = ? AND p.user_id = ?
            """,
            (comment_id, user["id"]),
        ).fetchone()
        if not comment:
            raise HTTPException(status_code=404, detail="Feedback not found.")
        if comment["project_status"] in {"Approved", "Published"}:
            raise HTTPException(
                status_code=400,
                detail="Approved projects are read-only.",
            )
        if (
            comment["author_role"] != "client"
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
        project_id = project["id"]
        versions = db.execute("SELECT * FROM video_versions WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
        comments = db.execute(
            """SELECT cm.id, cm.author_name, cm.author_role, cm.body, cm.type,
                      cm.is_resolved, cm.resolved_at, cm.created_at, vv.version_label
               FROM comments cm JOIN video_versions vv ON cm.video_version_id = vv.id WHERE vv.project_id = ?
               UNION ALL
               SELECT NULL AS id, 'Studio' AS author_name, 'studio' AS author_role,
                      'Uploaded ' || version_label AS body, 'upload' AS type,
                      FALSE AS is_resolved, NULL AS resolved_at, created_at, version_label
               FROM video_versions WHERE project_id = ?
               UNION ALL
               SELECT NULL AS id, 'System' AS author_name, 'system' AS author_role,
                      'Project Created' AS body, 'create' AS type,
                      FALSE AS is_resolved, NULL AS resolved_at, created_at, '' AS version_label
               FROM projects WHERE id = ?
               ORDER BY created_at DESC""", (project_id, project_id, project_id)
        ).fetchall()
        open_feedback_count = sum(
            1
            for comment in comments
            if (
                (
                    comment["author_role"] == "client"
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

        # 📂 公開頁面同步解析附件
        attachments = []
        raw_notes = project["notes"] or ""
        display_notes = raw_notes
        if "||" in raw_notes:
            parts = raw_notes.split("||")
            display_notes = parts[0].strip()
            for att in parts[1:]:
                if "::" in att:
                    title, url = att.split("::", 1)
                    attachments.append({"title": title.strip(), "url": url.strip()})
        branding = get_owner_branding(db, project["user_id"])

    return templates.TemplateResponse(
        "project.html",
        {
            "request": request,
            "user": {"role": "client", "email": "Public Reviewer"},
            "project": project,
            "branding": branding,
            "display_notes": display_notes,
            "attachments": attachments,
            "versions": versions,
            "comments": comments,
            "open_feedback_count": open_feedback_count,
            "status_options": STATUS_OPTIONS,
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
    if user["role"] != "owner":
        raise HTTPException(status_code=403, detail="Only owners can mark final delivery.")

    if is_demo_user(user):
        return demo_read_only_redirect(f"/projects/{project_id}")
        
    with get_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchone()
        if not project:
            raise HTTPException(status_code=404)
            
        # 將專案狀態更新為 Published (代表最終交付結案)
        db.execute("UPDATE projects SET status='Published' WHERE id=?", (project_id,))
        
        # 📬 【加分功能】：同時自動觸發一封結案信通知客戶前來下載最終成片！
        branding = get_owner_branding(db, user["id"])
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
    if user["role"] != "owner":
        raise HTTPException(status_code=403)

    if is_demo_user(user):
        return demo_read_only_redirect(f"/projects/{project_id}")

    with get_db() as db:
        owned_project = db.execute(
            "SELECT id FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchone()
        if not owned_project:
            raise HTTPException(status_code=404, detail="Project not found.")

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

    public_url = f"{SUPABASE_URL}/storage/v1/object/public/attachments/{storage_path}"

    with get_db() as db:
        project = db.execute("SELECT notes FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found.")

        current_notes = project["notes"] or ""
        new_notes = f"{current_notes} || {safe_title} :: {public_url}"

        db.execute(
            "UPDATE projects SET notes=? WHERE id=?",
            (new_notes, project_id),
        )

    return redirect(f"/projects/{project_id}")

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
