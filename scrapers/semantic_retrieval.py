"""
Semantic Retrieval — finds the most relevant chunks from the embedding index.

At query time:
1. Embed the user's query via Ollama (one call, ~100ms on CPU)
2. Compute cosine similarity against all stored vectors (numpy, <1ms for ~5K chunks)
3. Return top-K most relevant text chunks

Falls back to the old tail-truncation approach if:
- The embedding index doesn't exist yet
- Ollama is not running (and can't be started briefly)
"""

import os
import json
import logging
import subprocess
import time
import threading

import httpx
import numpy as np

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from config import STATE_DIR
from scrapers.embedding_indexer import (
    DIM,
    INDEX_SCHEMA_VERSION,
    MAX_INDEX_BYTES,
    MAX_METADATA_BYTES,
    MODEL_FINGERPRINT,
)

INDEX_PATH = os.path.join(STATE_DIR, "embedding_data", "embedding_index.npz")
METADATA_PATH = os.path.join(STATE_DIR, "embedding_data", "embedding_metadata.json")
EMBED_MODEL = "nomic-embed-text"

# How many chunks to inject into the bot's prompt
DEFAULT_TOP_K = 8
MAX_TOP_K = 50
MIN_RELEVANCE_SCORE = 0.20
# Maximum total characters of retrieved context (don't blow up the prompt)
MAX_CONTEXT_CHARS = 12000


# ── Index loading (lazy, cached with TTL) ──────────────────────────────────────────────

# Bounded index cache with TTL (5 minutes)
_index_cache = None
_index_cache_timestamp = 0
_index_cache_signature = None
_INDEX_CACHE_TTL = 300  # 5 minutes
_index_cache_lock = threading.Lock()


def _load_index() -> tuple[np.ndarray, list[str], list[str]] | None:
    """Load the embedding index from disk. Cached with TTL."""
    global _index_cache, _index_cache_timestamp, _index_cache_signature
    now = time.time()
    with _index_cache_lock:
        try:
            signature = (
                os.stat(INDEX_PATH).st_mtime_ns,
                os.stat(INDEX_PATH).st_size,
                os.stat(METADATA_PATH).st_mtime_ns,
                os.stat(METADATA_PATH).st_size,
            )
        except OSError:
            return None
        if (
            _index_cache is not None
            and signature == _index_cache_signature
            and (now - _index_cache_timestamp) < _INDEX_CACHE_TTL
        ):
            return _index_cache
        try:
            if signature[1] > MAX_INDEX_BYTES or signature[3] > MAX_METADATA_BYTES:
                raise ValueError("embedding index exceeds configured size limit")
            with open(METADATA_PATH, "r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            with np.load(INDEX_PATH, allow_pickle=False) as data:
                vectors = np.asarray(data["vectors"])
                generation_id = str(np.asarray(data["generation_id"]).item())
                vector_fingerprint = str(np.asarray(data["model_fingerprint"]).item())
            chunks = metadata.get("chunks")
            sources = metadata.get("sources")
            if metadata.get("schema_version") != INDEX_SCHEMA_VERSION:
                raise ValueError("unsupported embedding index schema")
            if metadata.get("generation_id") != generation_id:
                raise ValueError("embedding vector/metadata generation mismatch")
            if metadata.get("model_fingerprint") != MODEL_FINGERPRINT or vector_fingerprint != MODEL_FINGERPRINT:
                raise ValueError("embedding model fingerprint mismatch")
            if metadata.get("dimension") != DIM:
                raise ValueError("embedding dimension mismatch")
            if not isinstance(chunks, list) or not all(isinstance(item, str) for item in chunks):
                raise ValueError("embedding chunks metadata is invalid")
            if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
                raise ValueError("embedding sources metadata is invalid")
            if vectors.dtype != np.float32 or vectors.ndim != 2 or vectors.shape[1:] != (DIM,):
                raise ValueError(f"invalid embedding vector dtype/shape: {vectors.dtype} {vectors.shape}")
            if vectors.shape[0] != len(chunks) or len(chunks) != len(sources):
                raise ValueError("embedding vectors/chunks/sources counts differ")
            if len(vectors) == 0:
                return None
            if not np.isfinite(vectors).all():
                raise ValueError("embedding index contains NaN or infinity")
            # Normalize vectors once for cosine similarity
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if np.any(norms == 0):
                raise ValueError("embedding index contains zero vectors")
            vectors = vectors / norms
            _index_cache = (vectors, chunks, sources)
            _index_cache_timestamp = now
            _index_cache_signature = signature
            logger.info(f"Loaded index: {len(chunks)} chunks, {vectors.shape}")
            return _index_cache
        except Exception as e:
            logger.warning(f"Failed to load index: {e}")
            return None


def invalidate_cache():
    """Call this after the index is rebuilt."""
    global _index_cache, _index_cache_timestamp, _index_cache_signature
    with _index_cache_lock:
        _index_cache = None
        _index_cache_timestamp = 0
        _index_cache_signature = None


# ── Query embedding ───────────────────────────────────────────────────────────

# Shared httpx client for connection pooling
_ollama_client = None
_ollama_client_lock = threading.Lock()


def _get_ollama_url() -> str:
    """Get Ollama URL from config (loaded from .env)."""
    from config import OLLAMA_URL
    return OLLAMA_URL


def _get_ollama_client() -> httpx.Client:
    global _ollama_client
    with _ollama_client_lock:
        if _ollama_client is None:
            timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)
            _ollama_client = httpx.Client(timeout=timeout)
        return _ollama_client


def _ollama_is_running() -> bool:
    """Check if Ollama is reachable."""
    try:
        client = _get_ollama_client()
        resp = client.get(f"{_get_ollama_url()}/api/tags")
        return resp.status_code == 200
    except Exception:
        return False


# ── Background-start state (single-spawn gating) ──────────────────────────────────────────────────────────────────
_ollama_start_lock = threading.Lock()
_ollama_start_attempted = False

# Success-stays-True gate: flag latches True for the process lifetime
# after a successful warm. Ollama crashes mid-session do NOT recover
# (restart the bot). This is stricter than pre-bbdfce9 (which would
# re-Popen on the next embed_query after a mid-session crash), accepted
# for sub-ms cold-start. Failure / timeout in _start_ollama_async reset
# the flag under _ollama_start_lock so the next cold-start can retry.


def _start_ollama_async() -> None:
    """Background thread target. Popen + readiness poll OFF the hot
    request path so embed_query's caller doesn't block on cold start.
    Resets _ollama_start_attempted on failure/timeout so a later query
    can try again; on success the flag stays True to suppress repeated
    redundant starts while Ollama is up.
    """
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(10):  # wait up to 10s for readiness
            time.sleep(1)
            if _ollama_is_running():
                logger.info("Ollama started in background.")
                return
        logger.warning("Ollama background start did not become ready in 10s.")
    except (OSError, FileNotFoundError) as e:
        logger.warning(f"Failed to start Ollama in background: {e}")
    # On failure/timeout: reset the gate so a later query can retry.
    with _ollama_start_lock:
        global _ollama_start_attempted
        _ollama_start_attempted = False


def _start_ollama() -> bool:
    """Fast-path gate-spawn. Returns False immediately so embed_query
    can fall back to non-semantic retrieval. The daemon thread spawned
    inside does the actual Popen + readiness poll on its own time.
    """
    with _ollama_start_lock:
        global _ollama_start_attempted
        if not _ollama_start_attempted:
            _ollama_start_attempted = True
            threading.Thread(target=_start_ollama_async, daemon=True).start()
    return False


def embed_query(query: str) -> np.ndarray | None:
    """Embed a single query string via Ollama. Returns float32 vector of shape (DIM,).
    Starts Ollama if needed. Returns None if embedding fails.
    """
    if not isinstance(query, str) or not query.strip():
        return None

    # Try to start Ollama if not running
    if not _ollama_is_running():
        logger.info("Ollama not running, attempting to start...")
        if not _start_ollama():
            logger.warning("Could not start Ollama, falling back to non-semantic retrieval")
            return None

    try:
        client = _get_ollama_client()
        resp = client.post(
            f"{_get_ollama_url()}/api/embed",
            json={"model": EMBED_MODEL, "input": query},
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0),
        )
        if resp.status_code != 200:
            logger.warning(f"Embedding failed: {resp.status_code}")
            return None
        data = resp.json()
        embeddings = data.get("embeddings", [])
        if embeddings and len(embeddings[0]) == DIM:
            vec = np.array(embeddings[0], dtype=np.float32)
            if not np.isfinite(vec).all():
                logger.warning("Query embedding contained NaN or infinity")
                return None
            # Normalize for cosine similarity
            norm = np.linalg.norm(vec)
            if not np.isfinite(norm) or norm <= 0:
                logger.warning("Query embedding had zero/invalid norm")
                return None
            return vec / norm
    except httpx.TimeoutException:
        logger.warning("Query embedding timeout")
        return None
    except Exception as e:
        logger.warning(f"Query embedding error: {e}")
    return None


# ── Retrieval ──────────────────────────────────────────────────────────────────

def semantic_search(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    *,
    min_score: float = MIN_RELEVANCE_SCORE,
) -> list[dict]:
    """Search the embedding index for chunks most similar to the query.

    Returns list of {"text": str, "source": str, "score": float},
    sorted by descending similarity.
    """
    if not isinstance(query, str) or not query.strip():
        return []
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        return []
    if not isinstance(min_score, (int, float)) or not np.isfinite(min_score):
        return []
    min_score = float(max(-1.0, min(1.0, min_score)))

    index = _load_index()
    if index is None:
        return []

    vectors, chunks, sources = index

    query_vec = embed_query(query)
    if query_vec is None:
        return []

    # Cosine similarity (vectors already normalized)
    scores = vectors @ query_vec  # dot product == cosine similarity for unit vectors

    # Get top-K
    top_k = min(top_k, MAX_TOP_K, len(chunks))
    eligible = np.flatnonzero(np.isfinite(scores) & (scores >= min_score))
    if not len(eligible):
        return []
    ranked = eligible[np.argsort(scores[eligible])[::-1]]
    top_indices = ranked[:top_k]

    results = []
    for idx in top_indices:
        results.append({
            "text": chunks[idx],
            "source": sources[idx],
            "score": float(scores[idx]),
        })

    return results


def get_context_for_prompt(query: str, top_k: int = DEFAULT_TOP_K) -> str:
    """Get formatted context string for injection into the bot's system prompt.

    This is the main entry point called from main.py.
    Returns a formatted string of relevant chunks, or empty string if
    semantic retrieval is unavailable.
    """
    results = semantic_search(query, top_k=top_k)
    if not results:
        return ""

    context_parts = []
    total_chars = 0

    for r in results:
        source_name = os.path.basename(r["source"])
        score_pct = r["score"] * 100
        entry = f"[{source_name} (relevance: {score_pct:.0f}%)]\n{r['text']}"
        if total_chars + len(entry) > MAX_CONTEXT_CHARS:
            break
        context_parts.append(entry)
        total_chars += len(entry)

    if not context_parts:
        return ""

    header = f"=== SEMANTIC RETRIEVAL (top {len(context_parts)} chunks for: \"{query[:80]}\") ==="
    return header + "\n\n" + "\n\n---\n\n".join(context_parts) + "\n\n=== END RETRIEVAL ==="


# ── Fallback: old tail-truncation method ─────────────────────────────────────────

def get_fallback_context() -> tuple[str, str]:
    """Return (brain_context, digest_context) using the old tail-truncation method.
    Used when semantic retrieval is unavailable.
    """
    brain_context = "No offline memory consolidated yet."
    digest_context = "No recent data available."

    from config import CURATED_BRAIN_FILE, LATEST_DIGEST_FILE, MEGA_INDEX_FILE

    brain_file = os.fspath(CURATED_BRAIN_FILE)
    if os.path.exists(brain_file):
        with open(brain_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            brain_context = content[-15000:] if len(content) > 15000 else content

    mega_file = os.fspath(MEGA_INDEX_FILE)
    if os.path.exists(mega_file):
        with open(mega_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            # Combine: last 5K of mega index instead of separate
            if len(content) > 5000:
                brain_context += "\n\n--- Recent Index Entries ---\n" + content[-5000:]
            else:
                brain_context += "\n\n--- Recent Index Entries ---\n" + content

    digest_file = os.fspath(LATEST_DIGEST_FILE)
    if os.path.exists(digest_file):
        with open(digest_file, "r", encoding="utf-8", errors="replace") as f:
            digest_context = f.read()

    return brain_context, digest_context
