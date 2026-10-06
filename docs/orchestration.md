# Orchestration scope

Phase 13 keeps the MVP's existing Python services. No LangGraph dependency is added.

The implemented chat path in `src/backend/src/backend/main.py` authenticates the caller, checks organisation membership and conversation ownership, embeds the query, retrieves current tenant-owned chunks, and calls `services/rag.py` to generate one grounded answer. That service filters weak evidence, builds the prompt, invokes the configured provider and validates citation markers. With no evidence it returns the existing abstention response. The API persists the conversation and messages.

This is a linear retrieval → prompt → model pipeline with a bounded validation gate. It does not classify requests into separate agents, rewrite and retry questions, invoke business actions or pause for human approval. Microsoft 365 synchronization is deterministic ingestion with changed/deleted-file handling; it does not introduce an LLM-driven decision graph. Taskiq already handles background jobs, retries and recovery.

Revisit graph orchestration when an implemented feature needs persistent branching state: routing between collections and business tools, evidence grading followed by query rewriting, approval before an action, or execution followed by verification. First specify the graph's state, bounded retries, tenant-scoped tools, authorization at every action and resume behavior. Add LangGraph only if it makes that concrete workflow simpler than the existing service interfaces.

Existing seams support that future work: `ChatProvider` supplies model completions, `knowledge.search_chunks` scopes retrieval, `rag.answer_question` supplies grounded-answer behavior, and `KnowledgeSource` supplies ingestion adapters. No extra abstraction is needed for the current product.
