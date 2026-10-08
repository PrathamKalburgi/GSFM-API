"""Create uploaded_files and feature_results.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
BIG_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "uploaded_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("format", sa.String(32), nullable=False),
        sa.Column("source_crs", sa.Text(), nullable=False),
        sa.Column("measurement_crs", sa.Text(), nullable=True),
        sa.Column("feature_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("warnings", JSON_TYPE, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_uploaded_files_created_at", "uploaded_files", [sa.text("created_at DESC")])
    op.create_table(
        "feature_results",
        sa.Column("id", BIG_PK, primary_key=True, autoincrement=True),
        sa.Column(
            "file_id",
            sa.Uuid(),
            sa.ForeignKey("uploaded_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feature_index", sa.Integer(), nullable=False),
        sa.Column("source_layer", sa.Text(), nullable=False),
        sa.Column("source_feature_id", sa.Text(), nullable=True),
        sa.Column("geometry_type", sa.String(64), nullable=True),
        sa.Column("geometry_json", JSON_TYPE, nullable=True),
        sa.Column("properties_json", JSON_TYPE, nullable=False),
        sa.Column("area_m2", sa.Float(), nullable=True),
        sa.Column("length_m", sa.Float(), nullable=True),
        sa.Column("measurement_status", sa.String(32), nullable=False),
        sa.Column("measurement_message", sa.Text(), nullable=True),
        sa.UniqueConstraint("file_id", "feature_index", name="uq_feature_file_index"),
    )

    if op.get_bind().dialect.name == "postgresql":
        # Tables in Supabase's `public` schema are reachable through its auto-generated
        # Data API with the public anon key unless Row Level Security is on. No policies
        # means no access through that API. The API connects as the table owner, which
        # bypasses RLS, so it is unaffected.
        op.execute("ALTER TABLE uploaded_files ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE feature_results ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("feature_results")
    op.drop_index("ix_uploaded_files_created_at", table_name="uploaded_files")
    op.drop_table("uploaded_files")
