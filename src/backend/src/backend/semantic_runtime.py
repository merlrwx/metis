"""Optional pinned CPU embedding service; install the semantic dependency extra."""

import os
from functools import lru_cache
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_REPOSITORY = "qdrant/all-MiniLM-L6-v2-onnx"
MODEL_REVISION = "d13954661f83248295ba75c1ed411eef3b7b936e"
MODEL_ID = f"{MODEL_NAME}@{MODEL_REVISION}"
MODEL_DIMENSIONS = 384
MODEL_FILES = [
    "config.json",
    "model.onnx",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
]


def model_directory():
    return Path(os.environ.get("METIS_MODEL_DIR", ".agent/semantic-model"))


def download():
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=MODEL_REPOSITORY,
        revision=MODEL_REVISION,
        allow_patterns=MODEL_FILES,
        local_dir=model_directory(),
    )
    print(f"Downloaded pinned model {MODEL_ID} to {model_directory()}")


@lru_cache(maxsize=1)
def model():
    from fastembed import TextEmbedding

    directory = model_directory()
    if any(not (directory / filename).is_file() for filename in MODEL_FILES):
        raise RuntimeError(
            "Pinned model files are missing; run metis-download-embeddings first"
        )
    return TextEmbedding(
        model_name=MODEL_NAME,
        specific_model_path=directory,
        local_files_only=True,
        threads=2,
    )


class EmbeddingRequest(BaseModel):
    model: str
    input: list[str] = Field(min_length=1, max_length=64)


app = FastAPI(title="Metis local embeddings")


@app.get("/health")
def health():
    try:
        vector = next(model().embed(["Synthetic readiness check"]))
    except (RuntimeError, OSError, ValueError, ImportError) as error:
        raise HTTPException(
            status_code=503, detail="Pinned embedding model is not ready"
        ) from error
    return {"model": MODEL_ID, "dimensions": len(vector)}


@app.post("/v1/embeddings")
def embed(payload: EmbeddingRequest):
    if payload.model != MODEL_ID:
        raise HTTPException(
            status_code=422, detail="Embedding model does not match the pinned runtime"
        )
    if any(not value.strip() or len(value) > 8000 for value in payload.input):
        raise HTTPException(
            status_code=422, detail="Inputs must contain 1–8000 characters"
        )
    try:
        vectors = model().embed(payload.input)
        return {
            "model": MODEL_ID,
            "data": [
                {"index": i, "embedding": vector.tolist()}
                for i, vector in enumerate(vectors)
            ],
        }
    except (RuntimeError, OSError, ValueError, ImportError) as error:
        raise HTTPException(
            status_code=503, detail="Embedding generation failed"
        ) from error


def main():
    uvicorn.run(
        "backend.semantic_runtime:app",
        host=os.environ.get("METIS_EMBEDDING_HOST", "127.0.0.1"),
        port=int(os.environ.get("METIS_EMBEDDING_PORT", "8003")),
    )
