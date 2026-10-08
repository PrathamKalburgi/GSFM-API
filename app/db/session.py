from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import get_settings

TRANSACTION_POOLER_PORT = 6543  # Supabase transaction pooler


def build_engine(url: str) -> Engine:
    parsed = make_url(url)
    backend = parsed.get_backend_name()
    options: dict = {}
    connect_args: dict = {}

    if backend == "sqlite":
        if parsed.database and parsed.database != ":memory:":
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        # FastAPI runs sync handlers in a thread pool; the pool hands each connection to
        # one thread at a time.
        connect_args["check_same_thread"] = False
    elif backend == "postgresql":
        options["pool_pre_ping"] = True
        if parsed.port == TRANSACTION_POOLER_PORT:
            # The transaction pooler cannot keep server-side prepared statements.
            connect_args["prepare_threshold"] = None

    engine = create_engine(url, connect_args=connect_args, **options)
    if backend == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _enable_sqlite_foreign_keys(dbapi_connection, _record) -> None:
    # Without this, ON DELETE CASCADE silently does nothing on SQLite.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@lru_cache
def get_engine() -> Engine:
    return build_engine(get_settings().database_url)


def get_session() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session
