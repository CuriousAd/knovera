# Design Decisions, Production Risks, And Optimizations

## 1. Review Method

This review uses:

- Source code at commit
  `ebc970e8e44523fbbc8b984429ab4e951d106179`
- Repository history from the initial release through the current commit
- The checked-in README, environment example, dependencies, corpus, and
  evaluation suite
- Static syntax and evaluation-schema validation
- The selected target: regulated, multi-tenant, low-scale AWS pilot

Severity means:

| Priority | Meaning |
|---|---|
| **P0** | Can cause tenant/privacy compromise, unsafe exposure, or unrecoverable/corrupt data; block production |
| **P1** | Can cause significant reliability, quality, scalability, or operational failure; fix for pilot |
| **P2** | Maintainability, efficiency, or quality debt that should be scheduled |
| **P3** | Cleanup or polish with limited near-term operational impact |

This is a source review, not a penetration test, load test, dependency audit, or
live provider assessment.

## 2. Architectural Evolution From Git History

| Commit | Decision | What it solved | New tradeoff or remaining gap |
|---|---|---|---|
| `27685b2` | Initial Gradio, Chroma, RAG, extraction, sessions, evaluation | Delivered a complete demonstrator quickly | UI, orchestration, persistence, and evaluation share one process |
| `e51ecef` | Move session storage to OS temp | Addressed read-only deployment filesystems | Later reverted when bucket persistence was added |
| `b781e8f` through `6b5a0b6` | Rework web fallback and grounding | Reduced irrelevant KB answers and disclosed web use | Behavior became complex and some fallback/persistence branches are now dormant |
| `115100f` | Mirror whole `data/` directory to HF bucket | Persisted default vector data and moved sessions back under `data/` | Persisted private session stores and introduced live SQLite sync risk |
| `2c355e2` | Callable Gradio state and one Chroma client per session | Fixed shared session IDs and repeated-client file locking | Client ownership/closing and process-local registries remain |
| `1dea116` | Correct corpus counts and add analytical preprocessing | Improved conflict handling and synthesis prompts | Retrieval still returns at most six small chunks, which limits complete analytics |
| `ebc970e` | Add local custom router and scratchpad | Avoided unnecessary retrieval for coding/math/tasks and enabled follow-ups | Product is no longer strictly document-grounded; routing and memory are untyped process state |

The history shows pragmatic responses to real prototype failures. The main
production problem is not that those fixes were unreasonable; it is that local
process and filesystem techniques were extended past the boundary where they
can provide privacy, durability, or horizontal scaling.

## 3. Decision Review

### 3.1 Gradio modular monolith

**Decision:** Put UI and orchestration in one `app.py`.

**Why it works now:**

- Very low setup cost.
- Easy local demonstration.
- State and callbacks are visible in one place.
- Evaluation UI can directly call production functions.

**Where it stops working:**

- No stable or versioned API for another frontend/client.
- Synchronous callbacks own long-running CPU and network work.
- Authentication, authorization, quotas, and audit are absent.
- The 790-line file mixes view composition, routing, session memory, ingestion,
  web access, evaluation, and error presentation.

**Recommendation:** Keep one deployable modular monolith for the pilot, but
introduce FastAPI application services and typed boundaries. Mount Gradio as a
transitional UI rather than building microservices immediately.

### 3.2 Chroma as both default and session vector store

**Decision:** Use local Chroma persistence for simple vector search and one
database directory per custom session.

**Why it works now:**

- Embedded and easy to run.
- No separate database service.
- Per-directory session separation is simple to understand.
- Collection APIs hold text, metadata, and embeddings together.

**Where it stops working:**

- SQLite-backed live files are not a multi-replica synchronization protocol.
- Durable document metadata, ownership, versions, deletion, and audit are
  missing.
- One directory per browser session creates lifecycle and filesystem pressure.
- Process caches are required to avoid repeated connection/file-lock failures.
- Filtering, relational joins, authorization, and vector data cannot share one
  transaction.

**Recommendation:** For the selected pilot scale, consolidate metadata and
vectors in RDS PostgreSQL with `pgvector`. A dedicated vector database is not
justified yet. Re-evaluate OpenSearch or a dedicated vector service when
benchmarks show PostgreSQL is the limiting component.

### 3.3 Per-session UUID isolation

**Decision:** Generate UUID4 per Gradio session and use it in a filesystem path.

**Why it works now:**

- Fixes the earlier bug where all browsers could share one session.
- Reduces accidental cross-session reads inside a single process.
- Makes reset mechanically straightforward.

**Where it stops working:**

- A UUID is an identifier, not authorization.
- There is no user/tenant ownership record.
- Global registries disappear or diverge across replicas.
- Browser disconnects leave orphans.
- Bucket sync defeats ephemeral deletion.

**Recommendation:** Use authenticated user and tenant claims. Persist session
ownership and enforce tenant access in both application queries and PostgreSQL
row-level security.

### 3.4 Whole-directory bucket synchronization

**Decision:** Restore and periodically back up the entire `data/` tree with the
HF CLI.

**Why it was added:**

- A hosted filesystem can be ephemeral.
- Generated static vectors are expensive to rebuild.
- A CLI mirror is fast to implement.

**Why it is unsafe:**

- Static data, generated indexes, and private session data have different
  retention and access requirements but share one synchronization scope.
- Copying active Chroma/SQLite files can produce inconsistent snapshots.
- Multiple replicas have no conflict ownership.
- Restore can revive sessions and stale vector indexes.
- There is no integrity/version manifest or transactional cutover.

**Recommendation:** Remove this mechanism. Keep immutable originals and derived
artifacts in explicit S3 prefixes, metadata in PostgreSQL, and rebuildable
embeddings in `pgvector`. Never synchronize live database files.

### 3.5 Multi-provider OpenAI-compatible clients

**Decision:** Reuse `ChatOpenAI` for local, Groq, and Mistral providers.

**Strengths:**

- Small adapter surface.
- Runtime provider selection.
- Cached clients avoid repeated setup.
- Temperature zero improves repeatability.

**Gaps:**

- Provider labels and actual endpoints can diverge.
- No timeout, retry, token, budget, data-classification, or egress policy.
- Raw provider errors can reach users.
- Any configured remote provider receives history and sensitive retrieved text.
- Provider fallback/availability is UI logic, not governed routing.

**Recommendation:** Introduce a typed model gateway interface with per-tenant
policy, explicit timeouts/retries, model allowlists, token accounting, tracing,
redaction rules, and normalized errors. Use Bedrock as the production default
for the AWS pilot and keep local/OpenAI-compatible adapters for development.

### 3.6 Query rewrite plus dual semantic retrieval

**Decision:** Rewrite each query, retrieve for both forms, merge, and lexically
rerank.

**Strengths:**

- Rewrites can improve terse or colloquial queries.
- Searching both forms protects against a bad rewrite.
- Deterministic lexical reranking is cheap.
- Empty/no-overlap context causes refusal.

**Gaps:**

- Rewrite, retrievals, and classifications are serial.
- Chroma distances are discarded.
- Exact-keyword reranking can demote semantically strong chunks.
- The gate only requires one overlap in one chunk.
- Rewrites can introduce terms that make the gate pass for the wrong reason.
- Hardcoded distance `1.6` is tied to an implicit metric and embedding model.
- Query embeddings are encoded in two separate calls.

**Recommendation:** Start with tenant-filtered hybrid vector and PostgreSQL
full-text retrieval, fuse rankings with reciprocal-rank fusion, preserve all
scores, and make rewriting conditional based on benchmarked intent. Batch
embeddings and add an optional reranker only if evaluation proves the gain.

### 3.7 Character chunking

**Decision:** Use `RecursiveCharacterTextSplitter` with 400 characters and 100
characters overlap.

**Strengths:**

- Simple and consistent across static and custom data.
- Works without a model-specific tokenizer.
- Overlap reduces boundary loss.

**Gaps:**

- Character counts do not map reliably to model tokens.
- Contract clauses, Markdown headings, pages, and tables lose structure.
- No stable chunk hash, ordinal, page, section, or parent reference.
- High 25 percent overlap increases storage and repeated prompt text.
- The same strategy is applied to every format and query type.

**Recommendation:** Normalize extraction into blocks with provenance, then use
token-aware, heading/page-aware child chunks linked to parent sections. Store
chunk hashes and embedding-model versions so changed content can be reindexed
incrementally.

### 3.8 Analytical LLM preprocessing

**Decision:** Classify analytical questions and ask the LLM to structure
evidence before final answering.

**Strengths:**

- Makes prompt intent explicit.
- Can improve synthesis over messy snippets.
- Prompt correctly admits missing evidence.

**Gaps:**

- It cannot recover facts that were never retrieved.
- Six 400-character chunks are insufficient for many corpus-wide aggregates.
- It adds one LLM call and can transform/misstate evidence.
- Structured output is Markdown rather than a validated schema.

**Recommendation:** Retrieve at document/parent-section scope for analytical
queries and use structured facts for trusted aggregations. Validate extraction
schemas and retain chunk-level provenance for every generated row.

### 3.9 Heuristic router and scratchpad

**Decision:** Route common tasks locally and retain the latest working material
in a 12,000-character scratchpad.

**Strengths:**

- Avoids an extra router LLM call.
- Produces better behavior for pasted coding problems and summaries.
- Makes short follow-ups practical.

**Gaps:**

- Broad substring checks cause false positives.
- UI text decides whether documents exist.
- Scratchpads are global process memory with no tenant policy or expiry.
- The final 12,000 characters can omit the beginning/instructions of pasted
  material.
- Direct answers change the system from strict RAG to a mixed-trust assistant.

**Recommendation:** Keep deterministic routing but represent decisions as typed
policy results with confidence/reason. Persist bounded conversation state,
label ungrounded answers, and make direct/web routes explicit tenant settings.

### 3.10 Built-in evaluation dashboard

**Decision:** Ship evaluation in the same UI and call production functions.

**Strengths:**

- Evaluation is discoverable.
- The 150-case dataset covers more than direct facts.
- Retrieval can still complete when the local LLM is unavailable.

**Gaps:**

- Any user can start expensive sequential runs.
- Metrics use keyword substrings rather than labelled relevant chunks.
- Retrieval evaluation attempts one rewrite LLM call per test.
- Answer and judge can be the same model.
- Runs/results/configuration are not persisted.
- There are no security, isolation, parser, citation, latency, or failure tests.

**Recommendation:** Move evaluation behind an admin/CI boundary. Store immutable
run configuration and results, add labelled chunk/document relevance, use
independent judges plus human calibration, and establish release thresholds.

## 4. P0 Production Blockers

| ID | Finding and evidence | Impact | Required correction |
|---|---|---|---|
| P0-01 | `app.py:599-607` restores and backs up all `data/`; `core/sync_manager.py:8-53` has no exclusions; sessions live under `data/` in `core/session_manager.py:6-18` | Uploaded employee/contract/customer text can be copied to a shared remote bucket and restored after supposed deletion | Disable session/default live-DB sync immediately; define explicit S3 object classes, tenant prefixes, retention, encryption, and deletion |
| P0-02 | Gradio is launched without authentication or authorization at `app.py:651-786`; session UUID is the only selector | Any reachable user can access assistant/evaluation capabilities; there is no tenant ownership boundary | Add Cognito/OIDC authentication, application RBAC, tenant context, and database RLS before real data |
| P0-03 | `core/extractors.py:99-131` fetches arbitrary URLs and follows redirects; `app.py:247-275` exposes it directly | Server-side request forgery can reach internal services, loopback, cloud metadata, or sensitive endpoints | Add URL policy, DNS/IP validation before and after redirects, private-range blocking, HTTPS/port rules, size/time limits, and egress controls |
| P0-04 | The backup thread can copy local Chroma/SQLite while callbacks write it; there is no lock/snapshot/version protocol | Corrupt or inconsistent vector databases and cross-replica last-writer-wins data loss | Stop copying live database directories; migrate durable records through supported database/object APIs |
| P0-05 | Retrieved documents and web text are placed directly into model prompts (`core/answer.py:26-64`, `430-443`, `673-679`) | Prompt injection can override intended behavior or cause cross-document data disclosure | Treat retrieved content as untrusted, isolate instructions from data, enforce authorization before retrieval, add input/output security tests and policy controls |

## 5. P1 Pilot Requirements

| ID | Finding | Consequence | Correction |
|---|---|---|---|
| P1-01 | Session metadata and scratchpads are process dictionaries | State loss, leaks, and inconsistent replicas | Persist ownership, sessions, messages, jobs, and TTLs |
| P1-02 | Browser disconnect has no cleanup; shutdown is best-effort | Orphan storage and unbounded growth | TTL/reaper jobs and auditable deletion workflow |
| P1-03 | Session size counts source bytes only; pasted ingestion lacks precheck | Resource exhaustion despite displayed 50 MB cap | Enforce upload, extracted-byte, page, token, chunk, vector, and tenant quota limits |
| P1-04 | File extension is trusted; no malware or parser sandbox | Malicious/invalid files can attack parsers or consume resources | Quarantine, signature/MIME validation, malware scan, parser isolation and timeouts |
| P1-05 | `collection.count()` allocates chunk IDs | Duplicate IDs under concurrent writes or prior deletions | UUID/content-derived IDs and idempotent upserts |
| P1-06 | File hash is marked before ingestion completes | Failed files can become non-retryable in that process | Record idempotency and status transactionally after successful stage completion |
| P1-07 | Static index skips any non-empty collection | Stale sources, chunks, or embedding schema remain active | Content/model/config manifests and incremental versioned indexing |
| P1-08 | Forced static rebuild deletes old collection first | Failed rebuild removes serviceable index | Build a new version then atomically switch active index |
| P1-09 | LLM clients have no explicit timeout/retry/output budget | Hung callbacks, unpredictable cost, poor provider recovery | Gateway-level timeout, bounded exponential backoff, circuit breaker, token limits, budget |
| P1-10 | Full chat history is sent on answer calls | Context overflow, latency/cost growth, unnecessary sensitive-data egress | Bounded history window plus persisted summary and token accounting |
| P1-11 | Upload, OCR, embedding, and evaluation are synchronous callbacks | User timeouts, blocked workers, no durable retry | SQS-backed ingestion/evaluation jobs with progress and DLQ |
| P1-12 | No structured logs, traces, audit, or metrics | Incidents and data access cannot be reconstructed | Correlation IDs, tenant/user audit, retrieval traces, provider metrics, alarms |
| P1-13 | Dependencies are unpinned | Non-reproducible builds and supply-chain exposure | Lock Python/system dependencies, scan images and SBOM, controlled upgrades |
| P1-14 | Provider availability can be empty in HF runtime | Startup crash at `AVAILABLE_PROVIDERS[0]` | Validated settings and fail-fast diagnostic |
| P1-15 | Evaluation is an unrestricted production UI action | Cost/availability denial of service | Admin-only asynchronous evaluation with quotas |
| P1-16 | Data deletion is directory best-effort and bucket unaware | Cannot prove erasure or retention compliance | Version-aware delete jobs covering objects, chunks, messages, caches, backups and audit outcome |

## 6. P2 Quality And Maintainability Findings

| ID | Finding | Recommended optimization |
|---|---|---|
| P2-01 | Semantic distances are discarded | Carry vector, lexical, fusion, and rerank scores in a typed retrieval result |
| P2-02 | Two embeddings and searches run serially | Batch query embeddings; parallelize independent store calls where justified |
| P2-03 | Exact keyword reranker is simplistic | Hybrid full-text/vector retrieval and benchmarked cross-encoder rerank |
| P2-04 | Relevance gate accepts all chunks if one overlaps | Score/filter each candidate and calibrate an abstention threshold |
| P2-05 | Hardcoded distance threshold depends on implicit Chroma metric | Configure metric explicitly and calibrate per embedding/index version |
| P2-06 | Character context cap can cut evidence mid-chunk | Use model tokenizer and reserve budgets for system/history/output |
| P2-07 | Search answers rely on snippets | Fetch approved sources safely or disclose snippet-only confidence |
| P2-08 | URL extraction removes every table | Preserve structured tables with provenance |
| P2-09 | DOCX extraction ignores tables/headers/footers | Rich document-normalization layer with format-specific fixtures |
| P2-10 | PDF extraction has no OCR fallback/page provenance | Page-block model and scanned-PDF OCR path |
| P2-11 | Source path can be absolute | Store stable logical source IDs and avoid leaking host paths to providers/users |
| P2-12 | Result and metadata are weakly typed | Pydantic models for chunks, scores, provenance, route decisions, provider errors |
| P2-13 | Callback return tuples are positional | Named application result models mapped to UI outputs |
| P2-14 | Route words are hardcoded English | Versioned router policy with tests, localization boundary, and reason codes |
| P2-15 | Dead web-persistence and `classify_input` paths remain | Remove or reconnect only after behavior tests define intent |
| P2-16 | Unused imports/globals and circular ownership | Separate embedding, retrieval, model, ingestion, and session services |
| P2-17 | README and `.env.example` drift | Generate a validated config reference and add docs checks to CI |
| P2-18 | Retrieval metrics are keyword-substring proxies | Label relevant documents/chunks and calculate query-level ranking metrics |
| P2-19 | Judge scores lack bounds and independent calibration | Constrained schema, independent model, repeat sampling, human agreement checks |
| P2-20 | No unit/integration tests | Router, extractor, session, retrieval, provider-error, and security suites |

## 7. Database And Storage Optimization

### 7.1 Current-to-target matrix

| Concern | Current | Pilot recommendation | Scale-out trigger |
|---|---|---|---|
| Original documents | Gradio temp files or Git Markdown | S3, immutable versioned object keys | Separate buckets/accounts by regulatory boundary |
| Extracted text | Only Chroma chunks/scratchpad | Versioned normalized artifact in S3 plus metadata row | Columnar analytics/data lake if required |
| Metadata | UI strings and dictionaries | PostgreSQL typed tables | Read replicas/partitioning based on measured load |
| Vectors | Local Chroma | RDS PostgreSQL `pgvector` | OpenSearch/dedicated vector DB when benchmarked recall/QPS/cost fails |
| Keyword search | Exact regex overlap | PostgreSQL `tsvector`/GIN plus vector fusion | OpenSearch for advanced search/large scale |
| Sessions/history | Gradio/process memory | PostgreSQL with TTL and tenant RLS | Redis only for measured hot ephemeral state |
| Jobs | Synchronous callbacks | SQS plus PostgreSQL job status | Step Functions for long multi-stage workflows |
| Audit | Console prints | Append-only audit table plus CloudTrail/log archive | Dedicated security lake/SIEM |
| Graph | None | No graph DB in MVP | Neptune after proven multi-hop accuracy need |

### 7.2 Why PostgreSQL plus `pgvector`

For the selected pilot, one managed transactional database minimizes moving
parts while enabling:

- Tenant-filtered vector retrieval
- Relational metadata and ownership
- Full-text search
- Row-level security
- Idempotent ingestion transactions
- Deletion and audit workflows
- Familiar backup and migration tooling

AWS documents `pgvector` support in both RDS and Aurora PostgreSQL, including
approximate indexes and combining domain data with vector queries:
[AWS RAG retriever guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/retrieval-augmented-generation-options/rag-custom-retrievers.html).

A dedicated vector database becomes justified when measured vector count,
filter cardinality, recall, p95 latency, write throughput, or operational cost
cannot meet the SLO with tuned PostgreSQL.

### 7.3 Graph database decision

A graph database should not be added because the corpus contains relationships.
It should be added when the product has:

1. Named, frequent multi-hop questions, such as connecting employees,
   contracts, clients, obligations, and products.
2. A governed entity-resolution and relationship-extraction process.
3. Proven inability to meet correctness/latency with PostgreSQL joins,
   recursive CTEs, or materialized views.
4. An evaluation suite with expected paths and provenance.
5. An operational owner for graph schema, ingestion, backup, and consistency.

Until then:

- Keep typed domain fields in PostgreSQL where correctness matters.
- Store optional extracted entities/relationships with source-chunk provenance.
- Do not use LLM-extracted graph facts as authoritative without validation.
- Design stable IDs so accepted relationships can later be exported to Neptune.

### 7.4 Object-storage layout

Use content-addressed, tenant-scoped keys:

```text
tenants/<tenant_id>/
  documents/<document_id>/
    versions/<version_id>/
      original/<sanitized_filename>
      normalized/content.json
      manifests/ingestion.json
```

Required metadata includes SHA-256, byte size, detected MIME type, source,
uploader, timestamps, classification, retention policy, scan status, extraction
version, and encryption key.

Use S3 versioning for recovery. Enable Object Lock only in a separate archive
class where a real immutable-retention requirement exists, because WORM
retention conflicts with ordinary deletion requirements:
[S3 Object Lock guidance](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html).

## 8. Retrieval And RAG Optimization

### 8.1 Ingestion quality

1. Normalize every format into ordered blocks with page, section, table, and
   source coordinates.
2. Compute document/version/block/chunk SHA-256 hashes.
3. Use model-token-aware chunks with parent section links.
4. Batch embeddings and store model name, dimension, normalization, metric, and
   version.
5. Upsert into a new document version, then atomically activate it.
6. Retain an ingestion manifest and errors per stage.

### 8.2 Candidate generation

Use two tenant-filtered candidate lists:

- Vector similarity over chunk embeddings.
- PostgreSQL full-text rank over normalized chunk text and selected metadata.

Fuse them using reciprocal-rank fusion. Preserve:

```text
vector_rank
vector_score
lexical_rank
lexical_score
fusion_score
rerank_score (optional)
```

Use source/parent diversity so six near-duplicate overlapping chunks do not
consume the context.

### 8.3 Query planning

- Classify route and retrieval scope with deterministic rules first.
- Rewrite only ambiguous/underspecified document questions.
- Batch original and rewritten embeddings in one request.
- For analytical queries, retrieve candidate documents/parent sections before
  child chunks.
- Filter by tenant, document ACL, active version, source type, and user policy
  before text reaches a model.
- Keep web search and direct answers disabled by default for regulated tenants.

### 8.4 Context and answer

- Budget using the selected model tokenizer, not characters.
- Reserve explicit budgets for system policy, conversation summary, evidence,
  current question, and output.
- Assign stable citation IDs before calling the model.
- Require structured answer output containing text, citations, confidence or
  abstention reason, and model/retrieval trace IDs.
- Validate that cited IDs exist and that major claims have evidence.
- Store answer lineage without logging raw sensitive content by default.

### 8.5 Latency reduction

Current lookup requests often require three serial LLM calls. The pilot should
target:

- Zero LLM calls for deterministic routing.
- Zero or one rewrite call only when needed.
- One final answer call.
- Optional reranking as a model-independent local/service call.
- Batched embeddings.
- Caches keyed by tenant, normalized query, ACL/version watermark, and retrieval
  configuration, only after authorization correctness is proven.

## 9. Security And Governance Optimization

The current product handles untrusted documents, URLs, search snippets, and
model output. Production controls must cover every boundary:

- Authenticate every request and derive tenant/user context from validated
  claims.
- Authorize document access before retrieval, not after answer generation.
- Enforce tenant isolation with application checks and PostgreSQL RLS.
- Quarantine and scan uploads; validate MIME/signature; sandbox parsers.
- Restrict URL ingestion with egress allow/deny policy and private-address
  blocking.
- Classify PII/secrets and apply tenant-specific model egress rules.
- Separate system instructions from untrusted retrieved text.
- Detect and test prompt injection, data poisoning, sensitive disclosure, and
  citation manipulation.
- Encrypt objects, database, queues, secrets, and logs with managed keys.
- Use least-privilege task roles and private service connectivity.
- Store access/deletion/admin/evaluation audit events.
- Define retention, legal hold, deletion, backup, and incident procedures.

The threat model should be reviewed against:

- [AWS Well-Architected Generative AI Lens](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/generative-ai-lens.html)
- [AWS Generative AI security guidance](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/security.html)
- [OWASP Top 10 for LLM Applications](https://genai.owasp.org/llm-top-10/)
- [NIST AI RMF Generative AI Profile](https://doi.org/10.6028/NIST.AI.600-1)

For an AWS-regulated deployment, Bedrock provides a more governable production
default than sending retrieved content to whichever OpenAI-compatible endpoint
is selected in the UI. Its exact model must still be selected by regional
availability, quality, cost, and governance evaluation. See
[Bedrock data protection](https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html).

## 10. Reliability And Observability Optimization

### Required identifiers

Every request/job/log/trace should carry:

- `request_id`
- `correlation_id`
- `tenant_id`
- `user_id` or service subject
- `conversation_id`
- `document_id` / `document_version_id` where relevant
- `ingestion_job_id`
- `retrieval_trace_id`
- Prompt, model, embedding, parser, and index versions

### Required metrics

- API request count, 4xx/5xx, and p50/p95/p99 latency
- Provider request latency, errors, throttles, input/output tokens, and cost
- Embedding latency, batch size, and errors
- Retrieval latency, candidate counts, score distribution, no-hit rate, and
  citation coverage
- Upload bytes, extraction duration, pages/blocks/chunks, scan failures
- SQS queue depth/age, retry count, and DLQ depth
- PostgreSQL connections, CPU, storage, locks, vector query latency
- Per-tenant document/vector/storage/model usage
- Deletion backlog and retention-policy failures

### Required failure patterns

- Timeouts and bounded retries with jitter for external calls
- Circuit breakers for unhealthy providers
- Idempotency keys for document finalization and ingestion
- SQS visibility heartbeat and DLQ
- Stage-level job status and safe redrive
- Atomic active-version switch
- Graceful degraded answers when optional reranking/rewrite is unavailable
- No raw context dumped to users on provider failure

## 11. Testing Optimization

### Unit tests

- Router precedence and ambiguous phrases
- URL and private-address validation
- MIME/signature and size/page/token limits
- Extractor success/error normalization
- Token chunk boundaries and provenance
- RRF/reranking/diversity
- Context budgets and citation validation
- Tenant-policy and RLS helpers
- Provider error normalization and retry decisions

### Integration tests

- Upload to S3 through indexed document
- Duplicate/idempotent ingestion and retries
- Version replacement and delete cascade
- Two-tenant adversarial retrieval isolation
- SQS visibility timeout and DLQ redrive
- Provider timeout/rate limit/circuit breaker
- Parser crash and malformed file
- Database failover/reconnect

### RAG evaluation

- Label relevant document and chunk IDs, not only keywords.
- Separate development and held-out sets.
- Add expected abstentions and confusing near-neighbor documents.
- Score retrieval recall@k, MRR/nDCG at query level, citation precision/recall,
  groundedness, answer correctness, and latency/cost.
- Add multi-hop cases before considering a graph database.
- Add direct prompt injection, indirect injection in documents, poisoned
  metadata, sensitive disclosure, and cross-tenant attempts.
- Version and persist every run's corpus, index, prompts, models, parameters,
  and code revision.

## 12. Recommended Design Patterns

| Pattern | Application |
|---|---|
| Modular monolith | Keep one API deployment while separating domain/application/infrastructure/UI modules |
| Ports and adapters | Model, embedder, vector search, object storage, extractor, and queue interfaces |
| Strategy | Format-specific extractors and provider-specific model adapters |
| Repository | Typed PostgreSQL persistence without leaking SQL into orchestration |
| Command/job | Durable ingestion stages with idempotent commands |
| Outbox | Commit document/job state and publish work without losing events |
| State machine | Explicit upload, scan, extract, embed, indexed, failed, deleting states |
| Policy object | Tenant web/direct/model/retention/access policies |
| Circuit breaker | Provider and scraper failure containment |
| Bulkhead | Separate API and worker capacity so ingestion cannot exhaust chat |
| Versioned artifact | Document, extraction, embedding, prompt, and index lineage |
| Strangler migration | Route selected traffic to new stores/services while old UI remains usable |

Microservices are deliberately not recommended for the pilot. Clear module and
data boundaries provide most of the engineering benefit without distributed
deployment overhead.

## 13. Immediate Containment Order

Before any quality tuning:

1. Stop backing up/restoring `data/sessions` and live Chroma files.
2. Do not expose the application to real users without authentication.
3. Disable arbitrary URL ingestion until SSRF controls exist.
4. Treat all existing remote bucket session data as potentially retained and
   review/delete it according to policy.
5. Pin dependencies and create a reproducible container.
6. Add upload/extraction quotas and isolate expensive work.
7. Add tenant-aware durable metadata and audit.

Only after these controls should the team optimize chunk size, reranking, graph
retrieval, model selection, or UI features.
