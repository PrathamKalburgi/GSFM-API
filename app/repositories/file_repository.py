"""All database queries and the insert transaction. Routes never touch SQL."""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session, defer

from app.models import FeatureResult, UploadedFile
from app.schemas.measurement import MeasurementSummary
from app.services.measurement import MeasurementStatus

INSERT_BATCH_SIZE = 1000


def create_file_with_features(
    session: Session, file: UploadedFile, features: Sequence[Mapping[str, Any]]
) -> None:
    """Insert the file and all its features in one transaction; roll back on any failure.

    Each mapping in `features` holds the `FeatureResult` columns except `file_id`.
    """
    try:
        session.add(file)
        session.flush()
        for start in range(0, len(features), INSERT_BATCH_SIZE):
            batch = [
                {**row, "file_id": file.id} for row in features[start : start + INSERT_BATCH_SIZE]
            ]
            session.execute(insert(FeatureResult), batch)
        session.commit()
    except Exception:
        session.rollback()
        raise


def get_file(session: Session, file_id: uuid.UUID) -> UploadedFile | None:
    return session.get(UploadedFile, file_id)


def list_files(session: Session, limit: int, offset: int) -> tuple[list[UploadedFile], int]:
    total = session.scalar(select(func.count()).select_from(UploadedFile)) or 0
    query = (
        select(UploadedFile)
        .order_by(UploadedFile.created_at.desc(), UploadedFile.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(query)), total


def list_features(
    session: Session,
    file_id: uuid.UUID,
    limit: int,
    offset: int,
    *,
    include_geometry: bool = True,
) -> tuple[list[FeatureResult], int]:
    total = (
        session.scalar(
            select(func.count()).select_from(FeatureResult).where(FeatureResult.file_id == file_id)
        )
        or 0
    )
    query = (
        select(FeatureResult)
        .where(FeatureResult.file_id == file_id)
        .order_by(FeatureResult.feature_index)
        .limit(limit)
        .offset(offset)
    )
    if not include_geometry:
        query = query.options(defer(FeatureResult.geometry_json))  # skip the large column
    return list(session.scalars(query)), total


def get_summary(session: Session, file_id: uuid.UUID) -> MeasurementSummary:
    """SQL aggregates over every feature of the file."""
    rows = session.execute(
        select(
            FeatureResult.measurement_status,
            func.count(),
            func.coalesce(func.sum(FeatureResult.area_m2), 0.0),
            func.coalesce(func.sum(FeatureResult.length_m), 0.0),
        )
        .where(FeatureResult.file_id == file_id)
        .group_by(FeatureResult.measurement_status)
    ).all()
    by_status = {status.value: 0 for status in MeasurementStatus}
    for status, count, _, _ in rows:
        by_status[status] = count
    return MeasurementSummary(
        features=sum(by_status.values()),
        by_status=by_status,
        total_area_m2=sum(row[2] for row in rows),
        total_length_m=sum(row[3] for row in rows),
    )


def delete_file(session: Session, file_id: uuid.UUID) -> bool:
    """Delete the file; its features go with it through the foreign-key cascade."""
    try:
        result = session.execute(delete(UploadedFile).where(UploadedFile.id == file_id))
        session.commit()
    except Exception:
        session.rollback()
        raise
    return result.rowcount > 0
