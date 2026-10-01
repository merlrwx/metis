import os

import pytest
from backend.models import Base
from sqlalchemy.orm import sessionmaker

from backend import database


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, request):
    test_database_url = os.environ.get("TEST_DATABASE_URL")
    if test_database_url:
        database.configure_database(test_database_url)
        if request.node.get_closest_marker("external_worker"):
            Base.metadata.create_all(database.engine)
            try:
                yield
            finally:
                with database.engine.begin() as connection:
                    for table in reversed(Base.metadata.sorted_tables):
                        connection.execute(table.delete())
                database.engine.dispose()
            return
        connection = database.engine.connect()
        transaction = connection.begin()
        Base.metadata.create_all(connection)
        database.SessionLocal = sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield
        finally:
            transaction.rollback()
            connection.close()
            database.engine.dispose()
    else:
        database.configure_database(f"sqlite:///{tmp_path / 'metis.sqlite3'}")
        Base.metadata.create_all(database.engine)
        try:
            yield
        finally:
            Base.metadata.drop_all(database.engine)
            database.engine.dispose()
