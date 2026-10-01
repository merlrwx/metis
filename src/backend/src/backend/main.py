import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import get_session
from backend.schemas import (
    DocumentCreate,
    DocumentView,
    OrganisationCreate,
    OrganisationView,
    SourceCreate,
    SourceView,
)
from backend.services import knowledge


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield


app = FastAPI(title="Metis API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health(session: Annotated[Session, Depends(get_session)]) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/info")
def info() -> dict[str, str]:
    return {
        "name": "Metis",
        "description": "Grounded answers from your organisation's knowledge.",
        "stage": "database-foundation",
    }


@app.post("/api/organisations", response_model=OrganisationView, status_code=201)
def create_organisation(
    payload: OrganisationCreate, session: Annotated[Session, Depends(get_session)]
) -> OrganisationView:
    return knowledge.create_organisation(session, payload.name)


@app.post(
    "/api/organisations/{organisation_id}/sources",
    response_model=SourceView,
    status_code=201,
)
def create_source(
    organisation_id: UUID,
    payload: SourceCreate,
    session: Annotated[Session, Depends(get_session)],
) -> SourceView:
    source = knowledge.create_source(
        session,
        organisation_id,
        payload.name,
        payload.type,
        payload.configuration,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Organisation not found")
    return source


@app.post(
    "/api/organisations/{organisation_id}/documents",
    response_model=DocumentView,
    status_code=201,
)
def create_document(
    organisation_id: UUID,
    payload: DocumentCreate,
    session: Annotated[Session, Depends(get_session)],
) -> DocumentView:
    document = knowledge.create_document(
        session,
        organisation_id,
        payload.title,
        payload.source_id,
        payload.source_uri,
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Organisation or source not found")
    return document


@app.get(
    "/api/organisations/{organisation_id}/documents",
    response_model=list[DocumentView],
)
def list_documents(
    organisation_id: UUID, session: Annotated[Session, Depends(get_session)]
) -> list[DocumentView]:
    return knowledge.list_documents(session, organisation_id)


def main() -> None:
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
