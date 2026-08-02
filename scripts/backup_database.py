"""Create a compressed PostgreSQL backup using pg_dump."""

import argparse
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="backups")
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()
    load_dotenv()

    database_url = os.getenv("DATABASE_URL", "").strip()
    pg_dump = shutil.which("pg_dump")
    if not database_url:
        raise SystemExit("DATABASE_URL is required.")
    if not pg_dump:
        raise SystemExit("pg_dump is not installed or is not available on PATH.")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"lumaire-{timestamp}.dump"
    command_env = os.environ.copy()
    command_env.setdefault("PGSSLMODE", os.getenv("DATABASE_SSLMODE", "require"))
    subprocess.run(
        [pg_dump, "--format=custom", "--no-owner", "--file", str(output_path), database_url],
        check=True,
        env=command_env,
    )

    backups = sorted(output_dir.glob("lumaire-*.dump"), reverse=True)
    for old_backup in backups[max(1, args.keep):]:
        old_backup.unlink()
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
