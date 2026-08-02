# Lumaire operations

These commands do not require a paid plan. Run them from the repository root.

## Before a production deployment

1. Create database and attachment backups:

   ```powershell
   python scripts/backup_database.py
   python scripts/backup_attachments.py
   ```

2. Inspect the attachment privacy migration:

   ```powershell
   python scripts/configure_private_attachments.py
   ```

3. If the dry run reports zero unresolved URLs, apply it:

   ```powershell
   python scripts/configure_private_attachments.py --apply
   ```

4. Keep `ATTACHMENTS_BUCKET_PRIVATE=true` and `RUN_DB_MIGRATIONS=true` in Render.
   Alembic upgrades run automatically before the compatibility seed at startup.

5. After deployment, verify the live configuration from a trusted machine:

   ```powershell
   python scripts/preflight.py
   ```

Backups are written to `backups/`, which is excluded from Git. Store copies away
from the development computer before treating them as a production backup.

## Shared rate limiting

Set `REDIS_URL` to any Redis-compatible or Valkey connection string. With no URL,
the application keeps the existing in-memory limiter. If the shared service is
temporarily unavailable, requests fall back to memory for 60 seconds.

## Local staging

Create the local environment file and replace its session secret:

```powershell
Copy-Item .env.staging.example .env.staging
docker compose -f compose.staging.yml up --build
```

The staging application opens at `http://localhost:8000` and uses isolated local
PostgreSQL and Valkey containers. Add separate Supabase credentials only when the
staging environment needs upload testing. Never put production credentials in
`.env.staging`.
