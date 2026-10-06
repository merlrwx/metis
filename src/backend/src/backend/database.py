import os
from collections.abc import Generator
from time import perf_counter

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from backend import observability


def make_engine(database_url: str | None = None) -> Engine:
    configured = create_engine(
        database_url
        or os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://metis:metis-local-only@localhost:5432/metis",
        ),
        pool_pre_ping=True,
    )

    @event.listens_for(configured, "before_cursor_execute")
    def before_query(conn, cursor, statement, parameters, context, executemany):
        context.metis_query_start = perf_counter()

    @event.listens_for(configured, "after_cursor_execute")
    def after_query(conn, cursor, statement, parameters, context, executemany):
        duration = perf_counter() - context.metis_query_start
        observability.DB_DURATION.observe(duration)
        if duration > 1:
            observability.DB_SLOW.inc()
            observability.log_event("slow_database_query", duration_seconds=duration)

    return configured


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def configure_database(database_url: str) -> None:
    global engine, SessionLocal
    engine.dispose()
    engine = make_engine(database_url)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
