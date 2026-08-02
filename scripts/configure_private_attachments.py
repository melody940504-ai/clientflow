"""Convert legacy attachment URLs and make the Supabase bucket private.

Run without --apply first. The command aborts when it finds a URL that cannot
be mapped safely to the configured Supabase attachments bucket.
"""

import argparse
import os
from urllib.parse import unquote, urlparse

import httpx
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor


def storage_path_from_url(url: str, supabase_url: str) -> str | None:
    parsed = urlparse((url or "").strip())
    expected = urlparse(supabase_url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc != expected.netloc:
        return None
    marker = "/storage/v1/object/public/attachments/"
    if marker not in parsed.path:
        return None
    path = unquote(parsed.path.split(marker, 1)[1]).strip("/")
    if not path or ".." in path.split("/"):
        return None
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply database updates and set the attachments bucket to private.",
    )
    args = parser.parse_args()
    load_dotenv()

    database_url = os.getenv("DATABASE_URL", "").strip()
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_KEY", "").strip()
    if not database_url or not supabase_url or not supabase_key:
        raise SystemExit("DATABASE_URL, SUPABASE_URL, and SUPABASE_KEY are required.")

    connection = psycopg2.connect(
        database_url,
        cursor_factory=RealDictCursor,
        sslmode=os.getenv("DATABASE_SSLMODE", "require"),
    )
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT id, public_url
            FROM project_attachments
            WHERE (storage_path IS NULL OR storage_path = '')
              AND public_url IS NOT NULL AND public_url <> ''
            """
        )
        project_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT id, attachment_url
            FROM comments
            WHERE (attachment_storage_path IS NULL OR attachment_storage_path = '')
              AND attachment_url IS NOT NULL AND attachment_url <> ''
            """
        )
        comment_rows = cursor.fetchall()

        project_updates = [
            (storage_path_from_url(row["public_url"], supabase_url), row["id"])
            for row in project_rows
        ]
        comment_updates = [
            (storage_path_from_url(row["attachment_url"], supabase_url), row["id"])
            for row in comment_rows
        ]
        unresolved = sum(path is None for path, _ in project_updates + comment_updates)
        print(f"Project attachments to migrate: {len(project_updates)}")
        print(f"Comment attachments to migrate: {len(comment_updates)}")
        print(f"Unresolved external or malformed URLs: {unresolved}")
        if unresolved:
            raise SystemExit(
                "No changes made. Resolve or remove every unrecognized attachment URL first."
            )
        if not args.apply:
            print("Dry run complete. Re-run with --apply to perform the migration.")
            return 0

        headers = {
            "Authorization": f"Bearer {supabase_key}",
            "apikey": supabase_key,
            "Content-Type": "application/json",
        }
        response = httpx.put(
            f"{supabase_url}/storage/v1/bucket/attachments",
            headers=headers,
            json={"public": False},
            timeout=30,
        )
        response.raise_for_status()

        for path, row_id in project_updates:
            cursor.execute(
                """
                UPDATE project_attachments
                SET storage_path = %s, public_url = NULL
                WHERE id = %s
                """,
                (path, row_id),
            )
        for path, row_id in comment_updates:
            cursor.execute(
                """
                UPDATE comments
                SET attachment_storage_path = %s, attachment_url = NULL
                WHERE id = %s
                """,
                (path, row_id),
            )
        connection.commit()
        print("Attachments bucket is private and legacy URLs have been migrated.")
        return 0
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
