# Local semantic retrieval

The API and ingestion worker use the same independently configured embedding provider. ChatMock supplies chat completions; its observed embedding endpoint returns 404. Local semantic development uses a small CPU FastEmbed runtime over the existing compatible HTTP interface.

Selected model: `sentence-transformers/all-MiniLM-L6-v2`, Apache-2.0, 384 dimensions. The runtime downloads only the ONNX/tokenizer files from `qdrant/all-MiniLM-L6-v2-onnx` at revision `d13954661f83248295ba75c1ed411eef3b7b936e`. Runtime inference uses those local files; it does not silently download a newer revision. The optional `semantic` dependency extra is locked in uv; the normal API/worker images and deterministic CI do not install it.

## Measured selection

On 2026-10-06, DevPod reported 20 CPUs and 33.5 GB memory. FastEmbed used two CPU threads. A 25-document synthetic corpus covers payslips, leave, incidents, procurement, operations and other knowledge questions.

| Model | Document Recall@5 | Recall@1 | Mean query inference | Initial download/load |
| --- | --- | --- | --- | --- |
| BAAI/bge-small-en-v1.5 | 100% | 80% | 4.0 ms | 6.1 s |
| sentence-transformers/all-MiniLM-L6-v2 | 100% | 92% | 1.8 ms | 6.7 s |

The selected pinned model also passed actual extraction/ingestion and PostgreSQL retrieval: 100% Recall@5, 92% Recall@1, median embedding-plus-search 6.8 ms. The payslip paraphrase ranked first. Four unrelated questions scored below 0.2; relevant paired scores ranged from 0.262 to 0.802, so the existing 0.2 threshold is retained for this initial model/fixture. Scores are not probabilities; recalibrate on additional answerable/unanswerable material before adopting another model or asserting broad quality.

These are small, warm, sequential synthetic measurements, not capacity or comprehensive quality claims. MiniLM has a 256-token input limit; extraction/chunk evaluation must account for truncation when adding larger or denser documents. The current character-based splitter is a known limitation scheduled for the document-quality phase.

## Start in DevPod

Keep the existing ChatMock and UI/API tunnels active. From `/workspaces/metis`:

```bash
mise exec -- docker compose -f compose.yaml -f compose.semantic.yaml build embeddings
mise exec -- docker compose -f compose.yaml -f compose.semantic.yaml run --rm embeddings /app/.venv/bin/metis-download-embeddings
GPTMOCK_BASE_URL=http://host.docker.internal:8002/v1 \
  mise exec -- docker compose -f compose.yaml -f compose.semantic.yaml up --build -d --wait
```

The embedding runtime is available on DevPod loopback port 8003; API and worker use the internal `embeddings:8003` address. The named model volume retains the pinned artifacts. Missing files fail readiness with an explicit error; repeat the download command to recover. The runtime container has a 1 GB memory limit. CPU-only local inference sends no document text to an external embedding provider.

Base Compose retains hashing for deterministic development; the semantic override selects the real model for both consumers. Existing knowledge must be re-indexed when changing model. A configuration mismatch or maintenance state returns an indexing error, rather than an empty-answer response.

## Re-index existing knowledge

Back up PostgreSQL and original objects before rebuilding an existing corpus. Keep the backup outside Git. Wait for pending ingestion/source sync to finish. Stop scheduling new syncs during the maintenance operation.

```bash
mise exec -- docker compose exec -T database pg_dump -U metis -d metis -Fc > /path/outside/repository/metis-before-reindex.dump
mise exec -- docker compose -f compose.yaml -f compose.semantic.yaml exec backend /app/.venv/bin/metis-reindex ORGANISATION_UUID
```

The CLI requires PostgreSQL and obtains a per-organisation advisory lock. It checks the embedding service, marks the corpus as re-indexing, and uses the existing queue/worker jobs to rebuild current indexed document versions. Documents that previously failed without producing an index retain that visible failure and are reported as skipped; they require extraction recovery rather than a model migration. Search, uploads and source ingestion for that organisation are blocked during maintenance; other organisations remain independently scoped. Original objects and extracted version history are retained. Chunk replacement is transactional per document.

A failure leaves maintenance visible. Fix the dependency and repeat the same command; successfully rebuilt jobs are skipped. A timeout does not make the index ready. Completion checks every active ingestion job's stored model before recording the corpus model and enabling retrieval. Use a worker configured for the target model. Zero-downtime dual indexes are not implemented.

The migration changes the vector column from fixed 1536 dimensions to variable dimensions without deleting existing vectors. Provider validation checks the configured output dimension before storing new vectors. A downgrade refuses incompatible rows instead of truncating, padding or deleting them; rebuild with a 1536-dimensional provider or restore the backup before rolling back the schema.

## Evaluation

Provision and migrate an isolated PostgreSQL database first. The evaluation creates only unique synthetic organisations, stores originals in a temporary directory, runs the actual ingestion function, retrieves from PostgreSQL and removes its own data afterward.

```bash
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis_phase9_verify \
EMBEDDING_PROVIDER=openai-compatible EMBEDDING_BASE_URL=http://localhost:8003/v1 \
EMBEDDING_API_KEY=metis-local EMBEDDING_DIMENSIONS=384 \
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2@d13954661f83248295ba75c1ed411eef3b7b936e \
METIS_SEMANTIC_EVAL_ENABLED=true mise run evaluate-retrieval
```

Results go to ignored `.agent/semantic-evaluation.json`. This makes no chat-model requests. Missing opt-in/database configuration is rejected. Foreign-tenant evidence and the unanswerable cases are asserted alongside the Recall@5 target. Run the separate opt-in ChatMock check to validate generation.

For a host process rather than the optional container, `mise run download-embeddings` installs the extra and downloads the same pinned model. Run `uv run --locked --project src/backend --extra semantic metis-embeddings` to serve it on loopback8003; avoid sharing that environment with concurrent dependency synchronization.

References: [FastEmbed supported models](https://qdrant.github.io/fastembed/examples/Supported_Models/) and [pinned model repository](https://huggingface.co/qdrant/all-MiniLM-L6-v2-onnx/tree/d13954661f83248295ba75c1ed411eef3b7b936e).
