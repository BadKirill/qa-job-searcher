from pathlib import Path

from alembic import op

revision = "0003_accounts"
down_revision = "0002_legacy_import"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix(".sql").read_text())


def downgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_name("v0003_accounts_down.sql").read_text())
