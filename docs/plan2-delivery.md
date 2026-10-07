# Plan 2 delivery evidence

Phases 14–20 implement the simple knowledge assistant described in [Plan 2](../plans/plan2.md): automatic semantic discovery, optional scoped search across sources, cited synthesis and safe arithmetic, saved conversations, document management, connected-source controls and repeatable local operation. The bounded Python flow remains; no LangGraph, knowledge graph, reranker, autoscaler or new frontend framework was introduced.

## Measured local release checks — 2026-10-07

| Check | Result |
| --- | --- |
| Unchanged `scripts/verify` in DevPod | 149 backend passed, 11 environment-dependent skipped; 31 frontend passed, 82% frontend coverage |
| Isolated PostgreSQL/Redis15 suite | 160 passed, 91% backend coverage; includes concurrency, tenant/version exclusions and worker recovery |
| Actual ingestion + semantic retrieval, 25 paraphrased questions | Recall@5 100%, Recall@1 92%, median embedding/search 10.3 ms; four unrelated questions rejected |
| Exact identifiers/amounts/phrases, 72 questions | Recall@5 and Recall@1 100%, median embedding/search 12.3 ms |
| Combined-corpus ingestion → retrieval → live ChatMock | 12/12 outcome/claim/arithmetic/citation checks passed; expected-document coverage 100% for answerable cases |
| Synthetic PostgreSQL/original-object restore | Three historical/current/pending versions restored; all original SHA-256 checksums and current-version pointers match; indexed chunks preserved |
| Disposable Redis recovery | One restored pending job reconciled into Redis15 and successfully indexed from its restored original |
| Disposable Kubernetes E2E | Passed with current runtime; worker retry/reclaim/reconcile and source-sync fixture checked; cluster removed afterward |
| Repeatable host `mise run local` | DevPod services, pinned semantic readiness, live ChatMock response and localhost UI/API checked |
| User-requested local payslip replay | Cited amount returned without document selector; tenant isolation passed; no real document text, amounts or tokens committed/logged |

The 12 live cases include two-source incident synthesis, payslip difference/sum/average, exact invoice lookup, conflicting guidance, missing net/gross/period clarification, injection resistance, partial and insufficient evidence, bounded complete-corpus refusal and contextual last-month follow-up. The synthetic answers were reviewed against their supporting excerpts. Arithmetic is computed by the application's `Decimal` allowlist; no generated code runs.

Pipeline retrieval was roughly 11–19 ms and answers took roughly 0.01–7.5 seconds across recorded runs, with provider calls usually taking 2–7 seconds. These are small, warm, sequential fixtures using pinned MiniLM and `gpt-5.6-luna` through the configured live ChatMock bridge. They do not establish universal answer quality, production capacity or another model's behavior. `evaluate-answers` uses preselected evidence; the full `evaluate-pipeline` check instead ingests and retrieves its own combined corpus.

Observed local memory: API 155–157 MiB, worker 118–119 MiB, frontend 56 MiB, embedding runtime 193–705 MiB under its 1 GiB cap, PostgreSQL 48–78 MiB and Redis about 8 MiB. Waiting ingestion jobs and oldest waiting-job age were both zero in the release snapshot. This does not justify adding workers. The embedding artifacts total 87.1 MiB; the initial model download/load measurement was 6.7 seconds in the original selection environment.

## Scope and limitations

The Microsoft 365 source in the pipeline is a fixture using the connector storage path. Real Microsoft 365 library validation was explicitly deferred by the user on 2026-10-07 because no approved library is configured. Do not interpret fixture checks as live connector readiness. Actual ChatMock generation and local embedding inference were checked separately.

Scanned PDFs require searchable export/OCR before upload. Unsupported or oversized extraction fails visibly. Removal/disconnection excludes evidence immediately but retains originals, historical versions and stored answer data under the documented retention policy. Complete totals require a small provably complete scope; a top-K sample never establishes a census. Organisation libraries are shared with members, and restricted Microsoft 365 libraries are unsupported.

See [local operations](local-operations.md) for repeatable checks, bounds and backup/restore; [semantic search](semantic-search.md) for the pinned model and re-index procedure; [document management](document-management.md) and [Microsoft 365](microsoft365.md) for visibility and retention. Kubernetes E2E uses only disposable `metis-cluster`; final GitHub results are available in the repository's Actions runs. No shared-cluster deployment or production validation is claimed.
