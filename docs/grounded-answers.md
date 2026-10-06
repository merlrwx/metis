# Grounded answers

Chat returns one of four answer outcomes: `answered`, `partially_answered`,
`clarification_needed` or `insufficient_evidence`. Provider/network failures remain
HTTP 503 errors rather than becoming missing evidence. The live compatible model
must return validated JSON fields. A local `<think>` preamble is removed before
parsing. Invalid fields or out-of-scope citations are provider response errors.
Legacy plain-text test adapters remain supported.

Answers use only the current retrieved evidence. The prompt asks for both sides of
comparisons and conflicts, specific clarification when period/net/gross/authority
matters, and no invented explanations for differences. External modification time
is recorded as provenance, not a rule for deciding which policy wins. Each shown
citation has the matching marker, source/document, page/section, version and
available source modification date. Original downloads go through authenticated
organisation/document/version access checks; storage keys are never download URLs.

For small arithmetic, the model extracts operands rather than executing code.
The application checks each cited value against a labelled excerpt, its currency
or unit, net/gross meaning and explicit period. Operands must share units and
meaning. Duplicate operands and invalid/ambiguous values request clarification.
Only sum, difference and average are supported, using `Decimal`; no generated
Python or SQL executes. The shown result is replaced by the checked calculation
with cited operands, so model arithmetic and invented causes are not repeated.

Questions requesting totals/counts/all records require complete scoped coverage.
Metis attempts a complete active indexed scan, bounded to 50 documents, 20 chunks
and 12,000 characters. Every active version must have chunks in the configured
model. If completeness cannot be proved, the answer requests a narrower scope
rather than presenting top-K evidence as complete. Complete indexed text still
requires the model to identify the actual records; documents are not records.
Large corpus-wide aggregation is deferred.

`tests/fixtures/answer_quality.json` contains annotated synthetic live cases.
The Phase 16 ChatMock review confirmed policy plus runbook citations, the correct
100.15 AUD difference with two checked operands, both conflicting intervals and an
authority question, a period/net/gross clarification, and rejection of injected
warranty instructions. These checks assess supported claims, not just citation
presence. They do not establish correctness for arbitrary documents or every model.

Run the separate opt-in review with `METIS_LIVE_CHAT_EVAL_ENABLED=true`, an explicit
local `GPTMOCK_BASE_URL` and `mise run evaluate-answers`. It exercises generation
against fixed synthetic excerpts, independently of the ingestion/retrieval evaluator.
Normal CI uses deterministic protocol and operand checks, never a live model.
Review the printed synthetic claims against the fixture text; passing citation
counts and phrase checks alone is not proof that every answer claim is supported.
