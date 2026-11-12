
from alembic import op
import sqlalchemy as sa


revision = "58ceac9bbc98"
down_revision = "4c29ff6f0fc5"
branch_labels = None
depends_on = None


def upgrade():
    # Add the merchant code column.
    op.add_column(
        "users",
        sa.Column("merchant_code", sa.String(length=20), nullable=True),
    )

    # Assign unique codes to existing merchant accounts.
    op.execute(
        """
        UPDATE users
        SET merchant_code = CONCAT('MER-', LPAD(id, 5, '0'))
        WHERE role = 'Merchant'
          AND merchant_code IS NULL
        """
    )

    # Add a named unique constraint.
    op.create_unique_constraint(
        "uq_users_merchant_code",
        "users",
        ["merchant_code"],
    )


def downgrade():
    op.drop_constraint(
        "uq_users_merchant_code",
        "users",
        type_="unique",
    )

    op.drop_column("users", "merchant_code")
