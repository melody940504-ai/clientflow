"""Create the current Lumaire schema as an idempotent baseline."""

from alembic import op


revision = "20260803_0001"
down_revision = None
branch_labels = None
depends_on = None


TABLES = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'owner',
        client_reference_id INTEGER, is_verified BOOLEAN NOT NULL DEFAULT FALSE,
        verification_token TEXT, reset_token TEXT, reset_token_expires_at TEXT,
        studio_name TEXT DEFAULT 'Lumaire Studio', brand_color TEXT DEFAULT '#9b8cf6',
        logo_url TEXT, email_sender_name TEXT DEFAULT 'Lumaire', display_name TEXT,
        workspace_owner_id INTEGER, setup_completed BOOLEAN NOT NULL DEFAULT FALSE,
        session_version INTEGER NOT NULL DEFAULT 1, is_active BOOLEAN NOT NULL DEFAULT TRUE,
        must_change_password BOOLEAN NOT NULL DEFAULT FALSE, invitation_token TEXT,
        invitation_expires_at TEXT, created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clients (
        id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
        name TEXT NOT NULL, email TEXT, contact TEXT, notes TEXT,
        archived_at TEXT, created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
        client_id INTEGER NOT NULL REFERENCES clients(id), name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'Shorts', status TEXT NOT NULL DEFAULT 'Awaiting Review',
        notes TEXT, review_token TEXT, review_token_expires_at TEXT, review_due_at TEXT,
        guest_access TEXT NOT NULL DEFAULT 'comment', review_password_hash TEXT,
        review_link_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        review_allow_download BOOLEAN NOT NULL DEFAULT FALSE,
        review_allow_versions BOOLEAN NOT NULL DEFAULT TRUE,
        review_visit_count INTEGER NOT NULL DEFAULT 0, review_last_visited_at TEXT,
        delivery_checklist TEXT NOT NULL DEFAULT '', archived_at TEXT,
        reopened_at TEXT, reopened_by TEXT, created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS video_versions (
        id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL,
        project_id INTEGER NOT NULL REFERENCES projects(id), version_label TEXT NOT NULL,
        video_url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Awaiting Review',
        notes TEXT, created_by_name TEXT, created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comments (
        id SERIAL PRIMARY KEY, video_version_id INTEGER NOT NULL REFERENCES video_versions(id),
        author_role TEXT NOT NULL, author_name TEXT NOT NULL, body TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'comment', is_resolved BOOLEAN NOT NULL DEFAULT FALSE,
        resolved_at TEXT, parent_comment_id INTEGER, is_internal BOOLEAN NOT NULL DEFAULT FALSE,
        attachment_url TEXT, attachment_name TEXT, attachment_storage_path TEXT,
        annotation_data TEXT, author_email TEXT,
        identity_verified BOOLEAN NOT NULL DEFAULT FALSE, created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_notification_reads (
        user_id INTEGER NOT NULL REFERENCES users(id),
        project_id INTEGER NOT NULL REFERENCES projects(id), last_read_at TEXT NOT NULL,
        PRIMARY KEY(user_id, project_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_members (
        project_id INTEGER NOT NULL REFERENCES projects(id),
        user_id INTEGER NOT NULL REFERENCES users(id), assigned_at TEXT NOT NULL,
        PRIMARY KEY(project_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_attachments (
        id SERIAL PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
        title TEXT NOT NULL, original_name TEXT NOT NULL, storage_path TEXT,
        public_url TEXT, content_type TEXT, size_bytes INTEGER NOT NULL DEFAULT 0,
        created_by_user_id INTEGER REFERENCES users(id), created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_lifecycle_events (
        id SERIAL PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
        event_type TEXT NOT NULL, actor_user_id INTEGER REFERENCES users(id),
        actor_name TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
    )
    """,
)

COLUMNS = (
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_expires_at TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS studio_name TEXT DEFAULT 'Lumaire Studio'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS brand_color TEXT DEFAULT '#9b8cf6'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS logo_url TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_sender_name TEXT DEFAULT 'Lumaire'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS workspace_owner_id INTEGER",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS setup_completed BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS invitation_token TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS invitation_expires_at TEXT",
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS archived_at TEXT",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_token TEXT",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_token_expires_at TEXT",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_due_at TEXT",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS guest_access TEXT NOT NULL DEFAULT 'comment'",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS delivery_checklist TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_password_hash TEXT",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_link_enabled BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_allow_download BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_allow_versions BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_visit_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_last_visited_at TEXT",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived_at TEXT",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS reopened_at TEXT",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS reopened_by TEXT",
    "ALTER TABLE video_versions ADD COLUMN IF NOT EXISTS created_by_name TEXT",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS is_resolved BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS resolved_at TEXT",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS parent_comment_id INTEGER",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS is_internal BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS attachment_url TEXT",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS attachment_name TEXT",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS attachment_storage_path TEXT",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS annotation_data TEXT",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS author_email TEXT",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS identity_verified BOOLEAN NOT NULL DEFAULT FALSE",
)

INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS projects_review_token_idx ON projects(review_token)",
    "CREATE INDEX IF NOT EXISTS projects_user_created_idx ON projects(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS projects_client_created_idx ON projects(client_id, created_at)",
    "CREATE INDEX IF NOT EXISTS video_versions_project_created_idx ON video_versions(project_id, created_at)",
    "CREATE INDEX IF NOT EXISTS comments_version_created_idx ON comments(video_version_id, created_at)",
    "CREATE INDEX IF NOT EXISTS comments_open_feedback_idx ON comments(video_version_id, author_role, is_resolved, type)",
    "CREATE INDEX IF NOT EXISTS project_members_user_idx ON project_members(user_id)",
    "CREATE INDEX IF NOT EXISTS project_attachments_project_idx ON project_attachments(project_id, created_at)",
    "CREATE INDEX IF NOT EXISTS project_lifecycle_project_idx ON project_lifecycle_events(project_id, created_at)",
)


def upgrade() -> None:
    for statement in TABLES:
        op.execute(statement)
    for statement in COLUMNS:
        op.execute(statement)
    for statement in INDEXES:
        op.execute(statement)


def downgrade() -> None:
    # This baseline may be applied to an existing production database. Dropping
    # its tables automatically would be unsafe, so downgrade is intentionally empty.
    pass
