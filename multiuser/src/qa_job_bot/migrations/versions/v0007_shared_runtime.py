from pathlib import Path

from alembic import op

revision = "0007_shared_runtime"
down_revision = "0006_document_context"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix(".sql").read_text())


def downgrade():
    op.get_bind().exec_driver_sql(
        Path(__file__).with_name("v0007_shared_runtime_down.sql").read_text()
    )
