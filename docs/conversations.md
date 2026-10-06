# Conversations

Chat lists saved conversations in pages of 20. Reopen restores messages and the
explicit source/group/document scope. Rename and delete use the same access rules:
members access their own conversations; organisation owners/admins can access all
organisation conversations. Chat states this visibility explicitly. New conversation
clears conversational context and filters. The current scope appears near the input.

If an API follow-up omits scope fields, the saved scope is reused. Supplied fields
make an explicit selection; invalid/deleted IDs never silently become all knowledge.
Unavailable selections remain visible in the UI until the user removes them.
A deleted group must be replaced explicitly. Group membership is resolved freshly.

Follow-up retrieval performs one deterministic, bounded rewrite from recent user
questions. Standalone questions remain unchanged. “What about last month?” can derive
the previous month from a month/year explicitly stated in the earlier user question.
The rewrite never imports assistant assertions or modifies authorised scope. If no
context is available, Metis asks which topic/period the question refers to. The
original question is still saved and answered. No LangGraph or additional LLM call
is required for this flow.

Each turn retrieves fresh evidence. Old assistant messages with unavailable citations
are excluded from model history; transcript views redact those answers. The UI
refreshes the authorised transcript on each render. Deleted/superseded evidence and
foreign chunks cannot supply current history or cached responses. User questions
remain their own inputs, not document evidence.

Clients may send a UUID `request_id`. Retrying the same question/scope returns the
saved response without another turn. Reusing an ID with different input returns 409.
PostgreSQL transaction advisory locks serialise concurrent identical requests.
Messages, audit metadata and the response record commit atomically; failures roll
back, so the same request can be retried. Cached responses recheck current conversation
access and citations. The frontend preserves failed questions and the request ID;
editing the retry creates a new request. Changing organisation or signing out clears
pending input.

Each saved assistant answer accepts Helpful / Something is wrong feedback with an
optional reason and comment. Feedback follows conversation access rules, is stored
once per answer/user, and can be updated. Deleting a conversation cascades its
messages, feedback and request records. Normal request logs omit questions/answers.
