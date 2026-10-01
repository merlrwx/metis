import os
from collections.abc import Generator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str | None = None) -> Engine:
    return create_engine(
        database_url
        or os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://metis:metis-local-only@localhost:5432/metis",
        ),
        pool_pre_ping=True,
    )


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
