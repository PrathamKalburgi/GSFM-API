"""Settings read from environment variables (and an optional local `.env`)."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.services.archive import ArchiveLimits
from app.services.crs import ExtentLimits

LOCAL_HOSTS = {None, "", "localhost", "127.0.0.1", "::1"}


def normalize_database_url(url: str) -> str:
    """Make a database URL usable by SQLAlchemy with psycopg 3.

    Supabase hands out `postgresql://` URLs, which SQLAlchemy would resolve to psycopg2,
    so the scheme is rewritten. Remote PostgreSQL hosts get `sslmode=require` unless the
    URL already sets an `sslmode`.
    """
    parsed = make_url(url)
    if parsed.drivername in {"postgres", "postgresql"}:
        parsed = parsed.set(drivername="postgresql+psycopg")
    if (
        parsed.get_backend_name() == "postgresql"
        and parsed.host not in LOCAL_HOSTS
        and "sslmode" not in parsed.query
    ):
        parsed = parsed.update_query_dict({"sslmode": "require"})
    return parsed.render_as_string(hide_password=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/app.db"
    log_level: str = "INFO"

    max_upload_bytes: int = 50 * 1024 * 1024
    max_zip_entries: int = 200
    max_uncompressed_bytes: int = 200 * 1024 * 1024
    max_features: int = 50_000
    extent_warn_degrees: float = 6.0
    extent_max_degrees: float = 30.0

    @field_validator("database_url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @property
    def archive_limits(self) -> ArchiveLimits:
        return ArchiveLimits(self.max_zip_entries, self.max_uncompressed_bytes)

    @property
    def extent_limits(self) -> ExtentLimits:
        return ExtentLimits(self.extent_warn_degrees, self.extent_max_degrees)


@lru_cache
def get_settings() -> Settings:
    return Settings()
