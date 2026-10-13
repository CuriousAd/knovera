import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from chromadb import PersistentClient as ChromaClient
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage, convert_to_messages
from langchain_openai import ChatOpenAI
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer


from core.config import (
    DB_NAME,
    KNOWLEDGE_BASE,
    COLLECTION_NAME,
    EMBEDDING_MODEL_NAME,
    RETRIEVAL_K,
    FINAL_K,
    MAX_CONTEXT_CHARS,
    MODEL_PROVIDERS,
)

SYSTEM_PROMPT = """You are a helpful assistant. Answer the user's question using the Context provided below.

IMPORTANT RULES:
1. Base your answer strictly on the Context below. Summarize, explain, and elaborate on what the context says.
2. Distinguish clearly between facts stated in the text and your own inferences.
3. **CONFLICT DISCLOSURE**: If the Context contains conflicting information from different sources (e.g., differing contract counts or conflicting dates), explicitly state the conflict and list what each source says. Do NOT silently resolve or ignore the conflict.
4. **EVIDENCE DISCIPLINE**: If asked to compare, rank, or aggregate across many items, but the Context only provides incomplete evidence, explicitly state that you cannot fully answer the question due to insufficient evidence. Do not generalize from 1 or 2 examples.
5. NEVER make up facts, names, events, or details that are not in the Context.
6. Always cite the source document name for every major claim in your answer.

Context:
{context}
"""

WEB_FALLBACK_PROMPT = """You are a helpful assistant. The context below was retrieved from a live web search.

IMPORTANT RULES:
1. ALWAYS begin your answer with this exact disclaimer on its own line:
   > [Web Search] The following answer is based on a live web search.
2. Answer ONLY using the web search context below. Do not add facts from your training data.
3. If the web context below is clearly irrelevant or insufficient to answer the question, respond with:
   > [Web Search] The web search didn't return a reliable answer. Try rephrasing or adding a relevant document.
4. NEVER fabricate details not present in the context.
5. Cite the source URL at the end of your answer when available.

Web Search Context:
{context}
"""

DIRECT_ANSWER_PROMPT = """You are a helpful assistant. Answer the user directly.

If Active Context is provided, use it as the primary material for the answer.
If the user asks for code, provide a clear solution and briefly explain the approach.
If the user asks for a summary, summarize only the provided material.
Do not claim that the answer came from uploaded documents unless document context is explicitly provided.

Active Context:
{context}
"""

# Common English words excluded from keyword overlap checks.
# Without this, words like "what", "how", "the" match any document text
# and produce false positives that silently block the web search fallback.
_STOP_WORDS = {
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "the", "and", "but", "for", "are", "was", "were", "has", "have", "had",
    "not", "can", "could", "would", "should", "will", "shall", "may", "might",
    "does", "did", "its", "this", "that", "these", "those", "with", "from",
    "into", "about", "tell", "give", "show", "find", "get", "just", "also",
    "explain", "describe", "list",
    # 2-letter additions to safely support len > 1 acronyms (e.g. OS, DB, AI)
    "is", "on", "in", "to", "of", "it", "as", "be", "or", "at", "by", 
    "an", "am", "do", "go", "he", "me", "my", "no", "so", "up", "us", "we", "if"
}

WEB_SEARCH_K = 5

embedder = None
collection = None
_llm_cache: dict[str, ChatOpenAI] = {}
_chroma_cache = {}


class Result(BaseModel):
    page_content: str
    metadata: dict


def get_embedder() -> SentenceTransformer:
    """Load the embedding model, downloading it on first run if needed."""
    global embedder
    if embedder is None:
        embedder = SentenceTransformer(
            EMBEDDING_MODEL_NAME,
            cache_folder=os.getenv("HF_HOME"),
        )
    return embedder


def get_collection():
    """Connect lazily so importing the module stays fast and offline-safe."""
    global collection
    if collection is None:
        chroma = ChromaClient(path=DB_NAME)
        collection = chroma.get_or_create_collection(COLLECTION_NAME, metadata={"hnsw:space": "cosine"})
    return collection


from core.session_manager import get_session_client

def get_arbitrary_collection(session_id: str):
    """Connect to the session's Chroma DB path using the unified cache."""
    chroma = get_session_client(session_id)
    return chroma.get_or_create_collection("session", metadata={"hnsw:space": "cosine"})


def get_llm(provider: str = "Groq - Llama 3.3 70B (Free)") -> ChatOpenAI:
    """Return a cached LLM client for the given provider."""
    if provider not in _llm_cache:
        cfg = MODEL_PROVIDERS.get(provider)
        if not cfg or not cfg.get("api_key"):
            raise ValueError(
                f"API key for provider '{provider}' is not configured. "
                "Please configure GROQ_API_KEY, MISTRAL_API_KEY, or OPENAI_API_KEY."
            )
        _llm_cache[provider] = ChatOpenAI(
            temperature=0,
            model_name=cfg["model"],
            openai_api_base=cfg["base_url"],
            openai_api_key=cfg["api_key"],
        )
    return _llm_cache[provider]


def fetch_context_unranked(question: str, session_id: str | None = None) -> list[Result]:
    """Embed question with normalized embeddings and retrieve top-k chunks from Chroma."""
    coll = get_arbitrary_collection(session_id) if session_id else get_collection()

    try:
        vector = get_embedder().encode([question], normalize_embeddings=True).tolist()[0]
        results = coll.query(
            query_embeddings=[vector], 
            n_results=RETRIEVAL_K,
            include=["documents", "metadatas", "distances"]
        )
    except Exception as e:
        print(f"Retrieval error: {e}")
        return []

    chunks = []
    if results and "documents" in results and results["documents"] and results["documents"][0]:
        docs = results["documents"][0]
        metas = results["metadatas"][0]
        dists = results.get("distances", [[0]*len(docs)])[0]
        for doc, meta, dist in zip(docs, metas, dists):
            # Cosine distance < 1.4 tolerates slight paraphrasing while rejecting noise
            if dist < 1.4:
                metadata = (meta or {}).copy()
                metadata["_distance"] = float(dist)
                chunks.append(Result(page_content=doc, metadata=metadata))
    return chunks


def merge_chunks(chunks1: list[Result], chunks2: list[Result]) -> list[Result]:
    """Deduplicate and merge two result lists."""
    merged = chunks1[:]
    existing = {c.page_content for c in chunks1}
    for c in chunks2:
        if c.page_content not in existing:
            merged.append(c)
    return merged


def should_rewrite_query(question: str) -> bool:
    """Only rewrite verbose conversational queries to avoid wasting LLM latency on clean queries."""
    stripped = question.strip()
    words = stripped.split()
    if len(words) <= 3:
        return False
    lowered = stripped.lower()
    vague_triggers = ("tell me about", "can you explain", "give me details on", "i want to know", "what can you tell me", "look up", "find information")
    return any(trigger in lowered for trigger in vague_triggers)


def rewrite_query(question: str, provider: str | None = None) -> str:
    """Rewrite conversational questions into concise queries when needed."""
    if not provider or not should_rewrite_query(question):
        return question

    prompt = (
        "You are searching a knowledge base.\n"
        "Rewrite the following question into a short, precise search query (one sentence max).\n"
        "Respond ONLY with the search query, nothing else.\n\n"
        f"Question: {question}"
    )
    try:
        response = get_llm(provider).invoke([SystemMessage(content=prompt)])
        rewritten = response.content.strip()
        return rewritten if rewritten else question
    except Exception:
        return question


def fetch_context_batch_unranked(queries: list[str], session_id: str | None = None) -> list[Result]:
    """Batch embed multiple queries and retrieve top-k chunks from Chroma in a single vector call."""
    coll = get_arbitrary_collection(session_id) if session_id else get_collection()
    unique_queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
    if not unique_queries:
        return []

    try:
        vectors = get_embedder().encode(unique_queries, normalize_embeddings=True).tolist()
        results = coll.query(
            query_embeddings=vectors,
            n_results=RETRIEVAL_K,
            include=["documents", "metadatas", "distances"]
        )
    except Exception as e:
        print(f"Batch retrieval error: {e}")
        return []

    all_chunks: list[Result] = []
    seen = set()

    for doc_list, meta_list, dist_list in zip(
        results.get("documents", []),
        results.get("metadatas", []),
        results.get("distances", [])
    ):
        for doc, meta, dist in zip(doc_list, meta_list, dist_list):
            if doc in seen:
                continue
            if dist < 1.4:
                seen.add(doc)
                metadata = (meta or {}).copy()
                metadata["_distance"] = float(dist)
                all_chunks.append(Result(page_content=doc, metadata=metadata))

    return all_chunks


# Words that signal a question depends on prior context
_FOLLOWUP_SIGNALS = {
    "it", "its", "that", "this", "those", "these", "they", "their",
    "more", "else", "also", "too", "above", "mentioned", "said",
    "same", "such", "previous", "earlier",
}


def condense_question(question: str, history: list[dict], provider: str) -> str:
    """
    If the question looks like a follow-up (uses pronouns or vague references),
    rewrite it as a fully self-contained standalone query using conversation history.
    Falls back to the original question on any error.
    """
    if not history:
        return question

    words = set(question.lower().split())
    is_followup = bool(words & _FOLLOWUP_SIGNALS) or len(question.split()) <= 5
    if not is_followup:
        return question

    # Use the last 2 turns (4 messages) for context
    recent = [
        m for m in history[-4:]
        if isinstance(m.get("content"), str)
    ]
    if not recent:
        return question

    history_text = "\n".join(
        f"{m['role'].upper()}: {m['content'][:300]}" for m in recent
    )
    prompt = (
        "Given the conversation history below and a follow-up question, "
        "rewrite the follow-up as a fully self-contained standalone question. "
        "Resolve all pronouns and references using the history context. "
        "Respond ONLY with the rewritten question, nothing else.\n\n"
        f"Conversation:\n{history_text}\n\n"
        f"Follow-up: {question}\n\n"
        "Standalone question:"
    )
    try:
        response = get_llm(provider).invoke([SystemMessage(content=prompt)])
        condensed = response.content.strip()
        return condensed if condensed else question
    except Exception:
        return question


def rerank(question: str, chunks: list[Result]) -> list[Result]:
    """Rerank chunks deterministically using keyword overlap."""
    if len(chunks) <= 1:
        return chunks

    terms = {t for t in re.findall(r"[A-Za-z0-9]+", question.lower()) if len(t) > 1 and t not in _STOP_WORDS}
    term_patterns = [re.compile(rf"\b{re.escape(t)}\b") for t in terms]

    def score(chunk: Result) -> tuple[int, int, str]:
        text = chunk.page_content.lower()
        source = chunk.metadata.get("source", "").lower()
        return (
            sum(1 for pattern in term_patterns if pattern.search(text)),
            sum(1 for pattern in term_patterns if pattern.search(source)),
            chunk.metadata.get("source", ""),
        )

    return sorted(chunks, key=score, reverse=True)


def fallback_web_search(query: str) -> list[Result]:
    """Search the web when local context is missing."""
    html_results = _fallback_web_search_html(query)
    if html_results:
        return html_results

    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = ddgs.text(query, region="us-en", max_results=WEB_SEARCH_K)
        return [
            Result(
                page_content=r.get("body", ""),
                metadata={"source": r.get("href", "Web Search"), "doc_type": "web"}
            )
            for r in (results or [])
        ]
    except Exception as e:
        print(f"Web search failed: {e}")
        return []


def _fallback_web_search_html(query: str) -> list[Result]:
    """Fallback HTML scraping path when ddgs cannot complete HTTPS requests."""
    try:
        import certifi
        import requests
        from bs4 import BeautifulSoup

        response = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            },
            timeout=15,
            verify=certifi.where(),
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        for node in soup.select(".result")[:WEB_SEARCH_K]:
            link = node.select_one(".result__a")
            snippet = node.select_one(".result__snippet")
            if not link:
                continue
            href = _unwrap_search_result_url(link.get("href", "").strip())
            body = snippet.get_text(" ", strip=True) if snippet else ""
            if href:
                results.append(
                    Result(
                        page_content=body,
                        metadata={"source": href, "doc_type": "web"},
                    )
                )
        return results
    except Exception as e:
        print(f"HTML web search fallback failed: {e}")
        return []


def _unwrap_search_result_url(url: str) -> str:
    """Extract the real destination from DuckDuckGo redirect URLs."""
    if not url:
        return ""

    if url.startswith("//"):
        url = f"https:{url}"
    elif url.startswith("/"):
        url = f"https://duckduckgo.com{url}"

    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com"):
        params = parse_qs(parsed.query)
        target = params.get("uddg", [""])[0]
        if target:
            return unquote(target)
    return url


def _filter_relevant_chunks(question: str, rewritten: str, chunks: list[Result]) -> list[Result]:
    """Keep only reranked chunks that still overlap meaningfully with the query or meet semantic confidence."""
    reranked = rerank(question, chunks)[:FINAL_K]
    if not reranked:
        return []

    combined_query = f"{question} {rewritten}"
    terms = {t for t in re.findall(r"[A-Za-z0-9]+", combined_query.lower()) if len(t) > 1 and t not in _STOP_WORDS}
    if not terms:
        return reranked

    term_patterns = [re.compile(rf"\b{re.escape(t)}\b") for t in terms]
    has_overlap = any(
        any(pattern.search(c.page_content.lower()) for pattern in term_patterns)
        for c in reranked
    )
    if has_overlap:
        return reranked

    # Fallback to semantic distance: preserve chunks if top chunk has high embedding similarity
    best_dist = min((c.metadata.get("_distance", 2.0) for c in reranked), default=2.0)
    if best_dist < 0.95:
        return reranked

    return []


def fetch_context(question: str, session_id: str | None = None, provider: str | None = None) -> list[Result]:
    """Full RAG retrieval: batch semantic search and lexical rerank."""
    rewritten = rewrite_query(question, provider) if provider else question
    queries = [question] if rewritten == question else [question, rewritten]
    chunks = fetch_context_batch_unranked(queries, session_id)
    if not chunks:
        return []
    return _filter_relevant_chunks(question, rewritten, chunks)


def fetch_context_with_options(
    question: str,
    session_id: str | None = None,
    provider: str | None = None,
    allow_web_fallback: bool = False,
    is_custom_mode: bool = False,
) -> list[Result]:
    """Retrieve context with optional suppression of web fallback for custom sessions."""
    rewritten = rewrite_query(question, provider) if provider else question
    queries = [question] if rewritten == question else [question, rewritten]
    # In custom mode, query session DB; in default mode, query built-in DB
    target_session = session_id if is_custom_mode else None
    chunks = fetch_context_batch_unranked(queries, target_session)
    
    if not chunks:
        if allow_web_fallback:
            print("No context found. Falling back to web search...")
            return fallback_web_search(question)
        return []

    filtered = _filter_relevant_chunks(question, rewritten, chunks)
    if not filtered and allow_web_fallback:
        print("Retrieved context irrelevant. Falling back to web search...")
        return fallback_web_search(question)
    return filtered


def build_context(chunks: list[Result], max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """Keep the prompt small enough for local models with tight context limits."""
    parts = []
    used = 0
    for chunk in chunks:
        snippet = f"[{chunk.metadata.get('source', 'unknown')}]\n{chunk.page_content}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(snippet) > remaining:
            snippet = snippet[:remaining].rsplit(" ", 1)[0].rstrip() + "..."
        parts.append(snippet)
        used += len(snippet) + 2
    return "\n\n".join(parts)


def answer_directly(
    question: str,
    history: list[dict] | None = None,
    provider: str = "Groq - Llama 3.3 70B (Free)",
    active_context: str = "",
) -> tuple[str, list]:
    """Answer without retrieval for math, coding, summaries, and general questions."""
    if history is None:
        history = []

    context = active_context.strip() or "None"
    messages = [SystemMessage(content=DIRECT_ANSWER_PROMPT.format(context=context))]
    messages.extend(convert_to_messages(history))
    messages.append(HumanMessage(content=question))

    try:
        response = get_llm(provider).invoke(messages)
        return response.content, []
    except Exception as e:
        err_str = str(e)
        if "not configured" in err_str.lower() or "not set" in err_str.lower() or "api key" in err_str.lower():
            return (
                f"**AI Model Not Configured for '{provider}'**\n\n"
                "Please configure an API key in your Hugging Face Space Settings (Variables and secrets) "
                "or local `.env` (e.g. `GROQ_API_KEY`, `MISTRAL_API_KEY`, or `OPENAI_API_KEY`).",
                [],
            )
        return (
            f"**Could not reach '{provider}'** — {err_str[:200]}\n\n"
            "Please check your API key or try a different provider.",
            [],
        )


def answer_from_web_search(
    question: str,
    history: list[dict] | None = None,
    provider: str = "Groq - Llama 3.3 70B (Free)",
) -> tuple[str, list]:
    """Answer using live web search results only."""
    if history is None:
        history = []

    chunks = fallback_web_search(question)
    if not chunks:
        return (
            "The web search did not return a reliable answer. Try rephrasing or adding a relevant document.",
            [],
        )

    context = build_context(chunks)
    messages = [SystemMessage(content=WEB_FALLBACK_PROMPT.format(context=context))]
    messages.extend(convert_to_messages(history))
    messages.append(HumanMessage(content=question))

    try:
        response = get_llm(provider).invoke(messages)
        answer_text = response.content
    except Exception as e:
        err_str = str(e)
        answer_text = (
            f"**Could not reach '{provider}'** — {err_str[:200]}\n\n"
            "---\n*Here are the most relevant web results found:*\n\n"
            f"{context}"
        )

    docs = [Document(page_content=c.page_content, metadata=c.metadata) for c in chunks]
    return answer_text, docs


def classify_input(text: str, provider: str = "Local (LM Studio)") -> str:
    """
    Decide whether the input is document context to store or a question to answer.
    Returns: 'context' | 'query'
    """
    stripped = text.strip()

    # Fast heuristics — no LLM round-trip needed
    if len(stripped) < 100:
        return "query"
    if len(stripped) > 500:
        return "context"
    if stripped.endswith("?"):
        return "query"

    # LLM classification for ambiguous medium-length text (100–500 chars)
    prompt = (
        "Classify the following user input for a document Q&A system.\n"
        "Reply with ONLY one word: \"context\" if it is a chunk of document text "
        "or factual information meant to be stored, or \"query\" if it is a question, "
        "command, or conversational message.\n\n"
        f"Input: {stripped[:400]}"
    )
    try:
        response = get_llm(provider).invoke([SystemMessage(content=prompt)])
        result = response.content.strip().lower()
        return "context" if "context" in result else "query"
    except Exception:
        question_starts = {
            "what", "why", "how", "when", "where", "who", "is", "are",
            "can", "could", "would", "should", "does", "do", "tell",
            "explain", "describe", "list", "give", "show", "find", "get",
        }
        first_word = stripped.lower().split()[0] if stripped else ""
        return "query" if first_word in question_starts else "context"


_ANALYTICAL_KEYWORDS = {
    "compare", "comparison", "difference", "versus", "vs",
    "most", "least", "highest", "lowest", "average", "total",
    "trend", "trends", "aggregate", "distribution", "breakdown",
    "pattern", "patterns", "rank", "ranking", "how many", "all products", "across",
    "strategic", "revenue",
}

def is_analytical_question(question: str) -> bool:
    """Fast keyword/heuristic intent classifier that requires zero extra LLM API roundtrips."""
    lowered = question.lower()
    return any(re.search(rf"\b{re.escape(k)}\b", lowered) for k in _ANALYTICAL_KEYWORDS)


ANALYTICAL_DIRECTIVE = """
7. **STRUCTURED SYNTHESIS & COMPARISON**:
   - Because this question involves analyzing, comparing, ranking, or aggregating across multiple items or documents, first present a clear, concise Markdown table or structured bullet list organizing the relevant facts found in the Context.
   - Then, provide your direct analysis answering the question based strictly on the table.
   - If data for some items is missing from the Context, explicitly state the limitation.
"""


def answer_question(
    question: str,
    history: list[dict] | None = None,
    session_id: str | None = None,
    provider: str = "Groq - Llama 3.3 70B (Free)",
    allow_web_fallback: bool = False,
    is_custom_mode: bool = False,
) -> tuple[str, list]:
    """Answer a question using the optimized RAG pipeline. Returns (answer, context_docs)."""
    from core.session_ingest import ingest_document

    if history is None:
        history = []

    # Resolve follow-up questions into standalone queries for retrieval.
    retrieval_query = condense_question(question, history, provider)

    chunks = fetch_context_with_options(
        retrieval_query,
        session_id=session_id,
        provider=provider,
        allow_web_fallback=allow_web_fallback,
        is_custom_mode=is_custom_mode
    )

    # If nothing was found anywhere, return immediately without calling the LLM
    if not chunks:
        if session_id:
            if allow_web_fallback:
                msg = (
                    "This topic was not found in your uploaded documents, "
                    "and the web search did not return any reliable results either. "
                    "Try uploading a more relevant document or rephrasing your question."
                )
            else:
                msg = (
                    "This question doesn't appear to be covered by your uploaded documents. "
                    "Try asking something specific about the content you uploaded, "
                    "or upload a more relevant document."
                )
        else:
            if allow_web_fallback:
                msg = (
                    "This topic was not found in the provided documents, "
                    "and the web search did not return any reliable results either. "
                    "Try uploading a relevant document or rephrasing your question."
                )
            else:
                msg = (
                    "This topic was not found in the built-in documents. "
                    "Ask about the indexed knowledge base, switch to Custom mode and add relevant documents, "
                    "or rephrase your question with terms from the documents."
                )
        return msg, []

    context = build_context(chunks)
    is_web_fallback = all(c.metadata.get("doc_type") == "web" for c in chunks)

    # Automatically store web search results in the session database for future recall
    if is_web_fallback and session_id:
        for chunk in chunks:
            source = chunk.metadata.get("source", "Web Search")
            ingest_document(chunk.page_content, source, session_id)

    if is_web_fallback:
        system_prompt = WEB_FALLBACK_PROMPT.format(context=context)
    else:
        system_prompt = SYSTEM_PROMPT.format(context=context)
        if is_analytical_question(retrieval_query):
            system_prompt += f"\n{ANALYTICAL_DIRECTIVE}\n"

    messages = [SystemMessage(content=system_prompt)]
    messages.extend(convert_to_messages(history))
    messages.append(HumanMessage(content=question))

    try:
        response = get_llm(provider).invoke(messages)
        answer_text = response.content
    except Exception as e:
        err_str = str(e)
        if "not configured" in err_str.lower() or "not set" in err_str.lower() or "api key" in err_str.lower():
            answer_text = (
                f"**AI Model Not Configured for '{provider}'**\n\n"
                "To generate AI answers, please configure an API key in your Hugging Face Space Settings "
                "(Settings -> Variables and secrets) or in your `.env`:\n"
                "- **Groq (Free & Fast)**: Set `GROQ_API_KEY` (free key at [console.groq.com](https://console.groq.com))\n"
                "- **Mistral (Free Tier)**: Set `MISTRAL_API_KEY` (free key at [console.mistral.ai](https://console.mistral.ai))\n"
                "- **OpenAI**: Set `OPENAI_API_KEY`\n\n"
                "---\n*Retrieved Document Passages from Knowledge Base:*\n\n"
                f"{context}"
            )
        elif "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
            answer_text = (
                f"**Rate limit reached for '{provider}'.**\n\n"
                "Your free-tier quota is exhausted for this model. "
                "Please switch to a different provider using the dropdown above, "
                "or wait a moment for the rate limit window to reset.\n\n"
                "---\n*Here is the retrieved context found in the meantime:*\n\n"
                f"{context}"
            )
        else:
            answer_text = (
                f"**Could not reach '{provider}'** — {err_str[:200]}\n\n"
                "Please check your API key or select another provider.\n\n"
                "---\n*Here is the most relevant context found:*\n\n"
                f"{context}"
            )

    docs = [Document(page_content=c.page_content, metadata=c.metadata) for c in chunks]
    return answer_text, docs
