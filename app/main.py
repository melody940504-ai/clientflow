from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime
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
SECRET_KEY = "change-this-secret-before-deployment"
serializer = URLSafeSerializer(SECRET_KEY, salt="clientflow-session")

app = FastAPI(title="ClientFlow MVP")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "clientflow-dev-secret"),
    session_cookie="oauth_session"
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

DATABASE_URL = os.environ.get("DATABASE_URL")
STATUS_OPTIONS = ["Awaiting Review", "In Revision", "Approved", "Published"]
CATEGORY_OPTIONS = ["Shorts", "Reels", "TikTok", "Ad", "YouTube", "Other"]
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

        TEST_EMAIL = "melody940504@gmail.com"

        resend.Emails.send({
            "from": "ClientFlow <onboarding@resend.dev>",
            "to": [TEST_EMAIL],
            "subject": subject,
            "html": f"""
            <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">

                <h2 style="color:#4f46e5;">
                    🎬 ClientFlow Notification
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

        print(f"📬 Email sent successfully → {TEST_EMAIL}")

    except Exception as e:
        print(f"❌ Email failed: {e}")

def send_client_invitation_email(
    to_email: str,
    client_name: str,
    login_email: str,
    temporary_password: str,
    login_url: str,
):
    try:
        TEST_EMAIL = "melody940504@gmail.com"

        resend.Emails.send({
            "from": "ClientFlow <onboarding@resend.dev>",
            "to": [TEST_EMAIL],
            "subject": "You have been invited to ClientFlow",
            "html": f"""
            <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">

                <h2 style="color:#4f46e5;">
                    Welcome to ClientFlow
                </h2>

                <p>
                    Hi {client_name},
                </p>

                <p>
                    You have been invited to review projects on ClientFlow.
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
                    This is a test invitation sent by ClientFlow.
                </p>

            </div>
            """
        })

        print(f"📬 Client invitation email sent successfully → {TEST_EMAIL}")

    except Exception as e:
        print(f"❌ Client invitation email failed: {e}")

# 🎯 取得當前這個 main.py 檔案所在的資料夾絕對路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 🎯 不論是在本機 Windows 還是雲端 Linux，都能精準拼出正確的資料庫絕對路徑
DB_PATH = os.path.join(BASE_DIR, "database.db")

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

@app.on_event("startup")
def startup() -> None:
    init_db()

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
        raise HTTPException(status_code=401)
    return user

def redirect(path: str):
    return RedirectResponse(path, status_code=303)

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = get_current_user(request)
    if user:
        return redirect("/dashboard")
    return templates.TemplateResponse("login.html", {"request": request, "mode": "login", "error": None})

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "mode": "register", "error": None})

@app.post("/register")
def register(email: str = Form(...), password: str = Form(...)):
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
    
    verify_url = (
        f"https://clientflow-q250.onrender.com/verify-email/"
        f"{verification_token}"
    )

    send_activity_email(
        email.strip().lower(),
        "Verify your email",
        "ClientFlow",
        f"Please verify your email address.\n\n{verify_url}",
        verify_url
    )

    return redirect("/?success=verification-sent")


import logging

# 加入這行來設定記錄器，這樣我們能在 Render 的 Logs 看到後端發生什麼
logger = logging.getLogger("uvicorn.error")

@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    logger.info(f"Login attempt for: {email}") # 這行會出現在 Logs
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    
    if not user:
        return RedirectResponse(url="/?error=no_account", status_code=303)
    
    if not user["is_verified"]:
        return RedirectResponse(
            url="/?error=email-not-verified",
            status_code=303
        )   
        
    if not verify_password(password, user["password_hash"]):
        return RedirectResponse(url="/?error=wrong_password", status_code=303)
    
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie("session", serializer.dumps(user["id"]), httponly=True, samesite="lax")
    return response

@app.get("/login/google")
async def login_google(request: Request):
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
        return RedirectResponse(url="/?error=google_login_failed", status_code=303)

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
                VALUES (?, '', 'owner', FALSE, ?)
                """,
                (email, datetime.utcnow().isoformat())
            )

            user = db.execute(
                "SELECT * FROM users WHERE email = ?",
                (email,)
            ).fetchone()

    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        "session",
        serializer.dumps(user["id"]),
        httponly=True,
        samesite="lax"
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
            return redirect("/?error=invalid-verification-link")

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

    return redirect("/?success=email-verified")

@app.get("/logout")
def logout():
    response = redirect("/")
    response.delete_cookie("session")
    return response

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "all", category: str = "all", client_id: str = "all"):
    user = require_user(request)
    
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

    # 從 clients.notes 解析測試用客戶密碼，供 dashboard.html 顯示 c.raw_password
    parsed_clients = []
    for c in clients:
        c_dict = dict(c)
        raw_password = ""

        notes_text = c_dict.get("notes") or ""
        if "Password:" in notes_text:
            raw_password = notes_text.split("Password:", 1)[1].split("\n", 1)[0].strip()
        elif "密碼:" in notes_text:
            raw_password = notes_text.split("密碼:", 1)[1].split("\n", 1)[0].strip()
        
        c_dict["raw_password"] = raw_password
        parsed_clients.append(c_dict)

    clients = parsed_clients

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "clients": clients,
            "projects": projects,
            "stats": stats,
            "status_options": STATUS_OPTIONS,
            "category_options": CATEGORY_OPTIONS,
            "selected_status": status,
            "selected_category": category,
            "selected_client_id": client_id,
        },
    )


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

        generated_notes = (
            f"[System-generated credentials] Account: {login_email} | Password: {client_password}\n"
            f"{notes.strip()}"
        )

        db.execute(
            "UPDATE clients SET notes=? WHERE id=?",
            (generated_notes, client_id)
        )

        send_client_invitation_email(
            to_email=login_email,
            client_name=name.strip(),
            login_email=login_email,
            temporary_password=client_password,
            login_url="https://clientflow-q250.onrender.com",
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

    if not client_id or not client_id.strip():
        return redirect("/dashboard?error=Please+select+a+client.")

    try:
        client_id_int = int(client_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid client selected.")

    if not name or not name.strip():
        return redirect("/dashboard?error=Project+title+is+required.")

    with get_db() as db:
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
        
    return templates.TemplateResponse(
        "project.html",
        {
            "request": request,
            "user": user,
            "project": project,
            "display_notes": display_notes,  # 傳遞乾淨的備註文字給前端
            "attachments": attachments,      # 傳遞解析好的附件清單給前端
            "versions": versions,
            "comments": comments,
            "status_options": STATUS_OPTIONS,
            "is_public_link": False          # 代表是後台登入模式
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
                subject=f"[ClientFlow] New version {version_label.strip()} uploaded",
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

    if user:
        author_role = "client" if user["role"] == "client" else "studio"
        author_name = user["email"].split("@")[0]
    else:
        author_role = "client"
        author_name = "Anonymous Client"

    with get_db() as db:
        version = db.execute(
            "SELECT * FROM video_versions WHERE id = ?",
            (version_id,)
        ).fetchone()

        if not version:
            raise HTTPException(status_code=404)

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
                subject=f"📊 [ClientFlow] Project Activity Update: {action_display}",
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
        project = db.execute("SELECT p.*, c.name AS client_name FROM projects p JOIN clients c ON p.client_id=c.id WHERE p.id=?", (project_id,)).fetchone()
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

    return templates.TemplateResponse(
        "project.html",
        {
            "request": request,
            "user": {"role": "client", "email": "Public Reviewer"},
            "project": project,
            "display_notes": display_notes,
            "attachments": attachments,
            "versions": versions,
            "comments": comments,
            "status_options": STATUS_OPTIONS,
            "is_public_link": True
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
        
    with get_db() as db:
        project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404)
            
        # 將專案狀態更新為 Published (代表最終交付結案)
        db.execute("UPDATE projects SET status='Published' WHERE id=?", (project_id,))
        
        # 📬 【加分功能】：同時自動觸發一封結案信通知客戶前來下載最終成片！
        client_info = db.execute("SELECT email FROM clients WHERE id=?", (project["client_id"],)).fetchone()
        if client_info and client_info["email"]:
            send_activity_email(
                to_email=client_info["email"],
                subject=f"🎉 [ClientFlow] Final Delivery Completed for '{project['name']}'!",
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
    if not DATABASE_URL:
        return {"ok": False, "error": "DATABASE_URL not set"}

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    cur = conn.cursor()
    cur.execute("SELECT NOW() AS now")
    row = cur.fetchone()
    cur.close()
    conn.close()

    return {"ok": True, "now": str(row["now"])}