from pathlib import Path

from alembic import op

revision = "0006_document_context"
down_revision = "0005_personal_runs"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix(".sql").read_text())


def downgrade():
    op.get_bind().exec_driver_sql(
        Path(__file__).with_name("v0006_document_context_down.sql").read_text()
    )
