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
        engine_kwargs: dict = {"future": True}
        if settings.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        else:
            # Non-SQLite (Postgres in v1) shares one engine across the
            # API request handlers, the scheduler tick, and the scan
            # fan-out worker. The fan-out alone runs up to
            # ``Settings.scan_concurrency`` tasks in flight, each
            # holding a connection only for the persist step
            # (milliseconds), but brief overlap with the API and the
            # scheduler can still spike well past the SQLAlchemy
            # default of 5 + 10 overflow. 20 + 40 keeps headroom and
            # ``pool_pre_ping`` recycles dead Postgres connections
            # silently after a network blip.
            engine_kwargs["pool_size"] = 20
            engine_kwargs["max_overflow"] = 40
            engine_kwargs["pool_pre_ping"] = True
            engine_kwargs["pool_recycle"] = 1800
        _engine = create_engine(
            settings.database_url,
            connect_args=connect_args,
            **engine_kwargs,
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
    ("campaign", "outreach_window", "JSON"),
    ("thread", "hitl_dismissed_at", "DATETIME"),
    ("thread", "angle_type", "VARCHAR(32)"),
    ("lead", "do_not_contact_at", "DATETIME"),
    ("lead", "do_not_contact_reason", "TEXT"),
    ("lead", "enrichment_status", "VARCHAR(32)"),
    ("lead", "enrichment_fetched_at", "DATETIME"),
    ("message", "provider_message_id", "VARCHAR(128)"),
)


# Indexes we've introduced after the initial schema shipped. ``create_all``
# only creates indexes on tables it creates fresh, so legacy DBs need this
# step to pick them up. ``CREATE INDEX IF NOT EXISTS`` is supported by both
# SQLite and Postgres (>=9.5), keeping the helper dialect-agnostic.
_ADDITIVE_INDEX_MIGRATIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("message", "idx_message_provider_id", ("thread_id", "provider_message_id")),
    (
        "lead",
        "idx_lead_enrichment_status",
        ("workspace_id", "enrichment_status"),
    ),
    ("campaign_lead", "idx_campaign_lead_lead_id", ("lead_id",)),
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


def _apply_additive_index_migrations(engine: Engine) -> None:
    """Create any indexes in :data:`_ADDITIVE_INDEX_MIGRATIONS` that aren't there yet.

    Mirrors :func:`_apply_additive_column_migrations`: ``create_all`` only
    creates indexes on the tables it creates fresh, so we hand-roll the
    "create-if-missing" step for indexes added after the initial schema.
    """

    if not _ADDITIVE_INDEX_MIGRATIONS:
        return
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, index_name, columns in _ADDITIVE_INDEX_MIGRATIONS:
            if table not in existing_tables:
                continue
            present = {idx["name"] for idx in inspector.get_indexes(table)}
            if index_name in present:
                continue
            cols_sql = ", ".join(columns)
            logger.info(
                "db: adding missing index %s on %s(%s) to legacy DB",
                index_name,
                table,
                cols_sql,
            )
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table} ({cols_sql})"
                )
            )


# Bounded sweep — backfill at most this many rows per boot to keep the
# migration cheap on huge legacy DBs. Subsequent boots pick up where
# this left off because the column stays NULL until populated.
_ENRICHMENT_BACKFILL_BUDGET = 5000


def _backfill_lead_enrichment_columns(engine: Engine) -> None:
    """Populate the new ``lead.enrichment_status`` /
    ``enrichment_fetched_at`` columns from any existing JSON envelopes.

    Idempotent — only walks rows where the column is still ``NULL``
    but the JSON blob already has a status (i.e. legacy data shipped
    before the column existed). New writes go straight to the column
    via :func:`autosdr.enrichment.persist_enrichment`, so this is a
    one-time sweep on upgrade.

    Implemented in Python rather than SQL because freshness lives in
    a JSON path we don't want to lock in to a specific dialect.
    """

    from datetime import datetime, timezone

    from sqlalchemy import select as sa_select

    from autosdr.models import Lead

    inspector = inspect(engine)
    if "lead" not in set(inspector.get_table_names()):
        return
    columns = {col["name"] for col in inspector.get_columns("lead")}
    if not {"enrichment_status", "enrichment_fetched_at"}.issubset(columns):
        return

    Session = get_sessionmaker()
    with Session() as session:
        rows = session.execute(
            sa_select(Lead)
            .where(Lead.enrichment_status.is_(None))
            .limit(_ENRICHMENT_BACKFILL_BUDGET)
        ).scalars()

        touched = 0
        for lead in rows:
            envelope = (lead.raw_data or {}).get("enrichment")
            if not isinstance(envelope, dict):
                continue
            meta = envelope.get("_meta") if isinstance(envelope, dict) else None
            if not isinstance(meta, dict):
                continue
            status = meta.get("status")
            if not isinstance(status, str) or not status:
                continue
            lead.enrichment_status = status
            ts_raw = meta.get("fetched_at")
            if isinstance(ts_raw, str) and ts_raw:
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except ValueError:
                    pass
                else:
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    lead.enrichment_fetched_at = ts
            touched += 1

        if touched:
            session.commit()
            logger.info(
                "db: backfilled enrichment columns for %d legacy lead row(s)",
                touched,
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
    _apply_additive_index_migrations(engine)
    _backfill_lead_enrichment_columns(engine)


def reset_for_tests() -> None:
    """Dispose the engine/session cache. Used by test fixtures."""

    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
