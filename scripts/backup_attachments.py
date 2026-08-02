"""Download the private Supabase attachments bucket into a ZIP archive."""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
from dotenv import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="backups")
    args = parser.parse_args()
    load_dotenv()
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        raise SystemExit("SUPABASE_URL and SUPABASE_KEY are required.")

    headers = {"Authorization": f"Bearer {supabase_key}", "apikey": supabase_key}
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"lumaire-attachments-{timestamp}.zip"

    def list_objects(client: httpx.Client, prefix: str = ""):
        offset = 0
        while True:
            response = client.post(
                f"{supabase_url}/storage/v1/object/list/attachments",
                headers=headers,
                json={
                    "prefix": prefix,
                    "limit": 100,
                    "offset": offset,
                    "sortBy": {"column": "name", "order": "asc"},
                },
            )
            response.raise_for_status()
            entries = response.json()
            for entry in entries:
                name = str(entry.get("name") or "")
                object_path = f"{prefix}/{name}".strip("/")
                if entry.get("id"):
                    yield object_path
                elif name:
                    yield from list_objects(client, object_path)
            if len(entries) < 100:
                break
            offset += len(entries)

    count = 0
    with httpx.Client(timeout=60) as client, ZipFile(
        output_path, "w", compression=ZIP_DEFLATED
    ) as archive:
        for object_path in list_objects(client):
            safe_path = PurePosixPath(object_path)
            if safe_path.is_absolute() or ".." in safe_path.parts:
                raise RuntimeError(f"Unsafe object path returned by storage: {object_path}")
            sign_response = client.post(
                f"{supabase_url}/storage/v1/object/sign/attachments/{quote(object_path, safe='/')}",
                headers=headers,
                json={"expiresIn": 300},
            )
            sign_response.raise_for_status()
            signed_path = sign_response.json().get("signedURL", "")
            download_url = (
                signed_path
                if signed_path.startswith(("http://", "https://"))
                else f"{supabase_url}/storage/v1{signed_path}"
            )
            download_response = client.get(download_url)
            download_response.raise_for_status()
            archive.writestr(str(safe_path), download_response.content)
            count += 1

    print(f"{output_path} ({count} objects)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
