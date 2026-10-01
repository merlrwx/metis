import os
from io import BytesIO

import pytest
from backend.models import Base
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy.orm import sessionmaker

from backend import database


@pytest.fixture
def sample_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Medication incident policy) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


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
