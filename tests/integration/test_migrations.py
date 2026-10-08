import pytest
from sqlalchemy import inspect, text


def test_upgrade_creates_both_tables_and_indexes(engine):
    inspector = inspect(engine)
    assert {"uploaded_files", "feature_results"} <= set(inspector.get_table_names())
    index_names = {i["name"] for i in inspector.get_indexes("uploaded_files")}
    assert "ix_uploaded_files_created_at" in index_names
    unique = {tuple(u["column_names"]) for u in inspector.get_unique_constraints("feature_results")}
    assert ("file_id", "feature_index") in unique
    foreign_key = inspector.get_foreign_keys("feature_results")[0]
    assert foreign_key["referred_table"] == "uploaded_files"
    assert foreign_key["options"].get("ondelete", "").upper() == "CASCADE"


def test_downgrade_then_upgrade_round_trip(engine, database_url, migrate):
    migrate(database_url, "downgrade", "base")
    assert "uploaded_files" not in inspect(engine).get_table_names()
    migrate(database_url, "upgrade", "head")
    assert "uploaded_files" in inspect(engine).get_table_names()


def test_sqlite_connections_enforce_foreign_keys(engine):
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite only")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_row_level_security_is_enabled_on_postgres(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL only")
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT relname, relrowsecurity FROM pg_class "
                "WHERE relname IN ('uploaded_files', 'feature_results')"
            )
        ).all()
        policies = connection.execute(
            text(
                "SELECT count(*) FROM pg_policies "
                "WHERE tablename IN ('uploaded_files', 'feature_results')"
            )
        ).scalar()
    assert {name: enabled for name, enabled in rows} == {
        "uploaded_files": True,
        "feature_results": True,
    }
    assert policies == 0  # RLS on with no policies: the Data API sees nothing


def test_json_columns_are_jsonb_on_postgres(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL only")
    with engine.connect() as connection:
        types = dict(
            connection.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = 'feature_results' "
                    "AND column_name IN ('geometry_json', 'properties_json')"
                )
            ).all()
        )
    assert types == {"geometry_json": "jsonb", "properties_json": "jsonb"}
