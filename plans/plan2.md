# Metis — From working RAG to useful connected knowledge

Status: proposed implementation plan. No phases in this document are implemented by writing this plan.

This continues [plan1.md](plan1.md), whose foundation spans phases 0–13. It preserves the original plan and starts new work at Phase 14. Baseline: commit `0fe5f6a`, including the explicit document selector added after the payslip retrieval failure.

## Product direction

Metis should turn scattered knowledge into useful understanding. The name's association with wisdom should show up in the experience: find the relevant material, connect it, explain the answer, and make uncertainty visible.

A user should be able to ask a natural question without knowing which file or connected source contains the answer. They should also be able to narrow a question to a document, compare several sources, continue a conversation, and inspect the evidence.

“Ask anything about your knowledge” means accepting natural questions and responding appropriately: answer from evidence, explain what is missing, or ask for clarification. It does not promise an answer to information the organisation has never supplied.

Examples that define the next release:

| User question | Expected experience |
| --- | --- |
| How much did I get paid in two weeks? | Find the relevant payslip automatically; identify the period and distinguish net from gross pay. Clarify if several periods could apply. |
| Why was this payment different from the previous one? | Retrieve both payslips, compare cited amounts, and separate an observed difference from an explanation the evidence does not establish. |
| What do the policy and the runbook say I should do? | Combine relevant passages from an uploaded policy and a connected library, citing both. |
| Which procedure is current? | Use explicit version/effective-date evidence; show conflicts when authority is unclear. |
| What about last month? | Resolve the follow-up using the conversation and retrieve the relevant earlier material. |
| How many documents mention this supplier? | Distinguish a complete scoped query from a sample of retrieved excerpts. |

## What exists and what remains weak

- Uploads, extraction, background ingestion, PostgreSQL/pgvector, tenant filtering, chat, citations, persisted messages and a Streamlit interface exist.
- ChatMock supplies working chat completions. The observed local `/v1/embeddings` endpoint returns 404; it cannot currently supply semantic embeddings.
- Local Compose uses deterministic hashing embeddings. The semantic-provider interface exists, but local semantic retrieval is not configured or validated.
- The payslip failure was a retrieval failure: text was indexed, but the best score was about 0.106 against a 0.2 cutoff. Choosing a document explicitly now bypasses that cutoff for its retrieved chunks.
- Retrieval currently searches current-version chunks with a fixed top-K. A large document can dominate the results. Selecting a document does not guarantee that every part of it is read.
- Conversation history reaches the answering model, but retrieval embeds only the latest message. A follow-up can therefore retrieve the wrong evidence before the model sees its context.
- Citation validation checks supplied citation IDs. It does not prove that every claim is supported or that arithmetic is correct.
- Microsoft 365 synchronization exists and has fixture/integration coverage. Live tenant consent, permissions and downloads still require validation with configured credentials. Individual SharePoint ACLs are not mapped.
- Existing synthetic performance measurements are useful baselines, not evidence of real-world answer quality or capacity.

## KISS decisions

1. Keep FastAPI, Streamlit, PostgreSQL with pgvector, Taskiq/Redis, the existing storage abstraction, and the current DevPod/mise workflow.
2. Keep a straightforward retrieve → assemble evidence → answer flow. Introduce ordinary functions at existing service boundaries before adding orchestration machinery.
3. Use one semantic embedding model for the initial deployment. Hashing stays available for deterministic tests and explicitly labelled development mode.
4. Use PostgreSQL's native text search if semantic search alone misses exact names, identifiers or phrases. Keep both retrieval methods in the same database.
5. Connect sources through shared retrieval and provenance. A graph database, automatic entity graph and agents are not prerequisites for combining documents.
6. Deliver one useful vertical slice per phase. A new dependency needs a measured failure or a required capability that the existing stack cannot supply simply.
7. Preserve tenant boundaries in every search, download, background job, conversation and new grouping. Grouping sources never grants access to them.

## Delivery order

| Phase | Outcome | Priority | Depends on |
| --- | --- | --- | --- |
| 14 | Natural questions find the right document | Essential; first | Existing ingestion/search |
| 15 | Retrieval finds precise evidence across sources | Essential | 14 |
| 16 | Answers compare and combine evidence reliably | Essential | 15 |
| 17 | Conversations remain useful across turns and visits | Essential | 16 |
| 18 | Users understand and manage their uploaded knowledge | Next | 14; can follow 17 |
| 19 | Connected libraries are usable and stay trustworthy | Next | 15, 18 |
| 20 | Quality, recovery and operating limits are visible | Release gate | Checks accumulate from 14 onward |

Phases 14–17 form the first user-facing release. Phase 20's relevant checks run throughout; it is not a final testing phase. Phases 18–19 complete the broader knowledge-management experience.

## Phase 14 — Semantic retrieval that works locally

**User outcome:** ask about an uploaded document without selecting its name first.

### Implementation

- Extend the existing evaluation fixtures before changing retrieval. Use synthetic payslips, policies, runbooks and supplier documents, with paraphrased questions and expected document/chunk IDs.
- Evaluate at most two suitable embedding options against those fixtures and available DevPod resources. Prefer a small locally served semantic model for local development; allow the existing compatible HTTP provider for a separately configured service. Record model licence, pinned revision, dimensions, resource use and retrieval results before choosing one.
- Keep the chat provider and embedding provider independently configured. Add a startup/preflight check that makes a synthetic embedding request, validates dimensions and reports actionable configuration failures.
- Replace hardcoded Compose hashing settings with explicit provider configuration shared by API and worker. Supply a documented semantic development setup and keep normal CI deterministic.
- Resolve the current `VECTOR(1536)` constraint deliberately. If the selected model has another output size, migrate the schema and dimension validation together. Do not pad or truncate vectors merely to satisfy the existing column.
- Add an explicit re-index operation for existing documents, using stored originals and the current worker. Track model identity and indexing progress; never compare vectors from different embedding spaces.
- For this single-workspace MVP, use a clear re-index maintenance state: pause new ingestion for the affected corpus, retain originals, rebuild resumably, and enable search only when all active documents use the selected model. Back up the index first and document recovery. Zero-downtime dual indexes can wait for a deployment that needs them.
- Calibrate relevance decisions against answerable and unanswerable examples. The existing 0.2 hashing cutoff is not a universal semantic threshold. Provider failures must not silently fall back to hashing or appear as “no evidence.”

### Acceptance

- The synthetic payslip question finds the correct document in the top five without `document_id`, despite different wording.
- At least 90% document Recall@5 on a fixed initial set of at least 20 answerable questions; report misses individually. This is a proposed release target, not a measured result.
- Unrelated questions and wrong-tenant searches remain negative cases.
- Model/dimension mismatch is caught before accepting new incompatible chunks.
- Interrupting and resuming re-indexing preserves original files and ends with a consistent searchable corpus.
- Document the measured cold-start cost, download size, query latency and local setup commands. Demonstrate upload → semantic retrieval → live ChatMock answer in DevPod.

**Likely code:** `embeddings.py`, `models.py`/migrations if needed, `tasks.py`, `services/jobs.py`, Compose/mise configuration and retrieval fixtures.

## Phase 15 — Precise retrieval across multiple sources

**User outcome:** Metis finds evidence wherever it lives, while optional filters let the user narrow the scope.

### Implementation

- Keep “All indexed knowledge” as the default. Add optional source/document multi-selection with one consistent scope object shared by search and chat. Preserve the existing single-document API during migration.
- Apply organisation, visibility, active version, model and selected-scope filters before ranking. Validate every supplied ID; do not silently widen a scope containing invalid or inaccessible IDs.
- Measure exact-name, code, amount and phrase failures. Where justified, add PostgreSQL full-text search over chunk text and document titles, then merge semantic and keyword rankings using a small reciprocal-rank-fusion function. Rank fusion combines positions rather than pretending the two score scales are equivalent.
- Inspect tokenisation of identifiers and amounts in the fixtures; use narrowly scoped exact matching for cases the text-search parser does not preserve. Avoid general fuzzy-search machinery initially.
- Retrieve a bounded candidate set, remove overlapping duplicates, and prefer useful coverage across documents. Diversity must not force irrelevant sources into an answer.
- Expand selected hits with a small amount of neighbouring context when headings, labels or table values would otherwise be separated. Assemble a bounded evidence budget rather than passing an unbounded corpus to the model.
- Add simple saved knowledge groups, such as “Payroll” or “Operations,” only as a saved scope over existing sources. Use small relational tables for a group and its source memberships; keep them organisation-scoped. Groups organise knowledge and do not form a new permission system.
- Preserve source name, document/version identity, page/section and available dates throughout retrieval.

### Acceptance

- One query can retrieve useful passages from both an upload and a Microsoft 365 fixture source in the same organisation.
- Exact identifiers remain discoverable alongside paraphrased questions.
- Selecting two sources searches both and excludes all others; a saved group produces the same scope.
- Repeated overlapping chunks cannot consume the entire context budget in the cross-document fixture.
- Foreign IDs, deleted documents and superseded versions cannot enter any retrieval branch or neighbour expansion.

**KISS boundary:** start with semantic search from Phase 14; keep the keyword branch only when its evaluation benefit is demonstrated. No separate search service or reranker yet.

## Phase 16 — Answers that connect, compare and explain

**User outcome:** a useful synthesis with evidence, including when sources disagree or only partly answer the question.

### Implementation

- Give the answer service an evidence bundle containing bounded excerpts and provenance. Extend current response schemas where needed; do not create a generic agent framework.
- Support four clear outcomes: answered, partially answered, clarification needed, and insufficient evidence. Represent service/provider failure separately from those answer outcomes.
- Ask for clarification when period, entity, document authority or question scope materially changes the answer. For a payslip, distinguish net/gross and one pay period versus several.
- Prompt for a direct answer, cited supporting facts and relevant qualifications. Show disagreement with citations to both passages. Use explicit effective dates or a recorded supersedes relationship when available; file modification time alone does not establish authority.
- Improve citation rendering: document and source name, page/section, version/date where available, and the supporting excerpt. Cite each side of a comparison. Use authorised document access for uploaded originals rather than exposing storage keys.
- Validate citation membership and required response fields in code. Test claim support with annotated examples and human review of live evaluations; citation presence alone is not an answer-quality score.
- For small arithmetic questions, extract the cited operands, units, periods and gross/net meaning; calculate with Python `Decimal`; show the calculation and its sources. Support a small allowlist of arithmetic operations, never generated Python or SQL execution. If operands are ambiguous or absent, clarify or abstain.
- Separate “find relevant passages” from “cover every record.” Top-K excerpts cannot establish a complete total, count or exhaustive list. Use a bounded complete scoped scan or structured query when completeness is provable; otherwise explain the coverage limit and request a narrower scope. Defer large corpus-wide summarisation jobs until there is a measured need.

### Acceptance

- A policy/runbook answer uses evidence from both documents when both are needed.
- A two-payslip comparison gives the correct synthetic difference, shows both cited operands and does not invent a reason for the difference.
- Conflicting procedure fixtures produce a clear disagreement, not an unsupported choice of the “newest” rule.
- Missing-period and net/gross ambiguity fixtures request useful clarification.
- A query asking for all records never presents a top-K sample as complete.
- Instructions embedded inside uploaded documents cannot change retrieval scope or authorise actions; adversarial fixtures exercise the prompt boundary.

**Likely code:** `services/rag.py`, `services/knowledge.py`, `schemas.py`, chat endpoints, citation components and evaluation fixtures.

## Phase 17 — Conversations users can return to

**User outcome:** follow-ups work, previous conversations can be reopened, and search scope stays understandable.

### Implementation

- Add paginated conversation listing and reopen/rename/delete controls using existing persisted conversations and messages. Apply the current ownership rules consistently and explain organisation-admin visibility in the product.
- Persist the chosen knowledge scope with the conversation; display it near the input. Scope changes must be explicit, and starting a new conversation clears previous conversational context.
- Resolve context-dependent questions before retrieval. Use the original question directly for standalone queries. For ambiguous follow-ups, make at most one bounded rewrite using recent user messages and verified conversational context; retain the original question for answering and audit metadata.
- Treat a rewrite as a search suggestion. It cannot alter authorised scope, invent dates or entities, or convert a previous assistant assertion into source evidence.
- If the reference is still ambiguous, ask a clarification instead of repeatedly rewriting and searching. Fetch fresh authorised evidence for each turn; never assume old citations are still available.
- Distinguish “Searching knowledge,” “Preparing answer,” missing evidence, unreadable files and provider errors. Preserve typed input when a request fails, and make retry safe against duplicate saved turns.
- Add a small “Helpful / Something is wrong” action with reasons such as wrong source, missing information or incorrect answer. Store only useful identifiers and optional user-supplied feedback, with normal tenant access controls.

### Acceptance

- “What about last month?” retrieves the earlier payslip after an initial pay-period question.
- Reopening a conversation restores its messages and explicit scope.
- Changing organisation, revoking access or deleting a document cannot reuse now-inaccessible evidence through history or citations.
- A model/network failure preserves the question, displays an actionable error and does not create duplicate turns on retry.

**KISS boundary:** ordinary Python control flow and one optional rewrite call. LangGraph is not needed for this bounded flow.

## Phase 18 — Documents people can understand and manage

**User outcome:** users know what Metis read, whether it is ready, and how to replace or remove it.

### Implementation

- Add a document detail view: processing status, extraction preview, page/chunk counts, source, version and last successful indexing time. “Ready” means searchable; it does not guarantee every question can be answered.
- Show upload visibility explicitly: the current organisation library is shared with its members. Personal or restricted documents require a separate authorised scope; do not imply that uploading a payslip makes it private to its uploader.
- Support a small batch of uploads with individual progress and retry results. Detect unchanged content and distinguish replacement of an existing document from an unrelated file with the same name.
- Add explicit replace, rename, re-index and delete operations. Make deletion immediately exclude the document from retrieval. Define the separate policy for purging original files, historical versions and saved answer excerpts; retained history must not contradict a promised deletion.
- Preserve useful table headings, rows, page boundaries and number formatting. Test realistic synthetic payslips, invoices and policy tables before changing the existing extractor/chunker.
- Detect scanned or nearly empty PDFs and report “Text could not be read” with a recovery action. Add one optional OCR path only if scanned-document fixtures demonstrate the need, with resource limits and extraction provenance.
- Add CSV ingestion as the first new format if structured-table questions are part of the next release. Bound rows/cells/file size, preserve headers and provenance, and support complete scoped calculations explicitly. XLSX, images and arbitrary formats stay demand-driven.
- Link citations to an authorised original download and relevant page/section when possible. Recheck access on every download and preview.

### Acceptance

- A scanned PDF is never labelled successfully searchable with empty extracted text.
- Table fixtures preserve the relationship between amount, label and pay period.
- Replacing a document removes the old version from default retrieval while retaining clearly labelled history according to policy.
- Deleting a document removes it from new answers immediately; the UI accurately describes retained and purged data.
- Original downloads, previews, re-index and batch actions have negative cross-tenant tests.

## Phase 19 — Connected sources that stay trustworthy

**User outcome:** an organisation can connect approved knowledge, see its freshness and combine it with uploads.

### Implementation

- Finish the Microsoft 365 operator-to-user setup path: clear source identity, connection check, manual sync, periodic-sync toggle, last successful sync and actionable failure state. Credentials remain server-side.
- Validate the current connector against one approved real library before calling it production-ready. Exercise additions, edits, renames, deletions, expired cursors and interrupted sync. Fixture success must remain distinct from live validation.
- Keep the initial permission model explicit: a connected library must be approved for all members of its Metis organisation. Mark this at setup. Mixed-permission libraries remain unsupported until document-level permission mapping and revocation are implemented and tested across search, chat, history and downloads.
- Provide pause/disconnect actions that define whether indexed content is retained or removed. Show stale or failed-sync state beside source evidence so users do not mistake it for confirmed current information.
- Ensure source removal updates saved knowledge groups without silently widening their search scope. A group whose sources disappeared should become empty with an explanation.
- Add the next connector only for a concrete user need. If websites are next, start with explicit operator-approved URLs, bounded downloads, redirects and refresh rules; protect against private-network fetches. Avoid an unrestricted crawler.

### Acceptance

- A single answer can combine an upload with a live approved library document and expose the origin and freshness of each.
- A source deletion, pause or disconnect has predictable effects on retrieval and saved scopes.
- A failed refresh is visible and recoverable without credentials, signed URLs or document contents appearing in logs.
- Connection tests and sync requests cannot target another organisation's source.

## Phase 20 — Measured release quality and recovery

**User outcome:** the simple system stays dependable as real knowledge and usage grow.

### Evaluation from the first phase

- Expand the existing evaluation script into a small repeatable corpus test that runs ingestion and retrieval before answer generation. The current script supplies preselected hits, so it cannot detect the payslip retrieval failure by itself.
- Include paraphrases, exact identifiers, multiple sources, ambiguity, conflicts, arithmetic, follow-ups, missing evidence, scanned text, old versions, permission changes and document prompt injection.
- Separate retrieval metrics from answer metrics: document/chunk Recall@K, evidence coverage for multi-document cases, supported claims, clarification/abstention behaviour, arithmetic accuracy and latency.
- Keep ordinary CI deterministic with fake chat responses and unit/integration security checks. Provide a separate reproducible semantic-model evaluation using pinned local artifacts; report missing model artifacts explicitly. Live ChatMock evaluation remains opt-in and uses synthetic documents.
- Gate each phase on its new failure cases plus existing tenant-isolation tests. All fixed arithmetic and access-control cases must pass; manually review the small live answer set. Record the model/configuration with results rather than claiming universal quality from a small sample.

### Operations and local use

- Add one repeatable `mise` preflight/start workflow that checks DevPod services, embedding readiness, ChatMock reachability and port availability, then reports the local URLs. Keep credentials outside Git and avoid changing shared-cluster resources automatically.
- Use existing metrics to distinguish extraction failures, retrieval with no candidates, rejected evidence, provider failures and answer latency. Record counts and identifiers, not raw documents or prompts in operational logs.
- Document and test a PostgreSQL/object-storage backup and restore using an isolated environment. Restored metadata and originals must agree; disposable Redis recovery should use existing job reconciliation.
- Add bounded request sizes, model timeouts and per-user/organisation request limits appropriate to actual deployment. Measure resource use and queue age before increasing worker count.
- Pin the chosen embedding runtime/model and record upgrades with a re-index and evaluation procedure. Update stale setup documentation as part of each delivered change.

### Release acceptance

- From a fresh documented DevPod setup, a user can upload, ask naturally, combine sources, reopen a conversation and verify citations without editing application code.
- Run the unchanged `scripts/verify`, applicable isolated PostgreSQL/Redis checks and Kubernetes E2E when runtime/manifests change. Do not point destructive fixtures at the user's local application database.
- Required GitHub checks pass on the final pushed commit. Report live-provider and live-connector checks separately from mocks.
- Record measured latency, retrieval quality, answer limitations and recovery evidence. No release claim depends only on a mocked or hand-selected retrieval result.

## Deliberately deferred work and its trigger

| Improvement | Add only when |
| --- | --- |
| Reranking model | Semantic + keyword retrieval still misses relevant passages in the recorded evaluation, and measured benefit justifies latency/resource cost. |
| Extra retrieval/rewrite loop | A bounded retry demonstrably resolves recurring failures; cap attempts, time and model calls. |
| LangGraph | A shipped workflow needs resumable branching, approvals or several stateful tools, and a graph simplifies the actual code. |
| Knowledge graph / entity extraction | Users need repeated relationship traversal that shared retrieval and explicit links cannot answer reliably. |
| HNSW or another approximate index | Representative exact-search latency fails a recorded target, with acceptable recall verified under tenant filters. |
| Caching | Repeated-query measurements show useful hits and a correct tenant/version/model invalidation strategy. |
| Worker autoscaling or database distribution | Queue/backlog/resource measurements identify an actual bottleneck. |
| New frontend framework | A concrete interaction cannot be delivered acceptably with the current Streamlit UI. |
| Answer streaming | Measured waiting time warrants it and the UI can distinguish provisional text from validated citations. |
| More connectors, SSO, fine-grained ACLs | A specific organisation needs them; establish permission and revocation semantics before connecting restricted data. |

## Implementation discipline

- Start with Phase 14's failing natural-language retrieval cases. The document selector remains a useful scope control, not the success criterion for automatic discovery.
- Keep changes inside the existing modules where possible: `embeddings.py` for vectors, `services/knowledge.py` for scoped retrieval, `services/rag.py` for grounded answers, and existing worker/storage services for ingestion.
- Introduce schema changes only alongside the feature that uses them, with a migration, existing-data handling and recovery steps. Do not prebuild schemas for every later phase.
- Use synthetic payslips and internal-looking documents in committed fixtures. Never commit uploaded user documents, credentials or transcripts as test data.
- At each implemented phase, inspect the diff, run relevant checks in DevPod, update its delivery evidence and commit/push separately using Conventional Commits under the existing authorised workflow.
- This document is planning only. It does not start an autonomous goal, deploy services, connect new accounts or authorise production changes.

## References

- [Existing implementation plan](plan1.md), [retrieval baseline](../docs/performance.md), [Microsoft 365 limits](../docs/microsoft365.md), and [orchestration decision](../docs/orchestration.md).
- PostgreSQL provides parsing, normalisation and ranked text retrieval in the database: [full-text search introduction](https://www.postgresql.org/docs/17/textsearch-intro.html).
- pgvector documents exact/approximate retrieval and combining vector search with PostgreSQL full-text search: [pgvector documentation](https://github.com/pgvector/pgvector).
- LangGraph provides orchestration for stateful workflows; adoption remains conditional on the workflow we actually need: [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview).
