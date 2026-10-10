# Local operation and recovery

Run `mise run local` from the host with the existing Metis DevPod configured. It creates only read-only service forwards and Metis local Docker services. It never bootstraps Flux or changes a shared cluster. Install DevPod, OpenSSH, kubectl, Python and mise on the host first; run `mise install` in the DevPod. The pinned model is downloaded into the existing Docker model volume. Missing dependencies, occupied incompatible ports and failed service/model checks stop with recovery information. The automatic command is for the documented existing ChatMock bridge, not provisioning a new account.

## Fast local chat model

Run `mise run local-llm` on the host with the existing `metis` DevPod. It requires DevPod, OpenSSH and Python on the host, and the repository's mise tools in DevPod. It downloads missing Qwen and MiniLM models into existing volumes, creates the Metis Qwen template, builds/starts services, verifies semantic embeddings, warms the actual chat model and opens persistent loopback forwards on 8000/8501. Downloads and builds can take time on the first start; repeat starts reuse models and build caches.

The default `metis-qwen25:3b` uses Qwen 2.5 3B without a thinking phase, a 16,384-token context and a JSON/citation template in `scripts/Modelfile.local`. The context accommodates the app's 4,096-token output allowance plus retrieved evidence; oversized prompts can still exceed it. Ollama keeps the model loaded for 24 hours. Chat uses `http://llm:11434/v1`, timeout 30 seconds and zero retries by default. Existing `GPTMOCK_*` names configure this OpenAI-compatible local provider.

When AMD `/dev/dri` and `/dev/kfd` are available inside DevPod, `compose.local-llm-gpu.yaml` passes them into Ollama. Startup reports actual GPU memory usage and model context. `METIS_LOCAL_GPU=off mise run local-llm` forces CPU; `METIS_LOCAL_GPU=amd` requires the devices. CPU inference is slower. If GPU bytes remain zero, check device exposure in both DevPod and the Ollama container and inspect `docker compose ... logs llm`. This override does not configure NVIDIA devices.

For manual startup **inside DevPod**:

```bash
bash scripts/local-llm-devpod
```

This performs model provisioning and service checks without host forwarding. `METIS_DEVPOD_DIR` can override the host launcher's default `/workspaces/metis` directory. `LOCAL_LLM_MODEL` selects another installed/downloadable Ollama model; only the default alias receives the Metis template and context configuration. Changing chat models does not require re-indexing.

To inspect the running services inside DevPod, use the same overrides selected at startup:

```bash
mise exec -- docker compose -f compose.yaml -f compose.semantic.yaml -f compose.local-llm.yaml -f compose.local-llm-gpu.yaml ps
mise exec -- docker compose -f compose.yaml -f compose.semantic.yaml -f compose.local-llm.yaml -f compose.local-llm-gpu.yaml logs --since 10m backend worker embeddings llm
mise exec -- docker compose -f compose.yaml -f compose.semantic.yaml -f compose.local-llm.yaml -f compose.local-llm-gpu.yaml exec llm ollama ps
```

Omit the GPU override for CPU mode. `ollama ps` reports the loaded model, processor and context. Host tunnel diagnostics are in ignored `.agent/local/localhost-forward.log`. Re-run the launcher to reconnect after stopping DevPod. An incompatible listener on an application port fails the final health/identity check; the launcher does not terminate that listener.

Small models may miss instructions or citations; successful latency checks do not establish general answer quality. Use the existing live evaluation commands with synthetic evidence before relying on a new model.

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
