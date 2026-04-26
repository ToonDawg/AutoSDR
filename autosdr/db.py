"""SQLAlchemy engine/session factory.

POC uses SQLite; the same session factory and models work against Postgres in
v1 by swapping ``DATABASE_URL``. We enable foreign keys on SQLite (off by
default) and set ``check_same_thread=False`` so the webhook's background task
can share a connection pool with the scheduler.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from autosdr.config import get_settings

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _ensure_sqlite_parent_dir(url: str) -> None:
    if not url.startswith("sqlite"):
        return
    # Extract the path component after `sqlite:///`
    path = url.split("sqlite:///")[-1]
    if not path or path == ":memory:":
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _ensure_sqlite_parent_dir(settings.database_url)
        connect_args: dict = {}
        if settings.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        _engine = create_engine(
            settings.database_url,
            connect_args=connect_args,
            future=True,
        )

        if settings.database_url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _enable_sqlite_fk(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                # Write-ahead logging reduces reader/writer contention between
                # the scheduler and the webhook background task.
                cur.execute("PRAGMA journal_mode=WAL")
                # The scheduler holds a session open for the full duration of
                # a pipeline run (several LLM calls, often 30-60s end to end),
                # while the LLM client opens short-lived sessions to persist
                # each llm_call row. SQLite-WAL still serialises writers, so
                # the inner session has to wait for the outer one to commit.
                # A 2-minute busy_timeout covers realistic pipeline durations
                # without failing the secondary writers.
                cur.execute("PRAGMA busy_timeout=120000")
                cur.close()

    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Short-lived session with commit/rollback bookends."""

    Session = get_sessionmaker()
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Known additive column migrations for legacy DBs.
#
# SQLAlchemy's ``create_all`` is idempotent per-table: it will NOT add new
# columns to tables that already exist. For a POC that ships without
# Alembic, we hand-roll a tiny "add missing nullable column" step for any
# column we've introduced since the initial schema shipped. Each entry is
# ``(table, column_name, sql_type)`` — the type has to be the raw SQL
# declaration because SQLAlchemy's typed ADD COLUMN helper wants a
# migration context we deliberately don't set up here.
_ADDITIVE_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("campaign", "followup", "JSON"),
    ("campaign", "quota_reset_at", "DATETIME"),
    ("thread", "hitl_dismissed_at", "DATETIME"),
    ("lead", "do_not_contact_at", "DATETIME"),
    ("lead", "do_not_contact_reason", "TEXT"),
)


def _apply_additive_column_migrations(engine: Engine) -> None:
    """Apply any missing columns in :data:`_ADDITIVE_COLUMN_MIGRATIONS`.

    Only adds nullable columns with no default — any DB value we care about
    goes through the ORM default/server-side logic in ``models.py`` and the
    API layer. This keeps the migration safe to re-run and cheap on
    established installs (inspector returns cached columns).
    """

    if not _ADDITIVE_COLUMN_MIGRATIONS:
        return
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, column, sql_type in _ADDITIVE_COLUMN_MIGRATIONS:
            if table not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table)}
            if column in present:
                continue
            logger.info(
                "db: adding missing column %s.%s (%s) to legacy DB",
                table,
                column,
                sql_type,
            )
            conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
            )


def create_all() -> None:
    """Create all tables + apply additive schema migrations.

    Safe to call repeatedly: ``create_all`` is idempotent per-table, and the
    additive-column step is guarded by an inspector check.
    """

    from autosdr.models import Base  # local import avoids circular imports

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _apply_additive_column_migrations(engine)


def reset_for_tests() -> None:
    """Dispose the engine/session cache. Used by test fixtures."""

    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
