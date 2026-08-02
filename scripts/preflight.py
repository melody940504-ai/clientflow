"""Check production configuration without printing secrets."""

import os

import httpx
import psycopg2
from dotenv import load_dotenv


def status(label: str, ok: bool, detail: str) -> bool:
    print(f"[{'OK' if ok else 'FAIL'}] {label}: {detail}")
    return ok


def main() -> int:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL", "").strip()
    session_secret = os.getenv("SESSION_SECRET", "")
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_KEY", "").strip()
    checks = [
        status("SESSION_SECRET", len(session_secret) >= 32, "configured with at least 32 characters"),
        status("DATABASE_URL", bool(database_url), "configured"),
        status("SUPABASE_URL", bool(supabase_url), "configured"),
        status("SUPABASE_KEY", bool(supabase_key), "configured"),
        status(
            "Private attachment mode",
            os.getenv("ATTACHMENTS_BUCKET_PRIVATE", "true").lower() in {"1", "true", "yes", "on"},
            "enabled",
        ),
    ]

    if database_url:
        try:
            connection = psycopg2.connect(
                database_url,
                sslmode=os.getenv("DATABASE_SSLMODE", "require"),
            )
            cursor = connection.cursor()
            cursor.execute("SELECT version_num FROM alembic_version")
            revision = cursor.fetchone()
            checks.append(status("Database migration", bool(revision), revision[0] if revision else "missing"))
            cursor.close()
            connection.close()
        except Exception as exc:
            checks.append(status("Database migration", False, type(exc).__name__))

    if supabase_url and supabase_key:
        try:
            response = httpx.get(
                f"{supabase_url}/storage/v1/bucket/attachments",
                headers={"Authorization": f"Bearer {supabase_key}", "apikey": supabase_key},
                timeout=20,
            )
            response.raise_for_status()
            bucket = response.json()
            checks.append(status("Attachments bucket", bucket.get("public") is False, "private" if bucket.get("public") is False else "public"))
        except Exception as exc:
            checks.append(status("Attachments bucket", False, type(exc).__name__))

    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
