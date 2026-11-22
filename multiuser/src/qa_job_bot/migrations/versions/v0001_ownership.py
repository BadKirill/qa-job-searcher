from pathlib import Path

from alembic import op

revision = "0001_ownership"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    sql = Path(__file__).with_suffix(".sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade():
    sql = Path(__file__).with_name("v0001_ownership_down.sql").read_text()
    op.get_bind().exec_driver_sql(sql)
