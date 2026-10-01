FOR REPO STRUCTURE COPY:
https://github.com/merlrwx/devops-app

Location: /home/jason/jd/20-29-code/repos/public/devops-app


FOR LLM:
Use **LangChain’s `ChatOpenAI` with GPTMock’s base URL**, then call it inside a LangGraph node. [LangChain docs (https://docs.langchain.com/oss/python/integrations/chat/openai)](<https://docs.langchain.com/oss/python/integrations/chat/openai>), [LangGraph quickstart (https://docs.langchain.com/oss/python/langgraph/quickstart)](<https://docs.langchain.com/oss/python/langgraph/quickstart>).

**1\. Reach the existing bridge from your workstation**

Keep this running in another terminal:

```
kubectl -n hermes port-forward svc/chatmock 8000:8000
```

**2\. Install the packages**

```
python -m venv .venv
source .venv/bin/activate
pip install langchain-openai langgraph
```

**3\. Save as `main.py`**

```
import os

from langchain_openai import ChatOpenAI
from langgraph.graph import START, END, MessagesState, StateGraph

llm = ChatOpenAI(
    base_url=os.getenv("GPTMOCK_BASE_URL", "http://127.0.0.1:8000/v1"),
    api_key="chatmock",  # Placeholder; the deployed bridge owns the session.
    model=os.getenv("GPTMOCK_MODEL", "gpt-6-luna"),
    use_responses_api=False,
    timeout=120,
    max_retries=1,
)


def answer(state: MessagesState):
    response = llm.invoke(state["messages"])
    return {"messages": [response]}


builder = StateGraph(MessagesState)
builder.add_node("answer", answer)
builder.add_edge(START, "answer")
builder.add_edge("answer", END)
graph = builder.compile()

result = graph.invoke({
    "messages": [
        ("system", "You are a concise homelab operations assistant."),
        ("human", "What should I check first for a CrashLoopBackOff pod?"),
    ]
})

print(result["messages"][-1].content)
```

**4\. Run**

```
python main.py
```

For an app running **inside Kubernetes**, set:

```
export GPTMOCK_BASE_URL=http://chatmock.hermes.svc.cluster.local:8000/v1
```

For **LangChain alone**, use the same `llm` object directly:

```
print(llm.invoke("Explain Kubernetes readiness probes briefly.").content)
```

The deployed bridge recently passed a completion check for `gpt-6-luna`, but another check timed out. This example includes a timeout and one retry; it hasn’t been executed against the bridge.






# Generic Knowledge SaaS — Implementation Plan

## 1. Project Goal

Build a reusable multi-tenant SaaS application that allows organisations to connect or upload internal knowledge such as:

- policies
- procedures
- runbooks
- manuals
- technical documentation
- knowledge-base articles
- websites
- Markdown/text files
- PDFs
- DOCX files
- eventually SharePoint, OneDrive, Google Drive, Confluence, etc.

Users can then ask questions against their organisation's knowledge and receive grounded answers with citations back to the original source.

The project should reuse the existing `devops-app` delivery platform rather than redesigning its CI/CD system.

The existing repository already provides:

- independent Python projects managed with `uv`
- FastAPI backend
- Streamlit frontend
- Docker multi-stage builds
- GitHub Actions
- Ruff/pre-commit
- pytest and coverage
- Trivy scanning
- k3d E2E environments
- GHCR image publishing
- Release Please
- separate GitOps repository
- Flux reconciliation
- dev/prod overlays

These mechanics should largely remain intact.

---

# 2. Core Architectural Principles

This project should apply system-design principles where they solve an actual problem rather than adding components simply for the sake of architecture.

The System Design Primer specifically identifies asynchronous processing, message/task queues, back pressure, horizontal scaling, caching, load balancing, relational databases and service boundaries as concepts worth applying based on system requirements.

For this project:

**Synchronous work**

```text
HTTP request
    ↓
FastAPI
    ↓
authentication / authorisation
    ↓
retrieve knowledge
    ↓
LLM
    ↓
response
```

Suitable for operations expected to finish within several seconds.

**Asynchronous work**

```text
Upload document
    ↓
FastAPI
    ↓
store document + create ingestion job
    ↓
queue
    ↓
return immediately

          background

queue
    ↓
worker
    ↓
extract
    ↓
chunk
    ↓
embed
    ↓
index
```

Document ingestion is asynchronous because PDF parsing, chunking, external API calls and embedding generation may take seconds or minutes. The user should not hold an HTTP connection open waiting for this work.

This follows the producer → queue → worker model described in the System Design Primer.

---

# 3. Initial System Architecture

```text
                              Internet
                                 │
                                 ▼
                         Kubernetes Ingress
                                 │
                  ┌──────────────┴──────────────┐
                  │                             │
                  ▼                             ▼
             Frontend                       FastAPI
             Streamlit                         │
                                               │
                         ┌─────────────────────┼────────────────────┐
                         │                     │                    │
                         ▼                     ▼                    ▼
                    PostgreSQL              Redis             Object Storage
                    + pgvector             Streams             S3-compatible
                         │                     │
                         │                     ▼
                         │               Ingestion Worker
                         │                     │
                         │              ┌──────┴──────┐
                         │              │             │
                         │              ▼             ▼
                         │         document       embedding
                         │          parser          model
                         │              │             │
                         └──────────────┴─────────────┘
```

The initial Kubernetes workloads should be:

```text
frontend Deployment
api Deployment
worker Deployment
Redis
PostgreSQL + pgvector
```

The **API and worker should use the same backend container image**.

Only their Kubernetes commands differ.

For example:

```text
knowledge-api image
├── API deployment
│   └── uvicorn backend.main:app
│
└── Worker deployment
    └── taskiq worker backend.jobs.broker:broker
```

Do not create a separate worker repository or Docker image.

---

# 4. Technology Decisions

## Backend

Use:

```text
Python
FastAPI
SQLAlchemy
Alembic
Pydantic
LangChain
Taskiq
```

Do not add LangGraph initially.

The first version is conventional two-step RAG:

```text
question
   ↓
retrieve
   ↓
generate answer
```

LangGraph becomes relevant later when retrieval becomes an actual workflow involving multiple decisions and tools.

---

## Database

Use:

**PostgreSQL + pgvector**

PostgreSQL should remain the system of record for:

- organisations
- users
- memberships
- sources
- documents
- document versions
- ingestion jobs
- chunks
- conversations
- messages
- permissions

pgvector stores embeddings alongside application data.

pgvector supports exact vector search and approximate indexes such as HNSW and IVFFlat. It also specifically discusses considerations around filtered/multi-tenant vector retrieval.

Do not introduce a separate vector database initially.

---

# 5. Queue Decision

## Use Taskiq + Redis Streams

Use:

```text
FastAPI producer
      │
      ▼
Redis Streams
      │
      ▼
Taskiq workers
```

Taskiq is an async-native Python distributed task queue and supports async functions and FastAPI integrations. Its Redis Stream broker supports message acknowledgement, unlike its basic Pub/Sub and list queue implementations.

This fits the existing FastAPI/async Python stack without bringing in the larger Celery ecosystem.

### Why not ARQ?

ARQ would technically fit this architecture and you already have experience with it, but its repository currently describes the project as being in maintenance-only mode. It therefore should not become a dependency of a new reusable SaaS template.

### Why not RabbitMQ yet?

RabbitMQ would be reasonable if messaging became mission-critical or messaging topology became complicated.

It is unnecessary for the first version.

### Why not Kafka?

There is currently no requirement for:

- event replay
- event sourcing
- huge throughput
- multiple independent event consumers
- long-lived event streams

Kafka would be solving a problem the application does not currently have.

---

# 6. Queue Reliability Design

Redis should **not** be considered the authoritative record that ingestion needs to happen.

PostgreSQL should be.

Example:

```text
POST /documents
      │
      ├── store document
      │
      ├── INSERT ingestion_job
      │        status = pending
      │
      ├── publish job_id → Redis
      │
      └── return 202 Accepted
```

The queue message should contain something small:

```json
{
  "job_id": "uuid"
}
```

Do not put the PDF or complete document contents in Redis.

The worker then does:

```text
job_id
   ↓
load job from PostgreSQL
   ↓
status = processing
   ↓
load document
   ↓
parse/chunk/embed
   ↓
commit document chunks
   ↓
status = completed
```

Possible job states:

```text
pending
queued
processing
completed
failed
```

This means PostgreSQL can answer:

```text
Which documents are processing?
Which jobs failed?
How many attempts were made?
When did processing start?
What error occurred?
```

without relying on Redis internals.

---

# 7. Idempotency

Workers must assume a task can execute more than once.

That is normal distributed-system behaviour.

Every ingestion job should therefore be idempotent.

Use something such as:

```text
organisation_id
document_id
document_version
checksum
```

to determine whether a particular document version has already been indexed.

A retry should never produce:

```text
PDF
 → 100 chunks

retry
 → another 100 identical chunks

retry
 → another 100 identical chunks
```

Instead:

```text
document version
      ↓
delete/replace previous chunk set transactionally
      ↓
write new chunks
```

or use a unique identifier based on:

```text
document_version_id + chunk_index
```

---

# 8. Recovery From Queue Failures

There is one classic failure window:

```text
INSERT ingestion_job
        ↓
PostgreSQL COMMIT succeeds
        ↓
Redis publish fails
```

The document exists but nobody processes it.

Do not implement a full transactional-outbox architecture in the MVP.

Instead create a simple reconciliation process.

For example, every few minutes:

```text
Kubernetes CronJob
       ↓
SELECT ingestion_jobs
WHERE status = 'pending'
AND created_at < now() - interval '2 minutes'
       ↓
republish missing work
```

This provides an understandable introduction to distributed-system recovery without creating another subsystem.

A transactional outbox can be implemented later if reliability requirements justify it.

---

# 9. Back Pressure

The System Design Primer calls out back pressure because queues can grow faster than workers can consume them.

Expose metrics such as:

```text
queue_depth
oldest_job_age
jobs_processing
jobs_completed_total
jobs_failed_total
job_duration_seconds
```

Do not initially autoscale workers based on queue depth.

Start with:

```yaml
replicas: 1
```

Then deliberately test:

```text
1 worker
10 documents
100 documents
500 documents
```

Observe queue growth.

Later:

```text
worker replicas: 1 → 2 → 4
```

and measure throughput.

That is much more educational than configuring autoscaling before knowing the workload.

---

# 10. File/Object Storage

Do not store uploaded PDFs directly on a Kubernetes pod filesystem.

API pods and worker pods should remain stateless.

Introduce a storage interface:

```python
class ObjectStore:
    async def put(...):
        ...

    async def get(...):
        ...

    async def delete(...):
        ...
```

Initially support:

```text
LocalObjectStore
S3ObjectStore
```

Local development may use the filesystem.

k3d E2E can optionally use MinIO.

Production can eventually use:

- AWS S3
- Cloudflare R2
- another S3-compatible service

The database stores metadata and object keys, not the raw file itself.

---

# 11. Initial Database Model

Start approximately with:

```text
organisations
-------------
id
name
created_at


users
-----
id
email
name


organisation_memberships
------------------------
organisation_id
user_id
role


sources
-------
id
organisation_id
type
name
configuration
created_at


documents
---------
id
organisation_id
source_id
title
source_uri
current_version_id
created_at
updated_at


document_versions
-----------------
id
document_id
checksum
object_key
mime_type
size_bytes
created_at


ingestion_jobs
--------------
id
organisation_id
document_version_id
status
attempts
error
created_at
started_at
completed_at


chunks
------
id
organisation_id
document_version_id
chunk_index
content
page
section
metadata
embedding


conversations
-------------
id
organisation_id
user_id
title
created_at


messages
--------
id
conversation_id
role
content
created_at
```

Every tenant-owned row should contain or be traceably associated with an `organisation_id`.

---

# 12. Multi-Tenancy

Multi-tenancy belongs in version one even if there is initially only one tenant.

Never perform:

```sql
SELECT *
FROM chunks
ORDER BY embedding <=> :query
LIMIT 10;
```

Retrieval must always be scoped:

```sql
SELECT *
FROM chunks
WHERE organisation_id = :organisation_id
ORDER BY embedding <=> :query
LIMIT 10;
```

Eventually add collection/document-level permissions:

```text
Organisation
│
├── General
│    └── Everyone
│
├── IT
│    └── IT users
│
└── HR
     └── HR users
```

Permission filtering must occur during retrieval, not after the LLM sees the documents.

---

# 13. Initial RAG Architecture

Keep the first implementation deliberately simple.

```text
User Question
      │
      ▼
Create query embedding
      │
      ▼
PostgreSQL / pgvector
      │
 tenant + permission filters
      │
      ▼
Top K chunks
      │
      ▼
Prompt
 ├── question
 └── evidence
      │
      ▼
LLM
      │
      ▼
Answer + citations
```

Store enough metadata per chunk to generate useful citations:

```text
document title
page
section
source URL
document version
```

Example answer:

```text
Employees must notify their supervisor as soon as practical
after becoming aware of the incident.

Sources:
Medication Management Policy — p. 14, §7.2
Incident Reporting Procedure — p. 4, §3.1
```

If retrieval produces insufficient evidence, the application should say so rather than invent an answer.

---

# 14. Repository Structure

Evolve the current repository toward:

```text
.
├── .devcontainer/
├── .github/
│   └── workflows/
│
├── src/
│   ├── backend/
│   │   ├── backend/
│   │   │   ├── api/
│   │   │   ├── auth/
│   │   │   ├── database/
│   │   │   ├── documents/
│   │   │   ├── ingestion/
│   │   │   ├── jobs/
│   │   │   ├── retrieval/
│   │   │   ├── llm/
│   │   │   ├── storage/
│   │   │   └── sources/
│   │   │
│   │   ├── migrations/
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── uv.lock
│   │
│   └── frontend/
│
├── kubernetes/
│   ├── api/
│   ├── worker/
│   ├── frontend/
│   ├── redis/
│   └── e2e_test.py
│
├── scripts/
├── compose.yaml
├── mise.toml
└── README.md
```

Do not turn every directory into an independently deployed microservice.

The application remains:

```text
one backend codebase
one backend image
two backend processes
```

---

# 15. Implementation Phases

## Phase 0 — Convert `devops-app` Into the New Project

### Goal

Preserve the DevOps work while replacing the study-tracker domain.

### Tasks

- [ ] Create new repository/template from `devops-app`
- [ ] Rename application and Kubernetes resources
- [ ] Preserve DevPod/devcontainer setup
- [ ] Preserve `mise`
- [ ] Preserve `uv`
- [ ] Preserve Ruff/pre-commit
- [ ] Preserve pytest coverage checks
- [ ] Preserve Docker build patterns
- [ ] Preserve Trivy scanning
- [ ] Preserve Release Please
- [ ] Preserve GHCR publishing
- [ ] Preserve k3d E2E testing
- [ ] Preserve GitOps updates
- [ ] Preserve Flux reconciliation
- [ ] Remove study-tracker domain models
- [ ] Verify original pipeline still works with minimal FastAPI/Streamlit application

### Exit condition

A trivial renamed application can still travel:

```text
git push
→ CI
→ container
→ GHCR
→ GitOps
→ Flux
→ Kubernetes
```

---

# Phase 1 — PostgreSQL Application Foundation

### Goal

Replace SQLite with a production-style relational model.

### Tasks

- [ ] Add PostgreSQL dependency
- [ ] Add SQLAlchemy
- [ ] Add Alembic
- [ ] Create database session abstraction
- [ ] Add organisations table
- [ ] Add users
- [ ] Add memberships
- [ ] Add sources
- [ ] Add documents
- [ ] Add document versions
- [ ] Add ingestion jobs
- [ ] Add conversations/messages
- [ ] Add tenant-aware repository/service layer
- [ ] Add migrations
- [ ] Add integration tests against PostgreSQL
- [ ] Add PostgreSQL to Compose
- [ ] Add PostgreSQL to k3d E2E environment

### Exit condition

The application can create an organisation and persist document metadata.

---

# Phase 2 — Asynchronous Queue and Worker

### Goal

Introduce the producer/consumer model before adding AI.

### Tasks

- [ ] Deploy Redis
- [ ] Add Taskiq
- [ ] Configure Redis Stream broker
- [ ] Add worker command to backend image
- [ ] Add `worker` Kubernetes Deployment
- [ ] Implement trivial background task
- [ ] API creates `ingestion_jobs`
- [ ] API publishes `job_id`
- [ ] Worker consumes task
- [ ] Worker updates job status
- [ ] Implement retry behaviour
- [ ] Implement idempotency
- [ ] Implement error recording
- [ ] Implement job-status API
- [ ] Add queue/worker integration test

### Test

```text
POST /jobs/test

→ 202 Accepted
→ pending
→ processing
→ completed
```

### Exit condition

A worker can disappear and restart without corrupting application state.

---

# Phase 3 — Document Storage and Ingestion

### Goal

Turn uploaded documents into normalised text.

### Tasks

- [ ] Add object-storage abstraction
- [ ] Add local implementation
- [ ] Add S3-compatible implementation
- [ ] Implement file upload API
- [ ] Validate MIME types
- [ ] Add file-size limits
- [ ] Compute checksum
- [ ] Create document version
- [ ] Queue ingestion
- [ ] Implement PDF extraction
- [ ] Implement DOCX extraction
- [ ] Implement TXT extraction
- [ ] Implement Markdown extraction
- [ ] Normalise extracted documents
- [ ] Preserve page/section metadata
- [ ] Surface ingestion status in API

### Exit condition

Upload:

```text
policy.pdf
```

and receive:

```text
Uploaded
→ Queued
→ Processing
→ Indexed
```

without embeddings yet.

---

# Phase 4 — Embeddings and pgvector

### Goal

Turn parsed documents into searchable knowledge.

### Tasks

- [ ] Enable pgvector extension
- [ ] Create chunk table
- [ ] Add embedding column
- [ ] Implement text splitter
- [ ] Define chunk-size strategy
- [ ] Add embedding-provider abstraction
- [ ] Generate embeddings in worker
- [ ] Store chunks transactionally
- [ ] Implement vector similarity search
- [ ] Apply `organisation_id` filtering
- [ ] Add document/source filters
- [ ] Test retrieval quality
- [ ] Benchmark exact search before adding approximate indexes

Do not immediately add HNSW.

pgvector performs exact nearest-neighbour search by default; approximate indexes can be introduced when dataset size/latency provides a reason.

### Exit condition

Given:

```text
"What do we do after a medication incident?"
```

the API can return the most relevant document chunks without using an LLM.

---

# Phase 5 — Basic RAG

### Goal

Produce grounded answers from retrieved evidence.

### Tasks

- [ ] Add LangChain
- [ ] Add LLM-provider abstraction
- [ ] Add query embeddings
- [ ] Retrieve top-K chunks
- [ ] Construct grounded prompt
- [ ] Generate answer
- [ ] Return citations
- [ ] Prevent answers with zero supporting evidence
- [ ] Persist conversations/messages
- [ ] Record model/token usage
- [ ] Mock model calls in standard CI
- [ ] Create deterministic retrieval tests
- [ ] Create small evaluation dataset

### Exit condition

A user can upload internal documents and have a multi-turn conversation grounded in those documents.

This represents the first useful MVP.

---

# Phase 6 — Multi-Tenant Security

### Goal

Ensure one organisation can never retrieve another organisation's knowledge.

### Tasks

- [ ] Add authentication
- [ ] Resolve organisation from authenticated membership
- [ ] Add roles
- [ ] Require tenant context on repository operations
- [ ] Enforce tenant filtering in vector retrieval
- [ ] Add negative cross-tenant tests
- [ ] Scope object-storage keys
- [ ] Scope conversations
- [ ] Scope documents
- [ ] Add audit events for important operations
- [ ] Consider PostgreSQL Row Level Security as defence-in-depth

### Required test

```text
Tenant A uploads secret-policy.pdf

Tenant B searches for exact sentence from secret-policy.pdf

Expected:
0 results
```

This should become a mandatory security test.

---

# Phase 7 — Product UI

### Goal

Make the system usable without turning frontend development into the main project.

### Pages

```text
Login

Dashboard
├── document count
├── recent ingestion
└── failed jobs

Knowledge
├── Sources
├── Documents
└── Upload

Chat

Settings
└── Organisation
```

### Tasks

- [ ] Organisation selector/context
- [ ] Upload UI
- [ ] Document status UI
- [ ] Source list
- [ ] Chat UI
- [ ] Source citations
- [ ] Processing indicators
- [ ] Failed-job display
- [ ] Retry ingestion action

Streamlit is sufficient initially.

---

# Phase 8 — Kubernetes and GitOps Expansion

### Goal

Integrate the new architecture into the DevOps platform.

### Kubernetes

```text
namespace: knowledge-app

Deployment/frontend
Deployment/api
Deployment/worker

Service/frontend
Service/api

Redis
PostgreSQL / CNPG
```

### Tasks

- [ ] Create worker Deployment
- [ ] Create Redis deployment/chart
- [ ] Configure PostgreSQL
- [ ] Add pgvector
- [ ] Add ConfigMaps
- [ ] Add Secrets
- [ ] Add readiness probes
- [ ] Add liveness probes
- [ ] Set requests/limits
- [ ] Add graceful worker shutdown
- [ ] Ensure worker finishes/returns work appropriately on termination
- [ ] Update GitOps base
- [ ] Update dev overlay
- [ ] Update prod overlay
- [ ] Update k3d setup
- [ ] Extend E2E tests

### Important

The API should be stateless so:

```text
api replica 1
api replica 2
api replica 3
```

can operate behind a Kubernetes Service without application changes.

Workers should similarly scale independently:

```text
worker replica 1
worker replica 2
worker replica 3
```

This directly demonstrates horizontal scaling.

---

# Phase 9 — Observability and Reliability

### Goal

Understand system behaviour before introducing autoscaling.

### API metrics

```text
requests_total
request_duration
errors_total
rag_query_duration
llm_request_duration
retrieval_duration
```

### Worker metrics

```text
jobs_started_total
jobs_completed_total
jobs_failed_total
job_duration
documents_processed
chunks_created
embedding_requests
```

### Queue metrics

```text
queue_depth
oldest_message_age
active_workers
retry_count
```

### Database

Monitor:

```text
connections
query duration
slow queries
database size
vector query latency
```

### Tasks

- [ ] Structured JSON logging
- [ ] Prometheus metrics
- [ ] Grafana dashboard
- [ ] Correlation/request IDs
- [ ] Job IDs in worker logs
- [ ] Trace job lifecycle
- [ ] Alert on sustained ingestion failure
- [ ] Alert on worker unavailable
- [ ] Alert on excessive oldest-job age
- [ ] Add reconciliation CronJob for orphaned pending jobs

Only alert on conditions requiring intervention.

---

# Phase 10 — CI/CD Evolution

### Keep existing backend pipeline

```text
PR
 │
 ├── Ruff
 ├── pre-commit
 ├── unit tests
 ├── DB integration tests
 ├── queue tests
 ├── Docker build
 ├── Trivy
 └── k3d E2E
```

### E2E architecture

```text
k3d
├── frontend
├── api
├── worker
├── Redis
└── PostgreSQL
```

E2E test:

```text
upload known document
       ↓
wait until indexing completed
       ↓
perform retrieval
       ↓
verify expected source returned
```

Do not call a paid/live LLM during normal CI.

Use:

- fake model
- deterministic embedding implementation where appropriate
- known fixture documents

Live LLM evaluation should be a separate optional workflow.

---

# Phase 11 — External Knowledge Sources

Only begin after upload-based ingestion works well.

Create a common interface such as:

```python
class KnowledgeSource(Protocol):
    async def list_documents(self):
        ...

    async def fetch_document(self, document):
        ...
```

Then implement:

```text
UploadSource
WebsiteSource
SharePointSource
OneDriveSource
GoogleDriveSource
ConfluenceSource
```

### First external connector

Prefer implementing **one connector deeply** rather than five poorly.

A Microsoft 365 connector would be particularly useful for the intended business use case.

### Tasks

- [ ] OAuth/authentication
- [ ] List documents
- [ ] Pull document metadata
- [ ] Download changed documents
- [ ] Track external IDs
- [ ] Track external modification timestamps
- [ ] Detect deletions
- [ ] Re-index changed documents only
- [ ] Periodic synchronisation task
- [ ] Preserve source URL
- [ ] Map source permissions where possible

---

# Phase 12 — System Design Hardening

Implement only when measurements indicate a need.

## Caching

Potentially cache:

```text
embedding(query)
retrieval results
document metadata
```

Do not add general-purpose caching before profiling.

## Vector indexes

Introduce HNSW if exact vector retrieval becomes too slow.

pgvector documents HNSW as offering better query-performance/recall tradeoffs than IVFFlat, with higher memory usage and slower builds.

## Worker autoscaling

Possible later architecture:

```text
Redis queue depth
       ↓
KEDA
       ↓
worker Deployment
1 ──► 3 ──► 8 replicas
```

Only implement once queue behaviour is understood.

## Database scaling

Progress approximately through:

```text
indexes
↓
query optimisation
↓
larger PostgreSQL instance
↓
read replicas if required
↓
partitioning/sharding only at genuinely large scale
```

Do not jump directly to distributed databases.

---

# Phase 13 — LangGraph

LangGraph should be introduced when there is an actual graph to orchestrate.

For example:

```text
User Question
      │
      ▼
Determine request type
      │
      ├──── Policies
      │
      ├──── Procedures
      │
      ├──── Runbooks
      │
      └──── External Tool
              │
              ▼
         retrieve evidence
              │
              ▼
        enough evidence?
           │       │
          yes      no
           │       │
           │   rewrite query
           │       │
           └───────┘
              │
              ▼
         generate answer
```

Possible later nodes:

```text
classify request
search knowledge
search multiple collections
rewrite query
grade evidence
call business API
request human approval
execute action
verify result
```

That is where LangGraph becomes useful.

Do not use LangGraph simply to implement:

```text
retrieve → prompt → LLM
```

---

# 16. Explicitly Out of Scope for the MVP

Do not initially implement:

- Kafka
- RabbitMQ
- Elasticsearch
- dedicated vector database
- service mesh
- microservices for each subsystem
- Kubernetes operator
- LangGraph
- agents capable of modifying business systems
- automatic worker scaling
- multi-region deployment
- database sharding
- complex caching
- Stripe/billing
- mobile application
- elaborate React frontend
- multiple LLM providers simultaneously
- every enterprise document connector

The purpose is to build a good system and then allow its requirements to drive complexity.

---

# 17. MVP Definition

The MVP is complete when this works:

```text
User signs in
      ↓
belongs to Organisation A
      ↓
uploads a PDF
      ↓
API stores file
      ↓
PostgreSQL records ingestion job
      ↓
Redis queues job
      ↓
Taskiq worker receives job
      ↓
extract text
      ↓
chunk
      ↓
embed
      ↓
pgvector
      ↓
status becomes Indexed
      ↓
user asks question
      ↓
tenant-filtered retrieval
      ↓
LLM receives relevant evidence
      ↓
answer returned with citation
```

And the whole thing is delivered through:

```text
Git
 ↓
GitHub Actions
 ↓
tests + security scans
 ↓
GHCR
 ↓
GitOps repository
 ↓
Flux
 ↓
Kubernetes
```

---

# 18. What This Project Demonstrates

By the MVP stage, the project demonstrates much more than simply "I built a RAG chatbot."

It demonstrates:

```text
Application Engineering
├── Python
├── FastAPI
├── async programming
├── PostgreSQL
├── background workers
└── API design

AI Engineering
├── document ingestion
├── chunking
├── embeddings
├── vector retrieval
├── RAG
├── citations
└── evaluation

System Design
├── asynchronous processing
├── producer/consumer architecture
├── task queues
├── back pressure
├── idempotency
├── retry semantics
├── failure recovery
├── stateless services
├── horizontal scaling
├── object storage
└── multi-tenancy

Platform / DevOps
├── Docker
├── Kubernetes
├── GitHub Actions
├── GHCR
├── GitOps
├── Flux
├── k3d
├── observability
└── secrets/configuration
```

That combination is the real value of using `devops-app` as the foundation: **the delivery platform stays relatively stable while the application gives you progressively harder software and system-design problems to solve.**

# 19. Recommended Build Order

The shortest useful path is:

```text
1. Fork/template devops-app
2. PostgreSQL + schema
3. Redis Streams + Taskiq worker
4. Upload + object storage
5. PDF extraction
6. chunking
7. pgvector + embeddings
8. semantic search
9. RAG + citations
10. tenant isolation
11. UI polish
12. observability
13. one external connector
14. LangGraph only when the workflow requires it
```

This should be treated as an evolutionary architecture rather than attempting to implement the final-scale system on day one.
