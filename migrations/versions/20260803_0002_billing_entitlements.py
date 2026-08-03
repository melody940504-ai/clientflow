"""Add workspace subscription and Stripe identifiers."""

from alembic import op


revision = "20260803_0002"
down_revision = "20260803_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_plan TEXT NOT NULL DEFAULT 'free'")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status TEXT NOT NULL DEFAULT 'free'")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_price_id TEXT")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_current_period_end TEXT")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS billing_updated_at TEXT")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_stripe_customer_idx ON users(stripe_customer_id) WHERE stripe_customer_id IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_stripe_subscription_idx ON users(stripe_subscription_id) WHERE stripe_subscription_id IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS users_stripe_subscription_idx")
    op.execute("DROP INDEX IF EXISTS users_stripe_customer_idx")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS billing_updated_at")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS subscription_current_period_end")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS stripe_price_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS stripe_subscription_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS stripe_customer_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS subscription_status")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS subscription_plan")
