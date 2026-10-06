import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from backend.main import request_scope
from backend.models import Chunk, Document, DocumentVersion, Organisation, Source
from backend.schemas import SearchRequest
from backend.services import conversations, groups, knowledge

from backend import database, embeddings

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="requires PostgreSQL vector operations",
)


def test_multiple_sources_groups_versions_and_neighbours():
    provider = embeddings.HashingEmbeddingProvider()
    vector = provider.embed_query("medication incident supervisor")
    with database.SessionLocal() as session:
        own, foreign = Organisation(name="Own"), Organisation(name="Other")
        session.add_all([own, foreign])
        session.flush()
        sources = [
            Source(organisation_id=own.id, name="Upload", type="upload"),
            Source(organisation_id=own.id, name="Library fixture", type="microsoft365"),
            Source(organisation_id=own.id, name="Excluded", type="upload"),
            Source(organisation_id=foreign.id, name="Foreign", type="upload"),
        ]
        session.add_all(sources)
        session.flush()
        documents = []
        for index, source in enumerate(sources):
            document = Document(
                organisation_id=source.organisation_id,
                source_id=source.id,
                title=f"Document {index}",
            )
            session.add(document)
            session.flush()
            version = DocumentVersion(
                organisation_id=source.organisation_id,
                document_id=document.id,
                filename="a.txt",
                checksum=str(index),
                object_key=f"synthetic/{index}",
                mime_type="text/plain",
                size_bytes=100,
            )
            session.add(version)
            session.flush()
            document.current_version_id = version.id
            for chunk_index in range(2):
                session.add(
                    Chunk(
                        organisation_id=source.organisation_id,
                        document_version_id=version.id,
                        chunk_index=chunk_index,
                        content="medication incident supervisor"
                        if chunk_index == 0
                        else "Policy heading and labels",
                        start_offset=chunk_index * 100,
                        end_offset=(chunk_index + 1) * 100,
                        embedding_model=provider.model_id,
                        embedding=vector
                        if chunk_index == 0
                        else provider.embed_query("Policy heading labels"),
                    )
                )
            documents.append(document)
        session.flush()
        stale = DocumentVersion(
            organisation_id=own.id,
            document_id=documents[0].id,
            filename="old.txt",
            checksum="old",
            object_key="synthetic/old",
            mime_type="text/plain",
            size_bytes=30,
        )
        session.add(stale)
        session.flush()
        stale_chunk = Chunk(
            organisation_id=own.id,
            document_version_id=stale.id,
            chunk_index=0,
            content="old medication incident supervisor",
            start_offset=0,
            end_offset=40,
            embedding_model=provider.model_id,
            embedding=vector,
        )
        session.add(stale_chunk)
        session.commit()
        selected = [source.id for source in sources[:2]]
        documents[0].title = "ACME-118"
        session.commit()
        hits = knowledge.search_chunks(
            session,
            own.id,
            vector,
            provider.model_id,
            2,
            source_ids=selected,
            expand_neighbors=True,
            query_text="medication incident",
        )
        assert {hit.document.id for hit in hits} == {
            document.id for document in documents[:2]
        }
        assert stale_chunk.id not in {hit.chunk.id for hit in hits}
        assert len(hits) == 4
        citation = {
            "chunk_id": str(hits[0].chunk.id),
            "document_id": str(hits[0].document.id),
        }
        assert conversations.citations_available(session, own.id, [citation])
        assert not conversations.citations_available(session, foreign.id, [citation])
        assert not conversations.citations_available(
            session, own.id, [citation], source_ids=[]
        )
        exact_hits = knowledge.search_chunks(
            session,
            own.id,
            provider.embed_query("completely unrelated question"),
            provider.model_id,
            5,
            source_ids=selected,
            query_text="Who supplies ACME-118?",
        )
        assert any(
            hit.document.id == documents[0].id and hit.exact_match for hit in exact_hits
        )
        excluded_hits = knowledge.search_chunks(
            session,
            own.id,
            vector,
            provider.model_id,
            5,
            source_ids=[sources[1].id],
            query_text="Who supplies ACME-118?",
        )
        assert all(hit.document.id == documents[1].id for hit in excluded_hits)
        group = groups.save(session, own.id, "Operations", selected)
        resolved, _ = request_scope(
            session, own.id, SearchRequest(query="question", group_id=group.id)
        )
        grouped = knowledge.search_chunks(
            session, own.id, vector, provider.model_id, 2, source_ids=resolved
        )
        assert {hit.document.id for hit in grouped} == {
            document.id for document in documents[:2]
        }
        assert (
            knowledge.search_chunks(
                session, own.id, vector, provider.model_id, 2, source_ids=[]
            )
            == []
        )
        with pytest.raises(ValueError):
            knowledge.search_chunks(
                session,
                own.id,
                vector,
                provider.model_id,
                2,
                source_ids=[sources[-1].id],
            )
        documents[0].deleted_at = datetime.now(UTC)
        session.commit()
        with pytest.raises(ValueError):
            knowledge.search_chunks(
                session,
                own.id,
                vector,
                provider.model_id,
                2,
                document_ids=[documents[0].id],
            )
        remaining = knowledge.search_chunks(
            session,
            own.id,
            vector,
            provider.model_id,
            2,
            source_ids=selected,
            expand_neighbors=True,
        )
        assert {hit.document.id for hit in remaining} == {documents[1].id}
        with pytest.raises(ValueError):
            knowledge.validate_scope(session, own.id, None, [documents[1].id, uuid4()])


def test_complete_scope_requires_every_active_version_and_bounded_evidence():
    with database.SessionLocal() as session:
        organisation = Organisation(name="Coverage")
        session.add(organisation)
        session.flush()
        assert (
            knowledge.complete_scoped_evidence(
                session, organisation.id, "test", None, None
            )
            == []
        )
        document = Document(organisation_id=organisation.id, title="Not indexed")
        session.add(document)
        session.commit()
        assert (
            knowledge.complete_scoped_evidence(
                session, organisation.id, "test", None, None
            )
            is None
        )
        assert (
            knowledge.complete_scoped_evidence(
                session, organisation.id, "test", [], None
            )
            == []
        )
