import uuid
from datetime import UTC, datetime

from sqlalchemy import Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonType, UTCDateTime


def _now() -> datetime:
    return datetime.now(UTC)


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    original_filename: Mapped[str] = mapped_column(String(255))
    format: Mapped[str] = mapped_column(String(32))
    source_crs: Mapped[str] = mapped_column(Text)
    measurement_crs: Mapped[str | None] = mapped_column(Text, nullable=True)
    feature_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="COMPLETED")
    warnings: Mapped[list[str]] = mapped_column(JsonType, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    __table_args__ = (Index("ix_uploaded_files_created_at", created_at.desc()),)
