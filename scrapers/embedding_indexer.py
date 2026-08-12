"""
Embedding Indexer — builds a semantic vector index from the bot's knowledge corpus.

Runs as part of the 1 AM nightly pipeline (after Ollama is already awake).
Uses Ollama's nomic-embed-text model to embed text chunks, then saves
the index as a compressed numpy archive.

Index structure:
  - embedding_index.npz: float32 vectors plus non-object generation identifiers
  - embedding_metadata.json: chunks, sources, schema/model fingerprint
  - manifest.json: source checksums for incremental re-indexing

Metadata is deliberately JSON rather than pickled NumPy object arrays.
"""

import os
import json
import glob
import hashlib
import logging
import asyncio
import shutil
import tempfile
import time
import uuid

import httpx
import numpy as np

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from config import STATE_DIR

INDEX_DIR = os.path.join(STATE_DIR, "embedding_data")
INDEX_PATH = os.path.join(INDEX_DIR, "embedding_index.npz")
METADATA_PATH = os.path.join(INDEX_DIR, "embedding_metadata.json")
MANIFEST_PATH = os.path.join(INDEX_DIR, "manifest.json")

# Use local Ollama (has nomic-embed-text, works without Orange Pi)
# OLLAMA_URL loaded from config.py (which reads from .env)
from config import OLLAMA_URL
OLLAMA_URL = OLLAMA_URL
EMBED_MODEL = "nomic-embed-text"
CHUNK_SIZE = 1000  # tokens (approximate: 1 token ~ 4 chars, so ~4000 chars)
CHUNK_OVERLAP = 200  # tokens overlap between chunks (~800 chars)
BATCH_SIZE = 4  # embed this many chunks per API call (small for slow CPU)
DIM = 768  # nomic-embed-text dimensionality
INDEX_SCHEMA_VERSION = 2
MAX_INDEX_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024 * 1024
MODEL_FINGERPRINT = hashlib.sha256(
    f"{INDEX_SCHEMA_VERSION}:{EMBED_MODEL}:{DIM}".encode("utf-8")
).hexdigest()


# ── Text chunking ──────────────────────────────────────────────────────────────

def chunk_text(text: str, source: str = "") -> list[dict]:
    """Split text into overlapping chunks of ~CHUNK_SIZE tokens.
    Returns list of {"text": str, "source": str}.
    Uses simple char-based approximation: 1 token ≈ 4 chars.
    """
    char_size = CHUNK_SIZE * 4
    char_overlap = CHUNK_OVERLAP * 4
    chunks = []

    # Split by paragraphs first for cleaner boundaries
    paragraphs = text.split("\n\n")
    current_chunk = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if len(current_chunk) + len(para) + 2 <= char_size:
            current_chunk += ("\n\n" if current_chunk else "") + para
        else:
            # Flush current chunk
            if current_chunk.strip():
                chunks.append({"text": current_chunk.strip(), "source": source})
            # Start new chunk, carry over overlap from end of previous
            overlap_text = current_chunk[-char_overlap:] if char_overlap and len(current_chunk) > char_overlap else ""
            current_chunk = overlap_text + ("\n\n" if overlap_text else "") + para

            # If single paragraph exceeds chunk size, hard-split it
            while len(current_chunk) > char_size * 1.5:
                chunks.append({"text": current_chunk[:char_size].strip(), "source": source})
                current_chunk = current_chunk[char_size - char_overlap:]

    if current_chunk.strip():
        chunks.append({"text": current_chunk.strip(), "source": source})

    return chunks


# ── Source collection ──────────────────────────────────────────────────────────

def collect_sources() -> list[dict]:
    """Walk all text sources and return list of {"path": str, "content": str, "md5": str}."""
    sources = []

    def _add_file(filepath: str):
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if not content.strip():
                return
            md5 = hashlib.md5(content.encode()).hexdigest()
            sources.append({"path": filepath, "content": content, "md5": md5})
        except Exception as e:
            logger.warning(f"Failed to read {filepath}: {e}")

    # Knowledge base
    kb_dir = os.path.join(BASE_DIR, "knowledge_base")
    if os.path.isdir(kb_dir):
        for f in sorted(glob.glob(os.path.join(kb_dir, "*.md"))):
            _add_file(f)

    # Study guides — only index the first 20KB of each (they're generated content,
    # and the most important info is in the headers/summaries at the top)
    sg_dir = os.path.join(BASE_DIR, "study_guides")
    if os.path.isdir(sg_dir):
        for f in sorted(glob.glob(os.path.join(sg_dir, "*.md"))):
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    full_content = fh.read()
                    content = full_content[:20000]  # first 20KB has the overview
                if not content.strip():
                    continue
                md5 = hashlib.md5(full_content.encode()).hexdigest()
                sources.append({"path": f, "content": content, "md5": md5})
            except Exception as e:
                logger.warning(f"Failed to read {f}: {e}")

    # Mega index — only index the last 50KB (most recent/relevant entries)
    mega_path = os.path.join(BASE_DIR, "mega_index.md")
    if os.path.exists(mega_path):
        try:
            with open(mega_path, "r", encoding="utf-8", errors="replace") as f:
                full_content = f.read()
            if full_content.strip():
                content = full_content
                # Take last 50KB for most recent context
                if len(content) > 50000:
                    content = "[...earlier entries omitted...]\n" + content[-50000:]
                md5 = hashlib.md5(full_content.encode()).hexdigest()
                sources.append({"path": mega_path, "content": content, "md5": md5})
        except Exception as e:
            logger.warning(f"Failed to read {mega_path}: {e}")

    # Curated brain
    _add_file(os.path.join(BASE_DIR, "curated_brain.md"))

    # Classroom PDFs (downloaded PDFs are saved as .txt after OCR)
    classroom_dir = os.path.join(BASE_DIR, "classroom_pdfs")
    if os.path.isdir(classroom_dir):
        for f in sorted(glob.glob(os.path.join(classroom_dir, "*.txt"))):
            _add_file(f)

    # Source cache summaries
    from config import CACHE_DIR
    sc_dir = CACHE_DIR
    legacy_sc_dir = os.path.join(BASE_DIR, "scrapers", "source_cache")
    for d in [sc_dir, legacy_sc_dir]:
        if os.path.isdir(d):
            if d == legacy_sc_dir:
                logger.warning(f"Reading stale legacy cache from {legacy_sc_dir}")
            for f in sorted(glob.glob(os.path.join(d, "*.txt"))):
                _add_file(f)

    # Chat histories
    for f in sorted(glob.glob(os.path.join(BASE_DIR, "chat_history_*.txt"))):
        _add_file(f)

    # Important extracts
    _add_file(os.path.join(BASE_DIR, "important_extracts.txt"))

    # Learning rules
    _add_file(os.path.join(BASE_DIR, "learning_rules.txt"))

    logger.info(f"Collected {len(sources)} source files")
    return sources


# ── Ollama embedding ───────────────────────────────────────────────────────────

async def ensure_model():
    """Pull nomic-embed-text if not already present."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            if resp.status_code == 200:
                models = [m["name"] for m in resp.json().get("models", [])]
                if any(EMBED_MODEL in m for m in models):
                    logger.info(f"Model {EMBED_MODEL} already present")
                    return
        logger.info(f"Pulling {EMBED_MODEL} (first run only, ~274 MB)...")
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=5.0)) as client:
            resp = await client.post(f"{OLLAMA_URL}/api/pull", json={"name": EMBED_MODEL}, timeout=httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=5.0))
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to pull {EMBED_MODEL}: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to ensure model {EMBED_MODEL}: {type(e).__name__}: {e}")
        raise


async def embed_texts(texts: list[str]) -> np.ndarray:
    """Embed texts via Ollama or raise without manufacturing fake vectors."""
    if not texts:
        return np.empty((0, DIM), dtype=np.float32)
    all_embeddings = []

    # Use shared async client for connection pooling
    from utils import get_async_httpx_client
    client = get_async_httpx_client()

    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]

        # Retry logic for slow CPU
        for attempt in range(3):
            try:
                resp = await client.post(
                    f"{OLLAMA_URL}/api/embed",
                    json={"model": EMBED_MODEL, "input": batch},
                    timeout=httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=5.0),
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"Embedding failed: {resp.status_code} {resp.text[:200]}")

                data = resp.json()
                embeddings = np.asarray(data.get("embeddings", []), dtype=np.float32)
                if embeddings.shape != (len(batch), DIM):
                    raise RuntimeError(
                        f"Invalid embedding shape for batch {i // BATCH_SIZE}: "
                        f"expected {(len(batch), DIM)}, got {embeddings.shape}"
                    )
                if not np.isfinite(embeddings).all():
                    raise RuntimeError(f"Non-finite embedding returned for batch {i // BATCH_SIZE}")
                if np.any(np.linalg.norm(embeddings, axis=1) == 0):
                    raise RuntimeError(f"Zero embedding returned for batch {i // BATCH_SIZE}")
                all_embeddings.extend(embeddings)
                break  # success
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.HTTPStatusError) as e:
                if attempt < 2:
                    logger.warning(f"Timeout on batch {i//BATCH_SIZE} (attempt {attempt+1}/3): {type(e).__name__}, retrying...")
                    await asyncio.sleep(5)
                else:
                    raise RuntimeError(
                        f"Embedding batch {i // BATCH_SIZE} failed after 3 attempts"
                    ) from e
            except Exception as e:
                logger.error(f"Unexpected error on batch {i//BATCH_SIZE}: {type(e).__name__}: {e}")
                if attempt < 2:
                    await asyncio.sleep(5)
                else:
                    raise RuntimeError(
                        f"Embedding batch {i // BATCH_SIZE} failed after 3 attempts"
                    ) from e

        if i + BATCH_SIZE < len(texts):
            logger.info(f"  Embedded {i + len(batch)}/{len(texts)} chunks...")

    result = np.asarray(all_embeddings, dtype=np.float32)
    if result.shape != (len(texts), DIM) or not np.isfinite(result).all():
        raise RuntimeError(
            f"Embedding result validation failed: expected {(len(texts), DIM)}, got {result.shape}"
        )
    return result


# ── Incremental indexing ────────────────────────────────────────────────────────

def load_manifest() -> dict:
    """Load the manifest tracking source paths -> md5 checksums."""
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            if (
                isinstance(manifest, dict)
                and manifest.get("model_fingerprint") == MODEL_FINGERPRINT
                and isinstance(manifest.get("sources"), dict)
            ):
                return manifest
        except (OSError, json.JSONDecodeError, TypeError):
            logger.warning("Embedding manifest is invalid; a safe rebuild will be used")
    return {"sources": {}}


def _atomic_write_json(path: str, payload: dict) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def save_manifest(manifest: dict) -> None:
    _atomic_write_json(MANIFEST_PATH, manifest)


def _read_metadata(path: str = METADATA_PATH) -> dict:
    if not os.path.exists(path) or os.path.getsize(path) > MAX_METADATA_BYTES:
        raise ValueError("embedding metadata is missing or too large")
    with open(path, "r", encoding="utf-8") as stream:
        metadata = json.load(stream)
    if not isinstance(metadata, dict):
        raise ValueError("embedding metadata must be an object")
    return metadata


def _validate_loaded_index(
    vectors: np.ndarray,
    metadata: dict,
    generation_id: str,
    model_fingerprint: str,
) -> tuple[np.ndarray, list[str], list[str]]:
    chunks = metadata.get("chunks")
    sources = metadata.get("sources")
    if metadata.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise ValueError("unsupported embedding index schema")
    if metadata.get("model_fingerprint") != MODEL_FINGERPRINT:
        raise ValueError("embedding model fingerprint changed")
    if model_fingerprint != MODEL_FINGERPRINT:
        raise ValueError("vector archive model fingerprint mismatch")
    if metadata.get("generation_id") != generation_id:
        raise ValueError("embedding vector/metadata generation mismatch")
    if metadata.get("dimension") != DIM:
        raise ValueError("embedding dimension metadata mismatch")
    if not isinstance(chunks, list) or not all(isinstance(item, str) for item in chunks):
        raise ValueError("embedding chunks metadata is invalid")
    if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
        raise ValueError("embedding sources metadata is invalid")
    if vectors.dtype != np.float32 or vectors.ndim != 2 or vectors.shape[1:] != (DIM,):
        raise ValueError(f"embedding vectors have invalid dtype/shape: {vectors.dtype} {vectors.shape}")
    if vectors.shape[0] != len(chunks) or len(chunks) != len(sources):
        raise ValueError("embedding vectors/chunks/sources counts differ")
    if not np.isfinite(vectors).all():
        raise ValueError("embedding vectors contain NaN or infinity")
    if len(vectors) and np.any(np.linalg.norm(vectors, axis=1) == 0):
        raise ValueError("embedding vectors contain a zero row")
    return vectors, chunks, sources


def load_existing_index() -> tuple[np.ndarray | None, list[str], list[str]]:
    """Load a schema-v2 index without pickle; legacy archives are rebuilt."""
    if not os.path.exists(INDEX_PATH) or not os.path.exists(METADATA_PATH):
        if os.path.exists(INDEX_PATH):
            logger.warning("Legacy embedding archive detected; refusing pickle metadata and rebuilding")
        return None, [], []
    try:
        if os.path.getsize(INDEX_PATH) > MAX_INDEX_BYTES:
            raise ValueError("embedding archive exceeds size limit")
        metadata = _read_metadata()
        with np.load(INDEX_PATH, allow_pickle=False) as data:
            if "vectors" not in data or "generation_id" not in data or "model_fingerprint" not in data:
                raise ValueError("embedding archive is missing required arrays")
            vectors = np.asarray(data["vectors"])
            generation_id = str(np.asarray(data["generation_id"]).item())
            model_fingerprint = str(np.asarray(data["model_fingerprint"]).item())
        return _validate_loaded_index(vectors, metadata, generation_id, model_fingerprint)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Existing embedding index rejected; rebuilding safely: %s", exc)
        return None, [], []


def _stage_index(vectors: np.ndarray, chunks: list[str], sources: list[str]) -> tuple[str, str]:
    """Write and re-read a complete generation before it can replace live data."""
    generation_id = uuid.uuid4().hex
    metadata = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "generation_id": generation_id,
        "model": EMBED_MODEL,
        "model_fingerprint": MODEL_FINGERPRINT,
        "dimension": DIM,
        "chunks": chunks,
        "sources": sources,
    }
    os.makedirs(INDEX_DIR, mode=0o700, exist_ok=True)
    vector_fd, vector_path = tempfile.mkstemp(prefix=".tmp-index-", suffix=".npz", dir=INDEX_DIR)
    os.close(vector_fd)
    metadata_fd, metadata_path = tempfile.mkstemp(prefix=".tmp-metadata-", suffix=".json", dir=INDEX_DIR)
    os.close(metadata_fd)
    try:
        with open(vector_path, "wb") as stream:
            np.savez_compressed(
                stream,
                vectors=vectors,
                generation_id=np.asarray(generation_id),
                model_fingerprint=np.asarray(MODEL_FINGERPRINT),
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(vector_path, 0o600)
        with open(metadata_path, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(metadata_path, 0o600)

        staged_metadata = _read_metadata(metadata_path)
        with np.load(vector_path, allow_pickle=False) as staged:
            staged_vectors = np.asarray(staged["vectors"])
            staged_generation = str(np.asarray(staged["generation_id"]).item())
            staged_fingerprint = str(np.asarray(staged["model_fingerprint"]).item())
        _validate_loaded_index(
            staged_vectors,
            staged_metadata,
            staged_generation,
            staged_fingerprint,
        )
        return vector_path, metadata_path
    except Exception:
        for path in (vector_path, metadata_path):
            try:
                os.unlink(path)
            except OSError:
                pass
        raise


def _publish_staged_index(vector_path: str, metadata_path: str) -> None:
    """Publish only a fully validated generation, leaving failures untouched."""
    backups: dict[str, str] = {}
    published: set[str] = set()
    try:
        for live_path in (INDEX_PATH, METADATA_PATH):
            if os.path.exists(live_path):
                backup_fd, backup_path = tempfile.mkstemp(
                    prefix=".previous-", dir=INDEX_DIR
                )
                os.close(backup_fd)
                shutil.copy2(live_path, backup_path)
                backups[live_path] = backup_path
        os.replace(vector_path, INDEX_PATH)
        published.add(INDEX_PATH)
        os.replace(metadata_path, METADATA_PATH)
        published.add(METADATA_PATH)
        os.chmod(INDEX_PATH, 0o600)
        os.chmod(METADATA_PATH, 0o600)
    except Exception:
        for live_path in published:
            backup_path = backups.get(live_path)
            if backup_path and os.path.exists(backup_path):
                os.replace(backup_path, live_path)
            else:
                try:
                    os.unlink(live_path)
                except OSError:
                    pass
        raise
    finally:
        for path in (vector_path, metadata_path, *backups.values()):
            try:
                os.unlink(path)
            except OSError:
                pass


# ── Main pipeline ──────────────────────────────────────────────────────────────

async def build_index(force_rebuild: bool = False):
    """Build or incrementally update the embedding index.

    If force_rebuild, re-embeds everything from scratch.
    Otherwise, only re-embeds sources whose content has changed (md5 differs).
    """
    os.makedirs(INDEX_DIR, exist_ok=True)

    # Ensure Ollama is reachable
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            if resp.status_code != 200:
                raise ConnectionError()
    except Exception:
        logger.error("Ollama is not running at localhost:11434. Start it first.")
        return False

    try:
        await ensure_model()
    except Exception:
        return False

    # Collect all sources
    sources = collect_sources()
    if not sources:
        logger.warning("No sources found to index")
        return False

    # Load manifest and existing index
    manifest = load_manifest()
    existing_vectors, existing_chunks, existing_sources = load_existing_index()

    # Determine which sources need re-embedding
    source_map = {}  # path -> {"content": str, "md5": str}
    for s in sources:
        source_map[s["path"]] = s

    # Build list of chunks to keep vs re-embed
    if force_rebuild or existing_vectors is None:
        # Full rebuild
        logger.info("Full index rebuild requested")
        keep_indices = []
    else:
        keep_indices = []
        changed_paths = set()

        # Figure out which sources changed
        for i, src_path in enumerate(existing_sources):
            if src_path in source_map and source_map[src_path]["md5"] == manifest.get("sources", {}).get(src_path):
                # Unchanged — keep this chunk
                keep_indices.append(i)
            else:
                # Changed or deleted — will re-embed from the source file
                changed_paths.add(src_path)

        # Also find entirely new sources
        for s in sources:
            if s["path"] not in manifest.get("sources", {}):
                changed_paths.add(s["path"])

        logger.info(f"Keeping {len(keep_indices)} unchanged chunks, re-embedding {len(changed_paths)} changed/new sources")

    # Chunk all sources that need embedding
    all_new_chunks = []
    for s in sources:
        if force_rebuild or existing_vectors is None or s["path"] not in manifest.get("sources", {}) or manifest["sources"].get(s["path"]) != s["md5"]:
            chunks = chunk_text(s["content"], source=s["path"])
            all_new_chunks.extend(chunks)

    if not all_new_chunks and not keep_indices:
        logger.info("No chunks to index")
        return True

    # Embed new chunks
    new_vectors = None
    if all_new_chunks:
        texts = [c["text"] for c in all_new_chunks]
        logger.info(f"Embedding {len(texts)} new chunks via {EMBED_MODEL}...")
        start = time.time()
        try:
            new_vectors = await embed_texts(texts)
        except Exception as exc:
            logger.error("Embedding failed; preserving the last good index: %s", exc)
            return False
        elapsed = time.time() - start
        logger.info(f"Embedded {len(texts)} chunks in {elapsed:.1f}s ({len(texts)/elapsed:.1f} chunks/sec)")

    # Merge old + new
    if existing_vectors is not None and keep_indices:
        kept_vectors = existing_vectors[keep_indices]
        kept_chunks = [existing_chunks[i] for i in keep_indices]
        kept_sources = [existing_sources[i] for i in keep_indices]
    else:
        kept_vectors = np.empty((0, DIM), dtype=np.float32)
        kept_chunks = []
        kept_sources = []

    if new_vectors is not None and len(new_vectors) > 0:
        final_vectors = np.vstack([kept_vectors, new_vectors])
        final_chunks = kept_chunks + [c["text"] for c in all_new_chunks]
        final_sources = kept_sources + [c["source"] for c in all_new_chunks]
    else:
        final_vectors = kept_vectors if len(kept_vectors) > 0 else np.empty((0, DIM), dtype=np.float32)
        final_chunks = kept_chunks
        final_sources = kept_sources

    logger.info(f"Final index: {len(final_chunks)} chunks, {final_vectors.shape}")
    try:
        final_vectors, final_chunks, final_sources = _validate_loaded_index(
            np.asarray(final_vectors, dtype=np.float32),
            {
                "schema_version": INDEX_SCHEMA_VERSION,
                "generation_id": "prepublish",
                "model_fingerprint": MODEL_FINGERPRINT,
                "dimension": DIM,
                "chunks": final_chunks,
                "sources": final_sources,
            },
            "prepublish",
            MODEL_FINGERPRINT,
        )
        vector_path, metadata_path = _stage_index(final_vectors, final_chunks, final_sources)
        _publish_staged_index(vector_path, metadata_path)
    except Exception as exc:
        logger.error("Index validation/publication failed; previous generation retained: %s", exc)
        return False

    # Update manifest
    new_manifest = {"model_fingerprint": MODEL_FINGERPRINT, "sources": {}}
    for s in sources:
        new_manifest["sources"][s["path"]] = s["md5"]
    try:
        save_manifest(new_manifest)
    except OSError as exc:
        # The index remains valid.  A missing manifest merely forces a safe
        # full re-embed next run rather than risking stale chunk reuse.
        logger.warning("Index published but manifest update failed: %s", exc)

    logger.info(f"Index saved to {INDEX_PATH} ({os.path.getsize(INDEX_PATH)/1024:.1f} KB)")
    return True


# ── Bootstrap ───────────────────────────────────────────────────────────────────

async def rebuild_index_if_missing() -> bool:
    """Bootstrap helper for the nightly pipeline: if `embedding_index.npz`
    doesn't exist on disk, build it from scratch. Used at the start of the
    nightly pipeline (in `memory_consolidation.consolidate_memory`) so a
    freshly-cloned repo or a wiped `embedding_data/` dir doesn't leave
    semantic retrieval disabled until Phase 5's nightly rebuild.

    No-op + return True if the index is already present; rebuilds (via
    `build_index(force_rebuild=True)`) + returns its result otherwise.
    Logs at info when the bootstrap is skipped, warning when it fires.
    Idempotent on repeated calls.
    """
    existing_vectors, _, _ = load_existing_index()
    if existing_vectors is not None:
        logger.info(
            f"Embedding index already present at {INDEX_PATH} "
            f"({os.path.getsize(INDEX_PATH)/1024:.1f} KB); bootstrap skipped."
        )
        return True
    logger.warning(
        f"Embedding index missing at {INDEX_PATH}; running bootstrap rebuild..."
    )
    return await build_index(force_rebuild=True)


if __name__ == "__main__":
    asyncio.run(build_index())
