from alembic import context

import app.models  # noqa: F401  (registers the tables on Base.metadata)
from app.core.config import get_settings, normalize_database_url
from app.db.base import Base
from app.db.session import build_engine

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    # Tests and tooling can override the URL through the alembic config object.
    return normalize_database_url(
        config.get_main_option("sqlalchemy.url") or get_settings().database_url
    )


def run_migrations_offline() -> None:
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = build_engine(_database_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
