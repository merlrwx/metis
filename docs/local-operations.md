# Local operation and recovery

Run `mise run local` from the host with the existing Metis DevPod configured. It creates only read-only service forwards and Metis local Docker services. It never bootstraps Flux or changes a shared cluster. Install DevPod, OpenSSH, kubectl, Python and mise on the host first; run `mise install` in the DevPod. The pinned model is downloaded into the existing Docker model volume. Missing dependencies, occupied incompatible ports and failed service/model checks stop with recovery information. The automatic command is for the documented existing ChatMock bridge, not provisioning a new account.

## Fast tiny local chat model

For a fully local speed-first chat path, combine `compose.local-llm.yaml` with the semantic embedding override. The override runs Ollama, binds its API to host loopback `127.0.0.1:11434`, and configures the backend's existing OpenAI-compatible chat client with:

- `GPTMOCK_BASE_URL=http://llm:11434/v1`
- `GPTMOCK_API_KEY=ollama-local`
- `GPTMOCK_MODEL=${LOCAL_LLM_MODEL:-smollm2:135m}`
- `GPTMOCK_TIMEOUT=${LOCAL_LLM_TIMEOUT:-30}`
- `GPTMOCK_MAX_RETRIES=${LOCAL_LLM_MAX_RETRIES:-0}`

Download the default tiny model once:

```bash
mise exec -- docker compose -f compose.yaml -f compose.semantic.yaml -f compose.local-llm.yaml up -d llm
mise exec -- docker compose -f compose.yaml -f compose.semantic.yaml -f compose.local-llm.yaml exec llm ollama pull smollm2:135m
```

Start the local stack:

```bash
LOCAL_LLM_MODEL=smollm2:135m \
  mise exec -- docker compose -f compose.yaml -f compose.semantic.yaml -f compose.local-llm.yaml up --build --wait
```

Use `LOCAL_LLM_MODEL=smollm2:360m`, `LOCAL_LLM_MODEL=qwen2.5:0.5b` or another pulled Ollama model to trade more memory and latency for better instruction following. Pull the selected model before starting the full stack if the host cannot download during first chat. These chat model changes do not require re-indexing; only embedding model changes do.

This mode optimises for responsiveness, not quality. Very small local models can fail grounded-answer formatting or citation instructions, so Metis may reject their output and return the configured no-evidence answer. Keep the ChatMock/live evaluation path for answer-quality review.

## Request and ingestion bounds

JSON bodies are limited to 64 KiB, including chunked transfer. Upload bodies allow a 20 MiB file plus 64 KiB multipart overhead; file validation still enforces 20 MiB. DOCX expansion is limited to 20 MiB, extracted text to 2 million characters, and documents to 2,000 searchable chunks. CSV additionally limits 2,000 rows, 50 columns and 1,000 characters per cell. Split oversized documents and export searchable text for scanned PDFs.

Admission defaults to 30 expensive requests per user **within an organisation**, and 120 per organisation per UTC minute. Search, chat, uploads, connection checks and manual sync share this budget. `METIS_USER_REQUESTS_PER_MINUTE` and `METIS_ORG_REQUESTS_PER_MINUTE` may be set from 1 to 10,000. PostgreSQL serialises admission across API processes; 429 includes `Retry-After`. Provider failures consume admission, while a successful cached UUID chat retry does not. This is a small deployment budget, not an unauthenticated edge firewall.

Chat output is capped at 4,096 tokens, each model attempt at 120 seconds, with at most one retry. Frontend chat waits up to 300 seconds to accommodate bounded retries. Operational Prometheus labels distinguish extraction/embedding/index failures, empty retrieval, rejected evidence, generation failure and answer outcome. Metrics and audit logs contain counts and identifiers, never document excerpts or prompts. Queue depth and oldest waiting-job age remain available at `/metrics`.

## Evaluation

Normal verification is deterministic and does not contact ChatMock. Run `scripts/verify`, the isolated PostgreSQL/Redis suite and disposable Kubernetes E2E in DevPod. Never point destructive test fixtures at the application `metis` database or Redis0.

After migrating an isolated PostgreSQL database, set the semantic environment from [semantic-search.md](semantic-search.md), plus:

```bash
METIS_LIVE_CHAT_EVAL_ENABLED=true GPTMOCK_BASE_URL=http://127.0.0.1:8002/v1 \
  mise run evaluate-pipeline
```

`DATABASE_URL` must name an isolated PostgreSQL database ending `_verify` or `_evaluation`. This uploads synthetic originals through the document service, runs the actual ingestion worker function, retrieves from the combined PostgreSQL corpus, then generates live ChatMock answers. Its second source uses the external connector storage path with fixture metadata; it is **not live Microsoft 365**. The report separates retrieved expected-document coverage, retrieval latency and answer correctness checks. Read the synthetic answers and verify every claim against the fixture: keyword checks and citation counts alone do not prove claim support. Results are ignored `.agent/pipeline-evaluation.json`.

`evaluate-retrieval` independently measures 25 paraphrased queries, negative cases and Recall@5 without generation; `evaluate-answers` isolates generation over preselected excerpts. These are different checks and should be reported separately. Access, stale-version, scanned-file, UUID concurrency and complete-scope arithmetic negatives are covered by deterministic/isolated PostgreSQL tests.

## Backup and restore

Quiesce uploads, source sync and workers before taking matching metadata/original snapshots. Retain originals and history according to your organisation policy. Backups contain private content: keep them outside Git, permission 700 for their directory and 600 for files, with separately managed encryption/access/retention.

Inside DevPod, a PostgreSQL backup uses:

```bash
docker compose exec -T database pg_dump -U metis -d metis -Fc > /external/private/metis.dump
```

Back up the object store at the same checkpoint. With the local Compose store, `docker compose cp backend:/data/metis-objects /external/private/objects` copies originals; verify the configured `OBJECT_STORAGE_LOCAL_DIR` first. For S3, snapshot/copy the bucket objects under the same checkpoint using your configured operator tooling. Never restore metadata without the referenced original keys.

Restore into a **new isolated** database and empty object-store root first. Apply `pg_restore --exit-on-error --no-owner` to the new database, configure an isolated API/worker against that database and the copied objects, and check every `DocumentVersion.object_key` exists and SHA-256 matches `checksum`. Check current-version pointers, retained historical versions and indexed chunks. Run normal migration checks. Do not overwrite the user application while validating recovery.

Redis is a delivery mechanism; PostgreSQL remains the job ledger. After a restore, run `metis-reconcile` against the restored database and a disposable Redis instance/database to republish pending jobs. Observe restored originals successfully being indexed. Inspect processing/queued jobs and worker lease/reclaim behavior before enabling normal scheduling; reconcile handles pending publications, not arbitrary status rewriting.

Repeatable synthetic local recovery check:

```bash
METIS_RECOVERY_EVAL_ENABLED=true REDIS_URL=redis://localhost:6379/15 \
  mise run evaluate-recovery
```

It creates two uniquely named local Docker PostgreSQL databases, migrates/seeds originals and historical/current versions, dumps/restores metadata and archives/extracts objects, verifies keys/checksums and chunk counts, reconciles one pending job into Redis15, and indexes its restored original. It removes only its own databases/temp objects/published message. Do not run concurrently with other Redis15 checks. Application `metis` and Redis0 remain untouched. Worker delivery/reclaim is additionally tested by the isolated queue suite and disposable k3d E2E.

## Model upgrades

The ONNX runtime is locked in uv and the model/tokenizer revision is pinned in code and resources. Upgrade deliberately: back up metadata/originals, evaluate the proposed model with the same fixtures, change runtime/model/tokenizer together, verify dimensions, then re-index each affected organisation through the existing maintenance CLI. Resume search only after all active chunks use the selected model. Record retrieval, negative cases, resource use and live answer review before accepting the upgrade. Do not compare vectors from different model spaces or silently fall back to hashing.
