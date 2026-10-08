import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import FeatureResult, UploadedFile
from app.repositories import file_repository as repo

POLYGON = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}


def make_file(**overrides) -> UploadedFile:
    values = {
        "original_filename": "survey.kml",
        "format": "KML",
        "source_crs": "EPSG:4326",
        "measurement_crs": "EPSG:32643",
        "feature_count": 0,
        "warnings": [],
    }
    return UploadedFile(**{**values, **overrides})


def feature_row(index: int, **overrides) -> dict:
    values = {
        "feature_index": index,
        "source_layer": "Parcels",
        "source_feature_id": f"p{index}",
        "geometry_type": "Polygon",
        "geometry_json": POLYGON,
        "properties_json": {"Name": f"parcel {index}"},
        "area_m2": 100.0,
        "length_m": None,
        "measurement_status": "MEASURED",
        "measurement_message": None,
    }
    return {**values, **overrides}


def counts(engine) -> tuple[int, int]:
    with Session(engine) as fresh:
        files = fresh.scalar(select(func.count()).select_from(UploadedFile))
        features = fresh.scalar(select(func.count()).select_from(FeatureResult))
    return files, features


def test_create_and_read_back(session, engine):
    file = make_file(feature_count=3, warnings=["a warning"])
    repo.create_file_with_features(session, file, [feature_row(i) for i in range(3)])

    with Session(engine) as fresh:
        stored = repo.get_file(fresh, file.id)
        assert stored.original_filename == "survey.kml"
        assert stored.warnings == ["a warning"]
        assert stored.status == "COMPLETED"
        assert stored.created_at.tzinfo is not None  # timezone-aware UTC on every database
        features, total = repo.list_features(fresh, file.id, limit=10, offset=0)
    assert total == 3
    assert [f.feature_index for f in features] == [0, 1, 2]
    assert features[0].geometry_json == POLYGON
    assert features[0].properties_json == {"Name": "parcel 0"}


def test_get_unknown_file_returns_none(session):
    assert repo.get_file(session, uuid.uuid4()) is None


def test_missing_attribute_values_are_stored_as_null(session, engine):
    file = make_file(feature_count=1)
    row = feature_row(0, properties_json={"owner": None, "zone": 3}, geometry_json=None)
    repo.create_file_with_features(session, file, [row])
    with Session(engine) as fresh:
        (feature,), _ = repo.list_features(fresh, file.id, 10, 0)
    assert feature.properties_json == {"owner": None, "zone": 3}
    assert feature.geometry_json is None


def test_inserts_are_batched(session, engine, monkeypatch):
    monkeypatch.setattr(repo, "INSERT_BATCH_SIZE", 3)
    file = make_file(feature_count=10)
    repo.create_file_with_features(session, file, [feature_row(i) for i in range(10)])
    assert counts(engine) == (1, 10)


def test_failure_mid_insert_leaves_no_rows(session, engine, monkeypatch):
    monkeypatch.setattr(repo, "INSERT_BATCH_SIZE", 2)
    real_execute = session.execute
    feature_inserts = 0

    def failing_execute(statement, *args, **kwargs):
        nonlocal feature_inserts
        if getattr(statement, "is_insert", False) and statement.table.name == "feature_results":
            feature_inserts += 1
            if feature_inserts == 2:  # the first batch is already in the transaction
                raise RuntimeError("simulated failure")
        return real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", failing_execute)
    file = make_file(feature_count=5)
    with pytest.raises(RuntimeError, match="simulated failure"):
        repo.create_file_with_features(session, file, [feature_row(i) for i in range(5)])

    assert feature_inserts == 2
    assert counts(engine) == (0, 0)  # no file row and no feature rows


def test_constraint_violation_in_a_later_batch_leaves_no_rows(session, engine, monkeypatch):
    monkeypatch.setattr(repo, "INSERT_BATCH_SIZE", 2)
    rows = [feature_row(0), feature_row(1), feature_row(2), feature_row(2)]  # duplicate index
    with pytest.raises(IntegrityError):
        repo.create_file_with_features(session, make_file(feature_count=4), rows)
    assert counts(engine) == (0, 0)


def test_session_is_usable_after_a_failed_insert(session, engine):
    rows = [feature_row(0), feature_row(0)]
    with pytest.raises(IntegrityError):
        repo.create_file_with_features(session, make_file(), rows)
    repo.create_file_with_features(session, make_file(), [feature_row(0)])
    assert counts(engine) == (1, 1)


def test_list_files_is_newest_first_with_pagination(session):
    now = datetime.now(UTC)
    ids = []
    for age in (3, 1, 2):  # inserted out of order on purpose
        file = make_file(original_filename=f"age{age}.kml", created_at=now - timedelta(hours=age))
        repo.create_file_with_features(session, file, [])
        ids.append(file.original_filename)

    items, total = repo.list_files(session, limit=2, offset=0)
    assert total == 3
    assert [f.original_filename for f in items] == ["age1.kml", "age2.kml"]
    items, total = repo.list_files(session, limit=2, offset=2)
    assert total == 3
    assert [f.original_filename for f in items] == ["age3.kml"]
    assert repo.list_files(session, limit=10, offset=50) == ([], 3)


def test_list_features_paginates_in_feature_index_order(session):
    file = make_file(feature_count=5)
    rows = [feature_row(i) for i in (3, 0, 4, 1, 2)]  # inserted out of order
    repo.create_file_with_features(session, file, rows)

    page, total = repo.list_features(session, file.id, limit=2, offset=1)
    assert total == 5
    assert [f.feature_index for f in page] == [1, 2]
    assert repo.list_features(session, file.id, limit=2, offset=10) == ([], 5)


def test_list_features_only_returns_the_requested_file(session):
    first, second = make_file(), make_file()
    repo.create_file_with_features(session, first, [feature_row(0), feature_row(1)])
    repo.create_file_with_features(session, second, [feature_row(0)])
    assert repo.list_features(session, first.id, 10, 0)[1] == 2
    assert repo.list_features(session, second.id, 10, 0)[1] == 1


def test_include_geometry_false_does_not_load_geometry(session, engine):
    file = make_file(feature_count=1)
    repo.create_file_with_features(session, file, [feature_row(0)])
    with Session(engine) as fresh:
        (light,), _ = repo.list_features(fresh, file.id, 10, 0, include_geometry=False)
        assert "geometry_json" in inspect(light).unloaded
        assert light.properties_json == {"Name": "parcel 0"}
        (full,), _ = repo.list_features(fresh, file.id, 10, 0)
        assert full.geometry_json == POLYGON


def test_summary_covers_the_whole_file(session):
    file = make_file(feature_count=6)
    rows = [
        feature_row(0, area_m2=100.5),
        feature_row(1, area_m2=200.25),
        feature_row(2, geometry_type="LineString", area_m2=None, length_m=50.0),
        feature_row(3, geometry_type="Point", area_m2=None, measurement_status="NOT_REQUIRED"),
        feature_row(4, geometry_type=None, area_m2=None, measurement_status="INVALID_GEOMETRY"),
        feature_row(5, geometry_type="GeometryCollection", area_m2=None,
                    measurement_status="UNSUPPORTED"),
    ]  # fmt: skip
    repo.create_file_with_features(session, file, rows)

    summary = repo.get_summary(session, file.id)
    assert summary.features == 6
    assert summary.by_status == {
        "MEASURED": 3,
        "NOT_REQUIRED": 1,
        "UNSUPPORTED": 1,
        "INVALID_GEOMETRY": 1,
    }
    assert summary.total_area_m2 == pytest.approx(300.75)
    assert summary.total_length_m == pytest.approx(50.0)


def test_summary_of_a_file_without_measurements_has_zero_totals(session):
    file = make_file(feature_count=1)
    row = feature_row(0, area_m2=None, geometry_type="Point", measurement_status="NOT_REQUIRED")
    repo.create_file_with_features(session, file, [row])
    summary = repo.get_summary(session, file.id)
    assert summary.total_area_m2 == 0.0
    assert summary.total_length_m == 0.0
    assert summary.by_status["NOT_REQUIRED"] == 1


def test_summary_serializes_rounded_totals(session):
    file = make_file(feature_count=1)
    repo.create_file_with_features(session, file, [feature_row(0, area_m2=1922.7249)])
    assert repo.get_summary(session, file.id).model_dump()["total_area_m2"] == 1922.72


def test_delete_cascades_to_features(session, engine):
    keep, drop = make_file(), make_file()
    repo.create_file_with_features(session, keep, [feature_row(0)])
    repo.create_file_with_features(session, drop, [feature_row(0), feature_row(1), feature_row(2)])
    assert counts(engine) == (2, 4)

    assert repo.delete_file(session, drop.id) is True
    assert counts(engine) == (1, 1)  # no orphaned feature rows
    with Session(engine) as fresh:
        assert repo.list_features(fresh, keep.id, 10, 0)[1] == 1


def test_delete_unknown_file_returns_false(session):
    assert repo.delete_file(session, uuid.uuid4()) is False
