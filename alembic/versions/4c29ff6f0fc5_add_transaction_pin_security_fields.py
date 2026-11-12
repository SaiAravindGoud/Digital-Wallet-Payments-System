
from alembic import op
import sqlalchemy as sa

revision = "4c29ff6f0fc5"
down_revision = "1bf78036c076"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("pin_hash", sa.String(length=255), nullable=True)
    )

    op.add_column(
        "users",
        sa.Column(
            "pin_failed_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0"
        )
    )

    op.add_column(
        "users",
        sa.Column("pin_locked_until", sa.DateTime(), nullable=True)
    )


def downgrade():
    op.drop_column("users", "pin_locked_until")
    op.drop_column("users", "pin_failed_attempts")
    op.drop_column("users", "pin_hash")
