import pytest

from app.core import config
from app.core.config import Settings, normalize_database_url
from app.db import session as db_session


class TestNormalizeDatabaseUrl:
    def test_sqlite_is_unchanged(self):
        assert normalize_database_url("sqlite:///data/app.db") == "sqlite:///data/app.db"

    def test_supabase_scheme_is_rewritten_for_psycopg3(self):
        url = normalize_database_url("postgresql://u:p@db.example.supabase.co:5432/postgres")
        assert url.startswith("postgresql+psycopg://u:p@db.example.supabase.co:5432/postgres")

    def test_postgres_alias_is_rewritten(self):
        assert normalize_database_url("postgres://u:p@h/db").startswith("postgresql+psycopg://")

    def test_remote_hosts_require_ssl(self):
        assert "sslmode=require" in normalize_database_url("postgresql://u:p@remote.example/db")

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
    def test_local_hosts_do_not_force_ssl(self, host):
        assert "sslmode" not in normalize_database_url(f"postgresql://u:p@{host}/db")

    def test_explicit_sslmode_is_kept(self):
        url = normalize_database_url("postgresql://u:p@remote.example/db?sslmode=disable")
        assert "sslmode=disable" in url
        assert "sslmode=require" not in url

    def test_password_is_not_masked(self):
        url = normalize_database_url("postgresql://u:s3cret@remote.example/db")
        assert "s3cret" in url
        assert "***" not in url

    def test_special_characters_in_password_survive(self):
        url = normalize_database_url("postgresql://u:p%40ss%2Fword@remote.example/db")
        assert "p%40ss%2Fword" in url


def test_settings_defaults_use_sqlite_and_plan_limits(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:///data/app.db"
    assert settings.max_upload_bytes == 50 * 1024 * 1024
    assert settings.archive_limits.max_entries == 200
    assert settings.archive_limits.max_uncompressed_bytes == 200 * 1024 * 1024
    assert settings.max_features == 50_000
    assert settings.extent_limits.warn_degrees == 6.0
    assert settings.extent_limits.max_degrees == 30.0


def test_settings_read_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@remote.example/db")
    monkeypatch.setenv("MAX_FEATURES", "10")
    settings = Settings(_env_file=None)
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.max_features == 10


def test_get_settings_is_cached():
    config.get_settings.cache_clear()
    assert config.get_settings() is config.get_settings()


def test_transaction_pooler_disables_prepared_statements(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        db_session, "create_engine", lambda url, **kwargs: captured.update(kwargs) or object()
    )
    db_session.build_engine("postgresql+psycopg://u:p@pooler.example:6543/postgres")
    assert captured["connect_args"] == {"prepare_threshold": None}


def test_session_pooler_keeps_default_connect_args(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        db_session, "create_engine", lambda url, **kwargs: captured.update(kwargs) or object()
    )
    db_session.build_engine("postgresql+psycopg://u:p@pooler.example:5432/postgres")
    assert captured["connect_args"] == {}
