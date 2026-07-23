from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request, Response, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
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
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "clientflow.db"
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_urlsafe(32)
serializer = URLSafeSerializer(SESSION_SECRET, salt="clientflow-session")

app = FastAPI(title="Lumaire")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="oauth_session"
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
DEFAULT_BRAND_COLOR = "#6366f1"
DEFAULT_EMAIL_SENDER_NAME = "Lumaire"
EMAIL_TEST_RECIPIENT = os.getenv("EMAIL_TEST_RECIPIENT", "").strip()
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

# ==========================================
# 📬 Email 自動通知模擬引擎
# ==========================================
def send_activity_email(
    to_email: str,
    subject: str,
    project_name: str,
    action_text: str,
    link_url: str
):
    try:
        recipient = EMAIL_TEST_RECIPIENT or to_email

        resend.Emails.send({
            "from": "Lumaire <onboarding@resend.dev>",
            "to": [recipient],
            "subject": subject,
            "html": f"""
            <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">

                <h2 style="color:#4f46e5;">
                    Lumaire Notification
                </h2>

                <p>
                    <strong>Project:</strong>
                    {project_name}
                </p>

                <p>
                    {action_text}
                </p>

                <p style="margin-top:24px">
                    <a
                        href="{link_url}"
                        style="
                            background:#4f46e5;
                            color:white;
                            padding:12px 20px;
                            text-decoration:none;
                            border-radius:8px;
                            display:inline-block;
                        "
                    >
                        Open Project
                    </a>
                </p>

            </div>
            """
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
):
    try:
        recipient = EMAIL_TEST_RECIPIENT or to_email

        resend.Emails.send({
            "from": f"{sender_name} <onboarding@resend.dev>",
            "to": [recipient],
            "subject": "You have been invited to Lumaire",
            "html": f"""
            <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">

                <h2 style="color:#4f46e5;">
                    Welcome to Lumaire
                </h2>

                <p>
                    Hi {client_name},
                </p>

                <p>
                    You have been invited to review projects on Lumaire.
                </p>

                <div style="
                    background:#f5f5f5;
                    padding:16px;
                    border-radius:8px;
                    margin:20px 0;
                    color:#111827;
                ">
                    <p><strong>Login Email:</strong> {login_email}</p>
                    <p><strong>Temporary Password:</strong> {temporary_password}</p>
                </div>

                <p>
                    Use the button below to access your client portal.
                </p>

                <p style="margin-top:24px">
                    <a
                        href="{login_url}"
                        style="
                            background:#4f46e5;
                            color:white;
                            padding:12px 20px;
                            text-decoration:none;
                            border-radius:8px;
                            display:inline-block;
                        "
                    >
                        Open Client Portal
                    </a>
                </p>

                <p style="font-size:12px;color:#6b7280;margin-top:24px;">
                    This is a test invitation sent by Lumaire.
                </p>

            </div>
            """
        })

        print(f"Client invitation email sent successfully to {recipient}")

    except Exception as e:
        print(f"❌ Client invitation email failed: {e}")

# 🎯 取得當前這個 main.py 檔案所在的資料夾絕對路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 🎯 不論是在本機 Windows 還是雲端 Linux，都能精準拼出正確的資料庫絕對路徑
DB_PATH = os.path.join(BASE_DIR, "database.db")

def send_password_reset_email(to_email: str, reset_url: str):
    try:
        recipient = EMAIL_TEST_RECIPIENT or to_email

        resend.Emails.send({
            "from": "Lumaire <onboarding@resend.dev>",
            "to": [recipient],
            "subject": "Reset your Lumaire password",
            "html": f"""
            <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">

                <h2 style="color:#4f46e5;">
                    Reset your Lumaire password
                </h2>

                <p>
                    We received a request to reset your Lumaire password.
                </p>

                <p style="margin-top:24px">
                    <a
                        href="{reset_url}"
                        style="
                            background:#4f46e5;
                            color:white;
                            padding:12px 20px;
                            text-decoration:none;
                            border-radius:8px;
                            display:inline-block;
                        "
                    >
                        Reset Password
                    </a>
                </p>

                <p style="font-size:12px;color:#6b7280;margin-top:24px;">
                    This link expires in 1 hour. If you did not request a password reset, you can safely ignore this email.
                </p>

            </div>
            """
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
                brand_color TEXT DEFAULT '#6366f1',
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
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS brand_color TEXT DEFAULT '#6366f1'"
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
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(client_id) REFERENCES clients(id)
            )
        """)

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
                created_at TEXT NOT NULL,
                FOREIGN KEY(video_version_id) REFERENCES video_versions(id)
            )
        """)


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
    salt = secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest() == digest

def get_current_user(request: Request) -> Optional[sqlite3.Row]:
    token = request.cookies.get("session")
    if not token:
        return None
    try:
        user_id = serializer.loads(token)
    except BadSignature:
        return None
    with get_db() as db:
        return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

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
def register(request: Request, email: str = Form(...), password: str = Form(...)):
    if len(password) < 6:
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

    send_activity_email(
        email.strip().lower(),
        "Verify your email",
        "Lumaire",
        f"Please verify your email address.\n\n{verify_url}",
        verify_url
    )

    return redirect("/login?success=verification-sent")


import logging

# 加入這行來設定記錄器，這樣我們能在 Render 的 Logs 看到後端發生什麼
logger = logging.getLogger("uvicorn.error")

@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
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
    
    response = RedirectResponse(url=post_login_path(user), status_code=303)
    response.set_cookie(
        "session",
        serializer.dumps(user["id"]),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@app.post("/demo-login/{role}")
def demo_login(request: Request, role: str):
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

    response = redirect(post_login_path(user))
    response.set_cookie(
        "session",
        serializer.dumps(user["id"]),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response

@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return templates.TemplateResponse(
        "forgot_password.html",
        {"request": request, "user": None, "demo_enabled": DEMO_ENABLED},
    )

@app.post("/forgot-password")
def forgot_password(request: Request, email: str = Form(...)):
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
            send_password_reset_email(email_clean, reset_url)

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
def reset_password(token: str, password: str = Form(...)):
    if len(password) < 6:
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
    response.set_cookie(
        "session",
        serializer.dumps(user["id"]),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )

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

@app.get("/logout")
def logout():
    response = redirect("/")
    response.delete_cookie("session")
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
                    (SELECT COUNT(*) FROM video_versions v WHERE v.project_id = p.id) AS version_count,
                    (SELECT COUNT(*) FROM comments cm JOIN video_versions vv ON cm.video_version_id = vv.id WHERE vv.project_id = p.id) AS comment_count
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
                    (SELECT COUNT(*) FROM video_versions v WHERE v.project_id = p.id) AS version_count,
                    (SELECT COUNT(*) FROM comments cm JOIN video_versions vv ON cm.video_version_id = vv.id WHERE vv.project_id = p.id) AS comment_count
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

        if user["role"] == "client":
            notification_where = "p.client_id = ?"
            notification_params = [user["client_reference_id"]] * 3
        else:
            notification_where = "p.user_id = ?"
            notification_params = [user["id"]] * 3

        notifications = db.execute(
            f"""
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
            WHERE {notification_where}

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
            WHERE {notification_where}

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
            WHERE {notification_where}

            ORDER BY created_at DESC
            LIMIT 8
            """,
            notification_params,
        ).fetchall()
        
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
):
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
):
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
):
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
):
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
    notes: str = Form("")
):
    user = require_user(request)

    if user["role"] != "owner":
        raise HTTPException(status_code=403, detail="Only studio owners can create clients.")

    if is_demo_user(user):
        return demo_read_only_redirect("/dashboard")

    if not name or not name.strip():
        return redirect("/dashboard?error=Client+name+is+required.")

    if not email or not email.strip():
        return redirect("/dashboard?error=Client+email+is+required.")

    email_clean = email.strip().lower()
    client_password = secrets.token_hex(4)

    with get_db() as db:
        existing_user = db.execute(
            "SELECT id FROM users WHERE email = ?",
            (email_clean,)
        ).fetchone()

        if existing_user:
            return redirect("/dashboard?error=This+email+is+already+registered.")
        
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

        login_email = email_clean

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
                login_email,
                hash_password(client_password),
                client_id,
                datetime.utcnow().isoformat(),
            ),
        )

        branding = get_branding_for_user(db, user)
        send_client_invitation_email(
            to_email=login_email,
            client_name=name.strip(),
            login_email=login_email,
            temporary_password=client_password,
            login_url=str(request.base_url),
            sender_name=branding["email_sender_name"],
        )

    return redirect("/dashboard")


@app.post("/projects")
def create_project(
    request: Request,
    client_id: str = Form(""),
    name: str = Form(""),
    category: str = Form("Shorts"),
    status: str = Form("Awaiting Review"),
    notes: str = Form(""),
):
    user = require_user(request)

    if user["role"] != "owner":
        raise HTTPException(status_code=403)

    if is_demo_user(user):
        return demo_read_only_redirect("/dashboard")

    if not client_id or not client_id.strip():
        return redirect("/dashboard?error=Please+select+a+client.")

    try:
        client_id_int = int(client_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid client selected.")

    if not name or not name.strip():
        return redirect("/dashboard?error=Project+title+is+required.")

    with get_db() as db:
        client = db.execute(
            "SELECT id FROM clients WHERE id = ? AND user_id = ?",
            (client_id_int, user["id"]),
        ).fetchone()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        db.execute(
            "INSERT INTO projects (user_id, client_id, name, category, status, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user["id"],
                client_id_int,
                name.strip(),
                category,
                status,
                notes.strip(),
                datetime.utcnow().isoformat(),
            ),
        )

    return redirect("/dashboard")


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
            raise HTTPException(status_code=404, detail="Project not found")
            
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
                cm.author_name, 
                cm.author_role, 
                cm.body, 
                cm.type, 
                cm.created_at, 
                vv.version_label
            FROM comments cm 
            JOIN video_versions vv ON cm.video_version_id = vv.id
            WHERE vv.project_id = ?
            
            UNION ALL
            
            -- 2. 撈取工作室上傳新影片版本的事件
            SELECT 
                'Studio' AS author_name,
                'studio' AS author_role,
                'Uploaded ' || version_label AS body,
                'upload' AS type,
                created_at,
                version_label
            FROM video_versions
            WHERE project_id = ?
            
            UNION ALL
            
            -- 3. 撈取專案最初建立的事件
            SELECT 
                'System' AS author_name,
                'system' AS author_role,
                'Project Created' AS body,
                'create' AS type,
                created_at,
                '' AS version_label
            FROM projects
            WHERE id = ?
            
            ORDER BY created_at DESC
            """,
            (project_id, project_id, project_id),
        ).fetchall()
        
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
            "status_options": STATUS_OPTIONS,
            "is_public_link": False,
            "is_demo": is_demo_user(user),
        },
    )


@app.post("/projects/{project_id}/versions")
async def create_version(
    request: Request,
    project_id: int,
    version_label: str = Form(""),
    video_url: str = Form(""),
    video_file: UploadFile = File(None),
    notes: str = Form(""),
):
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

        video_bytes = await video_file.read()

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
            SELECT p.name AS p_name, c.email AS c_email
            FROM projects p
            JOIN clients c ON p.client_id=c.id
            WHERE p.id=?
            """,
            (project_id,),
        ).fetchone()

        if project_info and project_info["c_email"]:
            public_review_url = f"{request.base_url}review/{project_id}"

            send_activity_email(
                to_email=project_info["c_email"],
                subject=f"[Lumaire] New version {version_label.strip()} uploaded",
                project_name=project_info["p_name"],
                action_text=f"Studio uploaded a new version ({version_label.strip()}). Please review it when available.",
                link_url=public_review_url,
            )

    return redirect(f"/projects/{project_id}?success=Version+uploaded+successfully.")


@app.post("/versions/{version_id}/action")
def version_decision(
    request: Request,
    version_id: int,
    action_type: str = Form(...),
    body: str = Form(...),
    video_time: Optional[str] = Form(None),
    time_str: Optional[str] = Form(None),
):
    user = get_current_user(request)

    if action_type not in {"comment", "approve", "reject"}:
        raise HTTPException(status_code=400, detail="Invalid review action.")

    with get_db() as db:
        if user:
            author_role = "client" if user["role"] == "client" else "studio"
            if user["role"] == "client" and user["client_reference_id"]:
                client = db.execute(
                    "SELECT name FROM clients WHERE id = ?",
                    (user["client_reference_id"],),
                ).fetchone()
                author_name = client["name"] if client else "Client"
            else:
                author_name = normalize_branding(user)["studio_name"]
        else:
            author_role = "client"
            author_name = "Anonymous Client"

        version = db.execute(
            "SELECT * FROM video_versions WHERE id = ?",
            (version_id,)
        ).fetchone()

        if not version:
            raise HTTPException(status_code=404)

        project_owner = db.execute(
            """
            SELECT u.email
            FROM projects p
            JOIN users u ON p.user_id = u.id
            WHERE p.id = ?
            """,
            (version["project_id"],),
        ).fetchone()
        if project_owner and is_demo_email(project_owner["email"]):
            target_path = (
                f"/projects/{version['project_id']}"
                if user
                else f"/review/{version['project_id']}"
            )
            return demo_read_only_redirect(target_path)

        if not body or not body.strip():
            target_path = f"/projects/{version['project_id']}" if user else f"/review/{version['project_id']}"
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

        elif action_type == "reject":
            db.execute(
                "UPDATE video_versions SET status = 'Revision Requested' WHERE id = ?",
                (version_id,)
            )
            db.execute(
                "UPDATE projects SET status = 'In Revision' WHERE id = ?",
                (version["project_id"],)
            )

        studio_info = db.execute(
            """
            SELECT p.name AS p_name, u.email AS u_email
            FROM projects p
            JOIN users u ON p.user_id = u.id
            WHERE p.id = ?
            """,
            (version["project_id"],)
        ).fetchone()

        if studio_info:
            project_url = f"{request.base_url}projects/{version['project_id']}"
            status_emojis = {
                "approve": "✅ Approved",
                "reject": "❌ Change Requested",
                "comment": "💬 New Comment",
            }
            action_display = status_emojis.get(action_type, action_type)

            send_activity_email(
                to_email=studio_info["u_email"],
                subject=f"[Lumaire] Project Activity Update: {action_display}",
                project_name=studio_info["p_name"],
                action_text=(
                    f"Client ({author_name}) has submitted an action "
                    f"[{action_display}] on {version['version_label']}.\n"
                    f"Feedback: \"{final_body}\""
                ),
                link_url=project_url,
            )

    if not user:
        return redirect(f"/review/{version['project_id']}")

    return redirect(f"/projects/{version['project_id']}")


# ==========================================
# 客戶免登入公開審片連結路由 (Frame.io 模式)
# ==========================================

@app.get("/review/{project_id}", response_class=HTMLResponse)
def public_review_page(request: Request, project_id: int):
    with get_db() as db:
        project = db.execute(
            """
            SELECT p.*, c.name AS client_name, u.email AS owner_email
            FROM projects p
            JOIN clients c ON p.client_id = c.id
            JOIN users u ON p.user_id = u.id
            WHERE p.id = ?
            """,
            (project_id,),
        ).fetchone()
        if not project: raise HTTPException(status_code=404, detail="Review link invalid or expired")
        versions = db.execute("SELECT * FROM video_versions WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
        comments = db.execute(
            """SELECT cm.author_name, cm.author_role, cm.body, cm.type, cm.created_at, vv.version_label
               FROM comments cm JOIN video_versions vv ON cm.video_version_id = vv.id WHERE vv.project_id = ?
               UNION ALL
               SELECT 'Studio' AS author_name, 'studio' AS author_role, 'Uploaded ' || version_label AS body, 'upload' AS type, created_at, version_label
               FROM video_versions WHERE project_id = ?
               UNION ALL
               SELECT 'System' AS author_name, 'system' AS author_role, 'Project Created' AS body, 'create' AS type, created_at, '' AS version_label
               FROM projects WHERE id = ?
               ORDER BY created_at DESC""", (project_id, project_id, project_id)
        ).fetchall()

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
            "status_options": STATUS_OPTIONS,
            "is_public_link": True,
            "is_demo": is_demo_email(project["owner_email"]),
        },
    )


# ==========================================
# 專案最終交付結案路由
# ==========================================
@app.post("/projects/{project_id}/deliver")
def deliver_project(project_id: int, request: Request):
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
        client_info = db.execute("SELECT email FROM clients WHERE id=?", (project["client_id"],)).fetchone()
        if client_info and client_info["email"]:
            send_activity_email(
                to_email=client_info["email"],
                subject=f"[Lumaire] Final Delivery Completed for '{project['name']}'!",
                project_name=project["name"],
                action_text="Studio has marked this project as Final Delivered! All approved master files have been successfully dispatched and archived.",
                link_url=f"{request.base_url}review/{project_id}"
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
):
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

    file_bytes = await file.read()

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
