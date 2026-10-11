# Local semantic retrieval

The API and ingestion worker use the same independently configured embedding provider. ChatMock supplies chat completions; its observed embedding endpoint returns 404. Local semantic development uses a small CPU FastEmbed runtime over the existing compatible HTTP interface.

Selected model: `sentence-transformers/all-MiniLM-L6-v2`, Apache-2.0, 384 dimensions. The runtime downloads only the ONNX/tokenizer files from `qdrant/all-MiniLM-L6-v2-onnx` at revision `d13954661f83248295ba75c1ed411eef3b7b936e`. The six pinned inference/tokenizer files total 91,333,544 bytes (87.1 MiB), measured in the DevPod model volume on 2026-10-07. Runtime inference uses those local files; it does not silently download a newer revision. The optional `semantic` dependency extra is locked in uv; the normal API/worker images and deterministic CI do not install it.

## Measured selection

On 2026-10-06, DevPod reported 20 CPUs and 33.5 GB memory. FastEmbed used two CPU threads. A 25-document synthetic corpus covers payslips, leave, incidents, procurement, operations and other knowledge questions.

| Model | Document Recall@5 | Recall@1 | Mean query inference | Initial download/load |
| --- | --- | --- | --- | --- |
| BAAI/bge-small-en-v1.5 | 100% | 80% | 4.0 ms | 6.1 s |
| sentence-transformers/all-MiniLM-L6-v2 | 100% | 92% | 1.8 ms | 6.7 s |

The selected pinned model also passed actual extraction/ingestion and PostgreSQL retrieval: 100% Recall@5, 92% Recall@1, median embedding-plus-search 6.8 ms. The payslip paraphrase ranked first. Four unrelated questions scored below 0.2; relevant paired scores ranged from 0.262 to 0.802, so the existing 0.2 threshold is retained for this initial model/fixture. Scores are not probabilities; recalibrate on additional answerable/unanswerable material before adopting another model or asserting broad quality.

These are small, warm, sequential synthetic measurements, not capacity or comprehensive quality claims. MiniLM has a 256-token input limit; extraction/chunk evaluation must account for truncation when adding larger or denser documents. Semantic ingestion now uses the pinned model tokenizer and limits chunks to 254 content tokens plus two special tokens; character offsets and full text coverage are preserved. Embedding requests contain at most 64 chunks.

### Comparing embedding models and reranking

`mise run benchmark-embeddings` runs an opt-in exact-cosine comparison against the
annotated synthetic passages and direct/paraphrased questions. It reports evidence
Recall@5/10, reciprocal-rank answer-quality proxy, evidence precision, query latency,
peak process memory, and direct/paraphrase category results. It measures embeddings
through the configured OpenAI-compatible endpoint and does not alter application
defaults. The default candidate IDs are the pinned MiniLM, `BAAI/bge-small-en-v1.5`,
and `intfloat/e5-small-v2`; set `METIS_BENCHMARK_MODELS` to a JSON string array and
`EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, and `EMBEDDING_DIMENSIONS` for the service.
The pinned single-model
runtime accepts only MiniLM, so comparison requires a compatible endpoint that exposes
each candidate ID, or configure one model at a time. Unavailable models are recorded
with their exact endpoint/dependency error; at least one model must complete. The report
defaults to ignored `.agent/embedding-benchmark.json`.

Optional reranking uses `METIS_RERANKER_BASE_URL`, `METIS_RERANKER_MODEL`, and optional
`METIS_RERANKER_API_KEY` for a Jina-compatible `/rerank` endpoint. It reranks at most
the top 40 candidates and reports the same retrieval metrics including reranking
latency. No endpoint means no reranking. Application retrieval exposes an injected
Python `Reranker` seam, and only callers that explicitly pass one activate it. This
fixture benchmark measures evidence ranking as an answer-quality proxy; use the
existing answer/pipeline evaluations before adopting a model or reranker.

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

The synthetic QA fixtures contain at least 50 annotated cases, including paraphrase,
multi-paragraph, multi-turn, negative and multi-citation examples. Fixture annotations
record an expected answer, synthetic source version, format, supporting sections and
chunk identifiers. Retrieval and pipeline evaluations turn each annotated format into
real PDF, DOCX, Markdown, TXT or CSV bytes and run those files through the normal upload
and ingestion path. CSV fixtures use labeled table columns. Deterministic tests validate
each generated file with upload validation and the production extractor. Reports identify
failures as `evidence_not_retrieved`,
`evidence_retrieved_but_discarded`, or
`evidence_supplied_answer_or_citation_incorrect`. Reports include configured embedding
and chat identities and index/re-index readiness. Case text stays synthetic.

`evaluate-retrieval` needs an isolated migrated PostgreSQL database and a reachable
embedding service configured for that database. `evaluate-answers` needs
`METIS_LIVE_CHAT_EVAL_ENABLED=true` and an explicitly configured local GPTMock; it
evaluates answer generation from synthetic excerpts without PostgreSQL. `evaluate-pipeline`
needs both services and an isolated migrated PostgreSQL database whose name ends in
`_verify` or `_evaluation`. These opt-in commands are available through the matching
`mise run evaluate-retrieval`, `mise run evaluate-answers`, and
`mise run evaluate-pipeline` tasks.

Production metadata tracing is disabled by default. Set
`METIS_DIAGNOSTICS_ENABLED=true` for restricted diagnostic runs to emit extraction
availability, candidate rank/score/provenance, selected evidence and outcome metadata.
Trace events contain identifiers and section labels, never question or source snippets;
limit access to the application diagnostic logs.

Provision and migrate an isolated PostgreSQL database first. The evaluation creates only unique synthetic organisations, stores originals in a temporary directory, runs the actual ingestion function, retrieves from PostgreSQL and removes its own data afterward.

```bash
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis_phase9_verify \
EMBEDDING_PROVIDER=openai-compatible EMBEDDING_BASE_URL=http://localhost:8003/v1 \
EMBEDDING_API_KEY=metis-local EMBEDDING_DIMENSIONS=384 \
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2@d13954661f83248295ba75c1ed411eef3b7b936e \
METIS_SEMANTIC_EVAL_ENABLED=true mise run evaluate-retrieval
```

Results go to ignored `.agent/semantic-evaluation.json`. This makes no chat-model requests. Missing opt-in/database configuration is rejected. Foreign-tenant evidence and the unanswerable cases are asserted alongside the Recall@5 target. Run the separate opt-in ChatMock check to validate generation.

### Retrieval and context limits

The retrieval and RAG limits can be adjusted with environment variables. Values are clamped to the ranges below:

| Setting | Default | Accepted range |
| --- | ---: | ---: |
| `RETRIEVAL_CANDIDATE_COUNT` | 40 | 1–200 candidates per retrieval branch |
| `RETRIEVAL_EVIDENCE_COUNT` | 8 | 1–40 selected passages |
| `RETRIEVAL_MIN_RELEVANCE` | 0.2 | 0–1 cosine relevance threshold |
| `RAG_EVIDENCE_TOKEN_BUDGET` | 6,000 | 256–32,768 tokens during selection and neighbour expansion |
| `RAG_CONTEXT_TOKEN_BUDGET` | 8,192 | 512–32,768 total context tokens |
| `RAG_HISTORY_TOKEN_RESERVE` | 512 | 0–32,768 minimum reserved history tokens; actual history size is measured |
| `RAG_INSTRUCTION_TOKEN_RESERVE` | 1,600 | 256–32,768 reserved tokens |
| `RAG_GENERATION_TOKEN_RESERVE` | 1,200 | 256–32,768 reserved tokens |

The context budget subtracts measured conversation history and the instruction and generation reservations before fitting evidence. Evidence retains a minimum allocation of 128 tokens.

Run the opt-in retrieval evaluation only against an isolated, migrated PostgreSQL database; it creates synthetic organisations and removes its own data. Configure `DATABASE_URL` for that database, the embedding service variables shown above, and `METIS_SEMANTIC_EVAL_ENABLED=true`, then run `mise run evaluate-retrieval`. The report includes before/after evidence recall. This evaluation was not run during the current phase-1 work because `DATABASE_URL` was unset; the deterministic retrieval/RAG fixture tests ran as part of `./scripts/verify`.

For a host process rather than the optional container, `mise run download-embeddings` installs the extra and downloads the same pinned model. Run `uv run --locked --project src/backend --extra semantic metis-embeddings` to serve it on loopback8003; avoid sharing that environment with concurrent dependency synchronization.

References: [FastEmbed supported models](https://qdrant.github.io/fastembed/examples/Supported_Models/) and [pinned model repository](https://huggingface.co/qdrant/all-MiniLM-L6-v2-onnx/tree/d13954661f83248295ba75c1ed411eef3b7b936e).
