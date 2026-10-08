import uuid

from sqlalchemy import BigInteger, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonType

# SQLite only autoincrements a column declared exactly as INTEGER.
BigIntPk = BigInteger().with_variant(Integer, "sqlite")


class FeatureResult(Base):
    __tablename__ = "feature_results"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    file_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("uploaded_files.id", ondelete="CASCADE")
    )
    feature_index: Mapped[int] = mapped_column(Integer)
    source_layer: Mapped[str] = mapped_column(Text)
    source_feature_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    geometry_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    geometry_json: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    properties_json: Mapped[dict] = mapped_column(JsonType)
    area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    length_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    measurement_status: Mapped[str] = mapped_column(String(32))
    measurement_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Its index also serves ordered pagination, per-file aggregates and the cascade.
    __table_args__ = (UniqueConstraint("file_id", "feature_index", name="uq_feature_file_index"),)
