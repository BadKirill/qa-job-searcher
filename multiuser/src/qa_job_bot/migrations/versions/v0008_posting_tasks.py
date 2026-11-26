from alembic import op

revision = "0008_posting_tasks"
down_revision = "0007_shared_runtime"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("task_kind", "tasks", type_="check")
    op.create_check_constraint(
        "task_kind", "tasks", "kind IN ('search','chat','cv','cover_letter','posting_check')"
    )


def downgrade():
    op.drop_constraint("task_kind", "tasks", type_="check")
    op.create_check_constraint(
        "task_kind", "tasks", "kind IN ('search','chat','cv','cover_letter')"
    )
