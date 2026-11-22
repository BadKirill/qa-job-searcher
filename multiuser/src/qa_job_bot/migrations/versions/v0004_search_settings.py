from pathlib import Path

from alembic import op

revision = "0004_search_settings"
down_revision = "0003_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix(".sql").read_text())


def downgrade():
    op.get_bind().exec_driver_sql(
        Path(__file__).with_name("v0004_search_settings_down.sql").read_text()
    )
