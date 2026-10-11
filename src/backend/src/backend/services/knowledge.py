import os
import re
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend import observability
from backend.models import Chunk, Document, DocumentVersion, Organisation, Source


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    document: Document
    source: Source | None
    score: float
    exact_match: bool = False
    provenance: frozenset[str] = frozenset()
    rank_score: float = 0.0


class Reranker(Protocol):
    """Optional ranking provider; callers must explicitly opt in."""

    def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]: ...


def retrieval_settings() -> tuple[int, int, float, int]:
    return (
        min(200, max(1, int(os.environ.get("RETRIEVAL_CANDIDATE_COUNT", "40")))),
        min(40, max(1, int(os.environ.get("RETRIEVAL_EVIDENCE_COUNT", "8")))),
        min(1.0, max(0.0, float(os.environ.get("RETRIEVAL_MIN_RELEVANCE", "0.2")))),
        min(32768, max(256, int(os.environ.get("RAG_EVIDENCE_TOKEN_BUDGET", "6000")))),
    )


def relevant_neighbour(query_text: str | None, content: str, score: float) -> bool:
    _, _, minimum_relevance, _ = retrieval_settings()
    if score >= minimum_relevance:
        return True
    query_terms = set(re.findall(r"[a-z0-9]{3,}", (query_text or "").casefold()))
    content_terms = set(re.findall(r"[a-z0-9]{3,}", content.casefold()))
    return bool(
        query_terms and len(query_terms & content_terms) / len(query_terms) >= 0.5
    )


def provenance_label(hit: SearchHit) -> str:
    if hit.exact_match:
        return "exact"
    if "neighbour" in hit.provenance:
        return "neighbour"
    origins = hit.provenance & {"semantic", "lexical"}
    if origins == {"semantic", "lexical"}:
        return "both"
    if origins:
        return next(iter(origins))
    return "semantic"


def create_organisation(session: Session, name: str) -> Organisation:
    organisation = Organisation(name=name.strip())
    session.add(organisation)
    session.commit()
    session.refresh(organisation)
    return organisation


def create_source(
    session: Session,
    organisation_id: uuid.UUID,
    name: str,
    source_type: str,
    configuration: dict,
) -> Source | None:
    if session.get(Organisation, organisation_id) is None:
        return None
    source = Source(
        organisation_id=organisation_id,
        name=name.strip(),
        type=source_type,
        configuration=configuration,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def list_sources(session: Session, organisation_id: uuid.UUID) -> list[Source]:
    return list(
        session.scalars(
            select(Source)
            .where(
                Source.organisation_id == organisation_id,
                Source.configuration["disconnected"].as_boolean().is_not(True),
            )
            .order_by(Source.created_at, Source.id)
        )
    )


def create_document(
    session: Session,
    organisation_id: uuid.UUID,
    title: str,
    source_id: uuid.UUID | None,
    source_uri: str | None,
) -> Document | None:
    if session.get(Organisation, organisation_id) is None:
        return None
    if (
        source_id is not None
        and session.scalar(
            select(Source.id).where(
                Source.id == source_id,
                Source.organisation_id == organisation_id,
                Source.configuration["disconnected"].as_boolean().is_not(True),
            )
        )
        is None
    ):
        return None
    document = Document(
        organisation_id=organisation_id,
        title=title.strip(),
        source_id=source_id,
        source_uri=source_uri,
    )
    session.add(document)
    session.commit()
    session.refresh(document)
    return document


def list_documents(session: Session, organisation_id: uuid.UUID) -> list[Document]:
    return list(
        session.scalars(
            select(Document)
            .where(Document.organisation_id == organisation_id)
            .order_by(Document.created_at.desc(), Document.id)
        )
    )


def get_document(
    session: Session, organisation_id: uuid.UUID, document_id: uuid.UUID
) -> Document | None:
    return session.scalar(
        select(Document).where(
            Document.organisation_id == organisation_id,
            Document.id == document_id,
        )
    )


def validate_scope(
    session: Session,
    organisation_id: uuid.UUID,
    source_ids: list[uuid.UUID] | None,
    document_ids: list[uuid.UUID] | None,
) -> None:
    for model, identifiers in ((Source, source_ids), (Document, document_ids)):
        if identifiers is None:
            continue
        statement = select(model.id).where(
            model.organisation_id == organisation_id, model.id.in_(identifiers)
        )
        if model is Source:
            statement = statement.where(
                Source.configuration["disconnected"].as_boolean().is_not(True)
            )
        if model is Document:
            statement = statement.where(Document.deleted_at.is_(None))
        found = set(session.scalars(statement))
        if found != set(identifiers):
            raise ValueError("Knowledge scope contains unavailable IDs")


class IndexUnavailable(RuntimeError):
    pass


def ensure_index_ready(
    session: Session,
    organisation_id: uuid.UUID,
    model_id: str | None = None,
    *,
    lock: bool = False,
) -> None:
    statement = (
        select(Organisation)
        .where(Organisation.id == organisation_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    organisation = session.scalar(statement)
    if organisation is not None and (
        organisation.index_status != "ready"
        or (
            model_id is not None
            and organisation.index_model is not None
            and organisation.index_model != model_id
        )
    ):
        raise IndexUnavailable(
            "Knowledge is being re-indexed or uses another embedding model. Ask an administrator to check indexing status."
        )
    if (
        organisation is not None
        and organisation.index_model is None
        and model_id is not None
    ):
        models = set(
            session.scalars(
                select(Chunk.embedding_model)
                .join(
                    Document, Document.current_version_id == Chunk.document_version_id
                )
                .where(
                    Document.organisation_id == organisation_id,
                    Chunk.organisation_id == organisation_id,
                )
            )
        )
        if models and models != {model_id}:
            raise IndexUnavailable(
                "Knowledge uses another embedding model; re-index the organisation before searching or uploading."
            )


@observability.RETRIEVAL_DURATION.time()
def search_chunks(
    session: Session,
    organisation_id: uuid.UUID,
    query_embedding: list[float],
    embedding_model: str,
    limit: int,
    source_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    *,
    source_ids: list[uuid.UUID] | None = None,
    document_ids: list[uuid.UUID] | None = None,
    expand_neighbors: bool = False,
    query_text: str | None = None,
    reranker: Reranker | None = None,
) -> list[SearchHit]:
    candidate_count, default_evidence_count, _, token_budget = retrieval_settings()
    evidence_limit = (
        min(limit, default_evidence_count) if limit else default_evidence_count
    )
    validate_scope(session, organisation_id, source_ids, document_ids)
    validate_scope(
        session,
        organisation_id,
        [source_id] if source_id else None,
        [document_id] if document_id else None,
    )
    ensure_index_ready(session, organisation_id, embedding_model)
    distance = Chunk.embedding.cosine_distance(query_embedding)
    statement = (
        select(Chunk, Document, Source, distance.label("distance"))
        .join(
            DocumentVersion,
            (DocumentVersion.id == Chunk.document_version_id)
            & (DocumentVersion.organisation_id == Chunk.organisation_id),
        )
        .join(
            Document,
            (Document.id == DocumentVersion.document_id)
            & (Document.organisation_id == Chunk.organisation_id),
        )
        .outerjoin(
            Source,
            (Source.id == Document.source_id)
            & (Source.organisation_id == Document.organisation_id),
        )
        .where(
            Chunk.organisation_id == organisation_id,
            Document.organisation_id == organisation_id,
            Document.current_version_id == Chunk.document_version_id,
            Chunk.embedding_model == embedding_model,
        )
    )
    if source_id is not None:
        statement = statement.where(Document.source_id == source_id)
    if document_id is not None:
        statement = statement.where(Document.id == document_id)
    statement = statement.where(Document.deleted_at.is_(None))

    if source_ids is not None:
        statement = statement.where(Document.source_id.in_(source_ids))
    if document_ids is not None:
        statement = statement.where(Document.id.in_(document_ids))

    rows = session.execute(
        statement.order_by(distance, Chunk.id).limit(candidate_count)
    )
    candidates = [
        SearchHit(
            chunk=chunk,
            document=document,
            source=source,
            score=1 - distance_value,
            provenance=frozenset({"semantic"}),
        )
        for chunk, document, source, distance_value in rows
    ]

    if query_text:
        searchable = func.concat(Document.title, " ", Chunk.content)
        tsquery = func.websearch_to_tsquery("english", query_text)
        text_vector = func.to_tsvector("english", searchable)
        terms = [
            match.group(0).strip('"')
            for match in re.finditer(
                r'\b[A-Za-z][A-Za-z0-9]*[-_][A-Za-z0-9_-]+\b|\$?\d+\.\d{2}\b|"[^"\n]{3,100}"',
                query_text,
            )
        ][:8]
        predicates = [
            searchable.op("~*")(
                "(^|[^[:alnum:]])" + re.escape(term) + "([^[:alnum:]]|$)"
            )
            for term in terms
        ]
        exact = or_(*predicates) if predicates else None
        match = text_vector.op("@@")(tsquery)
        if exact is not None:
            match = or_(match, exact)
        lexical_statement = statement.where(match)
        if exact is not None:
            lexical_statement = lexical_statement.order_by(exact.desc())
        lexical_rows = session.execute(
            lexical_statement.order_by(
                func.ts_rank_cd(text_vector, tsquery).desc(), Chunk.id
            ).limit(candidate_count)
        )
        lexical = [
            SearchHit(
                chunk,
                document,
                source,
                1 - distance_value,
                any(
                    re.search(
                        r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])",
                        document.title + " " + chunk.content,
                        re.IGNORECASE,
                    )
                    for term in terms
                ),
            )
            for chunk, document, source, distance_value in lexical_rows
        ]
        candidates = fuse_rankings(candidates, lexical)
    if reranker is not None and query_text:
        reranked = reranker.rerank(query_text, candidates[:40])
        candidate_ids = {hit.chunk.id for hit in candidates}
        if any(hit.chunk.id not in candidate_ids for hit in reranked):
            raise ValueError("Reranker returned a chunk outside the candidate set")
        candidates = reranked + [
            hit
            for hit in candidates
            if hit.chunk.id not in {x.chunk.id for x in reranked}
        ]
    selected = select_evidence(
        candidates,
        evidence_limit,
        token_budget=token_budget,
        allow_same_document=document_id is not None or document_ids is not None,
    )
    if not expand_neighbors:
        return selected
    # Adjacent chunks carry headings and labels; retain their own citation identity.
    from backend.services.chunking import minilm_tokenizer

    tokenizer = minilm_tokenizer()
    consumed = sum(len(tokenizer.encode(hit.chunk.content).ids) for hit in selected)
    expanded = list(selected)
    seen = {hit.chunk.id for hit in selected}
    for hit in selected:
        neighbour_distance = Chunk.embedding.cosine_distance(query_embedding)
        neighbour_statement = (
            select(Chunk, Document, Source, neighbour_distance.label("distance"))
            .join(
                DocumentVersion,
                (DocumentVersion.id == Chunk.document_version_id)
                & (DocumentVersion.organisation_id == Chunk.organisation_id),
            )
            .join(
                Document,
                (Document.id == DocumentVersion.document_id)
                & (Document.organisation_id == Chunk.organisation_id),
            )
            .outerjoin(
                Source,
                (Source.id == Document.source_id)
                & (Source.organisation_id == Document.organisation_id),
            )
            .where(
                Chunk.organisation_id == organisation_id,
                Document.organisation_id == organisation_id,
                Document.current_version_id == Chunk.document_version_id,
                Document.deleted_at.is_(None),
                Chunk.document_version_id == hit.chunk.document_version_id,
                Chunk.embedding_model == embedding_model,
                Chunk.chunk_index.in_(
                    [hit.chunk.chunk_index - 1, hit.chunk.chunk_index + 1]
                ),
            )
        )
        if source_id is not None:
            neighbour_statement = neighbour_statement.where(
                Document.source_id == source_id
            )
        if document_id is not None:
            neighbour_statement = neighbour_statement.where(Document.id == document_id)
        if source_ids is not None:
            neighbour_statement = neighbour_statement.where(
                Document.source_id.in_(source_ids)
            )
        if document_ids is not None:
            neighbour_statement = neighbour_statement.where(
                Document.id.in_(document_ids)
            )
        neighbours = session.execute(neighbour_statement.order_by(Chunk.chunk_index))
        for (
            neighbour,
            neighbour_document,
            neighbour_source,
            distance_value,
        ) in neighbours:
            if (
                neighbour.id in seen
                or not relevant_neighbour(
                    query_text, neighbour.content, 1 - distance_value
                )
                or consumed + len(tokenizer.encode(neighbour.content).ids)
                > token_budget
                or len(expanded) >= evidence_limit * 3
            ):
                continue
            expanded.append(
                SearchHit(
                    neighbour,
                    neighbour_document,
                    neighbour_source,
                    1 - distance_value,
                    hit.exact_match,
                    frozenset({"neighbour"}),
                )
            )
            seen.add(neighbour.id)
            consumed += len(tokenizer.encode(neighbour.content).ids)
    return expanded


def select_evidence(
    candidates: list[SearchHit],
    limit: int,
    budget: int = 12000,
    *,
    token_budget: int | None = None,
    allow_same_document: bool = False,
) -> list[SearchHit]:
    """Cover relevant documents first, then fill with distinct bounded excerpts."""
    first = []
    remaining = []
    seen_documents = set()
    _, _, minimum_relevance, _ = retrieval_settings()
    for hit in candidates:
        if (
            hit.score >= minimum_relevance
            or hit.exact_match
            or ("lexical" in hit.provenance and hit.rank_score >= 1 / 61)
        ) and hit.document.id not in seen_documents:
            first.append(hit)
            seen_documents.add(hit.document.id)
        else:
            remaining.append(hit)
    selected = []
    consumed = 0
    for hit in first + remaining:
        if len(selected) >= limit:
            break
        if any(
            previous.chunk.document_version_id == hit.chunk.document_version_id
            and max(previous.chunk.start_offset, hit.chunk.start_offset)
            < min(previous.chunk.end_offset, hit.chunk.end_offset)
            for previous in selected
        ):
            continue
        if (
            not allow_same_document
            and any(previous.document.id == hit.document.id for previous in selected)
            and not (
                hit.score >= minimum_relevance
                or hit.exact_match
                or ("lexical" in hit.provenance and hit.rank_score >= 1 / 61)
            )
        ):
            continue
        length = len(hit.chunk.content)
        measured = length
        if token_budget is not None:
            from backend.services.chunking import minilm_tokenizer

            measured = len(minilm_tokenizer().encode(hit.chunk.content).ids)
        if consumed + measured > (token_budget if token_budget is not None else budget):
            continue
        selected.append(hit)
        consumed += measured
    return selected


def fuse_rankings(
    semantic: list[SearchHit], lexical: list[SearchHit]
) -> list[SearchHit]:
    if not lexical:
        return semantic
    scores = {}
    hits = {}
    provenance = {}
    for ranking in (semantic, lexical):
        origin = "semantic" if ranking is semantic else "lexical"
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.chunk.id] = scores.get(hit.chunk.id, 0.0) + 1 / (60 + rank)
            hits[hit.chunk.id] = hit
            provenance.setdefault(hit.chunk.id, set()).add(origin)
    return [
        SearchHit(
            hits[identifier].chunk,
            hits[identifier].document,
            hits[identifier].source,
            hits[identifier].score,
            hits[identifier].exact_match,
            frozenset(provenance[identifier]),
            scores[identifier],
        )
        for identifier in sorted(
            scores, key=lambda identifier: (-scores[identifier], str(identifier))
        )
    ]


def complete_scoped_evidence(
    session: Session,
    organisation_id: uuid.UUID,
    model_id: str,
    source_ids: list[uuid.UUID] | None,
    document_ids: list[uuid.UUID] | None,
) -> list[SearchHit] | None:
    """Prove coverage only for a small complete active indexed scope."""
    validate_scope(session, organisation_id, source_ids, document_ids)
    ensure_index_ready(session, organisation_id, model_id)
    statement = select(Document).where(
        Document.organisation_id == organisation_id, Document.deleted_at.is_(None)
    )
    if source_ids is not None:
        statement = statement.where(Document.source_id.in_(source_ids))
    if document_ids is not None:
        statement = statement.where(Document.id.in_(document_ids))
    documents = list(session.scalars(statement.order_by(Document.id).limit(51)))
    if len(documents) > 50 or any(
        document.current_version_id is None for document in documents
    ):
        return None
    if not documents:
        return []
    by_version = {document.current_version_id: document for document in documents}
    chunks = list(
        session.scalars(
            select(Chunk)
            .where(
                Chunk.organisation_id == organisation_id,
                Chunk.document_version_id.in_(by_version),
                Chunk.embedding_model == model_id,
            )
            .order_by(Chunk.document_version_id, Chunk.chunk_index)
            .limit(21)
        )
    )
    if (
        len(chunks) > 20
        or sum(len(chunk.content) for chunk in chunks) > 12000
        or set(by_version) != {chunk.document_version_id for chunk in chunks}
    ):
        return None
    sources = {source.id: source for source in list_sources(session, organisation_id)}
    return [
        SearchHit(
            chunk,
            by_version[chunk.document_version_id],
            sources.get(by_version[chunk.document_version_id].source_id),
            1.0,
        )
        for chunk in chunks
    ]
