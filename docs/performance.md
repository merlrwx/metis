# Retrieval baseline and scaling decisions

Measured on 2026-10-06 inside the Metis DevPod against its local PostgreSQL 17.11 / pgvector container. The benchmark calls the application's `search_chunks` with 1,536-dimensional synthetic hashing vectors, cosine distance and top five results. Each case has five warmups and 35 sequential timed calls; timings include SQL execution and ORM materialization, but exclude query embedding and LLM generation.

| Active chunks in queried tenant | Historical chunks | Median | p95 | Maximum |
| --- | --- | --- | --- | --- |
| 1,000 | 100 | 7.04 ms | 7.56 ms | 7.74 ms |
| 5,000 | 100 | 31.11 ms | 32.24 ms | 33.53 ms |

An additional tenant has 1,000 equally relevant chunks. Every call asserts that results belong to the requested tenant and current document versions. The script creates unique synthetic organisations and removes only those organisations in its cleanup. It stores no objects and makes no model requests.

Reproduce against an **isolated, migrated PostgreSQL database** inside DevPod:

```bash
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis_phase9_verify \
  METIS_BENCHMARK_ENABLED=true mise run benchmark-retrieval
```

Provision and migrate that database first; the benchmark does not create it. The JSON report goes to ignored `.agent/retrieval-benchmark.json`; `METIS_BENCHMARK_OUTPUT` overrides its location. A missing explicit database or opt-in flag prevents execution.

These are warm, single-client, small-corpus measurements with repeated synthetic vectors, not production capacity or retrieval-quality estimates. Re-run with realistic corpus sizes, embedding distributions, cold reads and concurrent tenants before establishing a capacity target. The separate three-case live ChatMock evaluation took 1.98–3.71 seconds for grounded answers; it is a different workload, but indicates that model response time currently exceeds these retrieval timings.

## Decisions for this MVP

- Keep exact retrieval. The measured p95 of 32.24 ms at 5,000 active chunks does not justify HNSW's additional build, memory and recall tradeoffs. Investigate query plans and tenant filtering first if representative retrieval p95 repeatedly exceeds a provisional 100 ms investigation threshold; benchmark an index with recall checks before adopting it.
- Keep query embedding, retrieval and document metadata uncached. There is no measured repeated-query bottleneck, hit rate or invalidation benefit. Any later cache must include tenant, model and current-version identity and must demonstrate a useful hit rate.
- Keep the configured worker replica count. Shutdown recovery, reconciliation and ingestion pass integration/E2E checks, but those functional checks do not establish sustained queue throughput. Use the existing queue age, job latency and failure metrics with a representative arrival-rate test before considering autoscaling. No KEDA dependency is added.
- Keep one PostgreSQL service and the existing tenant/version indexes. No measurements support replicas, partitioning or sharding. Profile real query plans, connection pressure and resource saturation before scaling the database.

The thresholds above are investigation triggers, not promised service-level objectives. Monitoring configuration and the operational runbook describe the existing measurements and alerts.
