# Knowledge scopes

Search and chat default to all active indexed knowledge in the current organisation.
Optional `source_ids` and `document_ids` each accept up to 20 IDs. Combining them
uses their intersection. Existing `source_id` and `document_id` remain supported;
a single ID conflicting with a multi-selection returns 422. Every supplied ID
must belong to the organisation, and documents must not be deleted. Invalid IDs
return 404 before an embedding request; Metis never silently drops them.

A saved group is a name and a selection of existing sources, not a permission.
Members can use groups; owners and administrators manage them in Knowledge.
`group_id` replaces source selection and may be narrowed by document selection.
An empty group returns no evidence, including after its last source is removed.
Group/source memberships use composite organisation foreign keys.

Retrieval filters organisation, current document version, deletion state, embedding
model and scope before ranking. It considers at most 120 candidates, prefers one
relevant passage per document, and removes overlapping selected chunks. Chat may
add immediately neighboring chunks from the same authorised version/model, with
their own citation identities. Evidence is bounded to 20 passages and 12,000
characters. Search preserves its requested result limit. Source names, document
version IDs, page/section and available external modification dates remain in
results and citations; modification time does not establish authority.

The initial semantic evaluation reaches 100% Recall@5 over 25 questions and
92% Recall@1. A broader check found three misses among 30 exact supplier codes
(90% Recall@5); 30 amounts, six names and six quoted phrases passed Recall@5.
That measured gap justified a small PostgreSQL full-text branch. It uses English
text search over titles and content, plus bounded exact matching for codes,
decimal amounts and quoted phrases whose boundaries matter. Both branches share
the same scope predicates. Reciprocal-rank fusion combines ranked positions;
original cosine scores remain cosine scores. An exact literal match can provide
evidence independently of the semantic cutoff. No separate search service,
fuzzy matching, index tuning or reranker is added.

The opt-in actual-ingestion evaluation accepts an alternate fixture:
`METIS_RETRIEVAL_FIXTURE=src/backend/tests/fixtures/exact_retrieval.json` with
`mise run evaluate-retrieval` and the documented isolated database/semantic provider
settings. With hybrid retrieval, all 72 exact-match fixtures placed the expected
document first; all four negative questions remained below the evidence gate.
