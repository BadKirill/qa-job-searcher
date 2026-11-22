from pathlib import Path

from alembic import op

revision = "0002_legacy_import"
down_revision = "0001_ownership"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix(".sql").read_text())


def downgrade():
    op.get_bind().exec_driver_sql(
        Path(__file__).with_name("v0002_legacy_import_down.sql").read_text()
    )
