# Knovera Engineering Study Guide

This documentation explains the repository as it exists today and proposes a
practical path to a production system. It is written for engineers who want to
understand the code before changing it.

## Analysis Baseline

| Item | Value |
|---|---|
| Repository commit | `ebc970e8e44523fbbc8b984429ab4e951d106179` |
| Analysis date | 2026-07-26 |
| Python checked | 3.11.9 |
| Runtime validation | Static analysis only |
| Syntax validation | `python -m compileall -q app.py core evaluation` passed |
| Evaluation data validation | All 150 JSONL records loaded through `TestQuestion` |
| Application import | Not runnable in this checkout because project dependencies, including Gradio, are not installed |
| Generated default vector index | Not present in the checkout; `data/vector_db/` is ignored and built at startup |

The file `C:\Users\Aditya\Downloads\analysis_results.md` was intentionally
excluded because it describes a different screen-overlay project.

## Reading Order

1. [Interview Study Guide](interview-study-guide.md)
   explains the project in simple interview language, including the full RAG
   pipeline, tools, design decisions, production improvements, and likely
   interview questions.
2. [Current Architecture and Pipelines](current-architecture.md)
   explains what runs, where state lives, and every end-to-end data path.
3. [System Reference](system-reference.md)
   catalogs modules, functions, models, configuration, APIs, URLs, resources,
   collections, and state shapes.
4. [Design Review](design-review.md)
   reconstructs the important design decisions and provides a prioritized risk
   and optimization register.
5. [AWS Production Blueprint](aws-production-blueprint.md)
   defines a low-complexity, regulated, multi-tenant pilot architecture.
6. [Migration Roadmap](migration-roadmap.md)
   turns the findings into phased work packages, tests, rollout gates, and
   acceptance criteria.

## Repository At A Glance

Knovera is a synchronous Python/Gradio application with two user modes:

- **Default mode** performs strict RAG over a shared built-in knowledge base.
- **Custom mode** creates a local Chroma database per browser session and
  combines document Q&A, pasted-text ingestion, direct LLM answers, URL
  scraping, and web search.

The current repository contains:

| Area | Inventory |
|---|---|
| Application | `app.py`, containing UI composition and most orchestration |
| Core modules | Configuration, retrieval/answering, extraction, static ingestion, session ingestion, session lifecycle, and bucket synchronization |
| Built-in source data | 76 Markdown documents in four categories |
| Source categories | 4 company, 32 contract, 32 employee, and 8 product files |
| Raw source size | Approximately 310 KB and 4,525 text lines |
| Evaluation suite | 150 synthetic questions across seven categories |
| Databases in code | ChromaDB only; no relational, graph, cache, or durable session database |
| Remote storage | One Hugging Face bucket synchronized through the `hf` CLI |
| External inference | LM Studio, Groq, or Mistral through OpenAI-compatible APIs |
| Other network use | Arbitrary URL fetching and DuckDuckGo search |

## Central Architectural Conclusion

The code is a useful RAG prototype, but its operational boundary is one Python
process and one writable local filesystem. Session identity, size accounting,
deduplication, scratchpads, model clients, and Chroma clients are all
process-local. The startup path synchronizes the entire `data/` directory with
a shared remote bucket, including session databases.

That combination is incompatible with the README's claims of fully ephemeral,
private, per-session data and is the first issue to contain before using real
employee, contract, insurance, or customer documents.

For the selected pilot profile, the recommended production direction is:

- Keep Python and the current RAG behavior long enough to migrate safely.
- Introduce a versioned FastAPI boundary while retaining Gradio as a
  transitional UI.
- Store source objects in S3.
- Store tenant metadata, conversations, jobs, audit events, and embeddings in
  PostgreSQL with `pgvector`.
- Move extraction and embedding to an idempotent SQS worker.
- Add Cognito authentication, tenant authorization, row-level security, KMS,
  and auditable deletion.
- Defer a graph database until named multi-hop use cases justify its operational
  cost.

## Terminology

| Term | Meaning in this repository |
|---|---|
| Static KB | Markdown under `data/raw/` and its generated `langchain` Chroma collection |
| Session KB | One `session` Chroma collection under `data/sessions/<uuid>/vector_db/` |
| Chunk | A character-based overlapping text fragment stored as one Chroma record |
| Result | A Pydantic object containing chunk text and an untyped metadata dictionary |
| Context | Retrieved chunks formatted into a prompt, limited by character count |
| Scratchpad | Process-memory working text used for short custom-mode follow-ups |
| Default mode | Built-in KB only, with no direct or web fallback |
| Custom mode | Session document retrieval plus heuristic direct/web/task routes |
| Analytical query | A query that receives an extra LLM evidence-structuring pass |
| Web context | DuckDuckGo result snippets, not the full contents of result pages |

## How To Use These Documents

- Use the current-state documents when debugging existing behavior.
- Use the design review before accepting README claims or extending a module.
- Use the production blueprint as a target, not as evidence that the target has
  already been implemented.
- Use the roadmap in order. Later retrieval improvements do not compensate for
  missing tenant security, unsafe storage, or unbounded ingestion.
- Re-run the inventory and refresh code references whenever `app.py` or
  `core/answer.py` changes substantially.
