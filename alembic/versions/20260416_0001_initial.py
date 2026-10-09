"""initial schema"""

from alembic import op
import sqlalchemy as sa


revision = "20260416_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "taskrecord",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("query_text", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("current_step", sa.String(), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("answer_json", sa.JSON(), nullable=True),
        sa.Column("latest_screenshot", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "browseractionrecord",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("screenshot_path", sa.String(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_browseractionrecord_task_id"), "browseractionrecord", ["task_id"], unique=False)
    op.create_table(
        "sourcerecord",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("domain", sa.String(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("published_at", sa.String(), nullable=True),
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("authority_score", sa.Float(), nullable=False),
        sa.Column("freshness_score", sa.Float(), nullable=False),
        sa.Column("completeness_score", sa.Float(), nullable=False),
        sa.Column("rank_score", sa.Float(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sourcerecord_task_id"), "sourcerecord", ["task_id"], unique=False)
    op.create_table(
        "evidencerecord",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column("evidence_type", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_evidencerecord_task_id"), "evidencerecord", ["task_id"], unique=False)
    op.create_table(
        "erroreventrecord",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("recoverable", sa.Boolean(), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_erroreventrecord_task_id"), "erroreventrecord", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_erroreventrecord_task_id"), table_name="erroreventrecord")
    op.drop_table("erroreventrecord")
    op.drop_index(op.f("ix_evidencerecord_task_id"), table_name="evidencerecord")
    op.drop_table("evidencerecord")
    op.drop_index(op.f("ix_sourcerecord_task_id"), table_name="sourcerecord")
    op.drop_table("sourcerecord")
    op.drop_index(op.f("ix_browseractionrecord_task_id"), table_name="browseractionrecord")
    op.drop_table("browseractionrecord")
    op.drop_table("taskrecord")
