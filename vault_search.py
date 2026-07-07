"""
vault_search.py
----------------
Pure-Python BM25 fallback searcher over the Obsidian vault (wiki/ +
journal/ + inbox/ markdown). Exists because the vault's own retrieval
pipeline (scripts/retrieve.py -> bm25-index.py) hard-imports fcntl,
which does not exist on Windows -- ask_brain tries the vault pipeline
first and falls back here.

- Index cache lives OUTSIDE the vault (~/.jarvis_vault_index.json) so
  Jarvis never writes vault infrastructure it doesn't own.
- No services, no network: tokenize -> BM25 (k1=1.5, b=0.75) -> top-k.
- ensure_index() is cheap when fresh; heartbeat refreshes it nightly
  and search() self-heals if the cache is missing or stale.
"""

import json
import math
import re
import time
from pathlib import Path

INDEX_PATH = Path.home() / ".jarvis_vault_index.json"
INDEX_MAX_AGE_SECONDS = 24 * 3600
CHUNK_TARGET_CHARS = 700
MIN_CHUNK_CHARS = 40
K1, B = 1.5, 0.75

_token_re = re.compile(r"[a-z0-9']+")


def _tokenize(text):
    return _token_re.findall(text.lower())


def _strip_frontmatter(text):
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def _iter_chunks(vault_root):
    """Yields (relative_path, chunk_text) over the three searchable
    folders, grouping paragraphs to ~CHUNK_TARGET_CHARS."""
    vault_root = Path(vault_root)
    for sub in ("wiki", "journal", "inbox"):
        d = vault_root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.md")):
            try:
                text = _strip_frontmatter(p.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            rel = p.relative_to(vault_root).as_posix()
            buf = []
            size = 0
            for para in re.split(r"\n\s*\n", text):
                para = para.strip()
                if not para:
                    continue
                buf.append(para)
                size += len(para)
                if size >= CHUNK_TARGET_CHARS:
                    chunk = "\n\n".join(buf)
                    if len(chunk) >= MIN_CHUNK_CHARS:
                        yield rel, chunk
                    buf, size = [], 0
            if buf:
                chunk = "\n\n".join(buf)
                if len(chunk) >= MIN_CHUNK_CHARS:
                    yield rel, chunk


def build_index(vault_root):
    """Full rebuild. Returns the index dict (also written to INDEX_PATH)."""
    chunks = []
    df = {}
    for rel, text in _iter_chunks(vault_root):
        tokens = _tokenize(text)
        tf = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        for t in tf:
            df[t] = df.get(t, 0) + 1
        chunks.append({"path": rel, "text": text, "tf": tf, "len": len(tokens)})

    avgdl = (sum(c["len"] for c in chunks) / len(chunks)) if chunks else 0.0
    index = {
        "built_at": time.time(),
        "vault_root": str(vault_root),
        "n": len(chunks),
        "avgdl": avgdl,
        "df": df,
        "chunks": chunks,
    }
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    return index


def _load_index(vault_root):
    try:
        index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        if index.get("vault_root") != str(vault_root):
            return None  # vault moved/changed -- rebuild
        return index
    except (OSError, json.JSONDecodeError):
        return None


def ensure_index(vault_root, max_age=INDEX_MAX_AGE_SECONDS):
    """Builds the index if missing, stale, or pointed at a different
    vault. Cheap no-op when fresh. Safe to call from the heartbeat."""
    index = _load_index(vault_root)
    if index is None or time.time() - index.get("built_at", 0) > max_age:
        return build_index(vault_root)
    return index


def search(vault_root, query, top_k=5):
    """BM25 top-k. Returns [{"page_path", "snippet", "score"}]."""
    index = ensure_index(vault_root)
    n = index["n"]
    if n == 0:
        return []
    avgdl = index["avgdl"] or 1.0
    df = index["df"]
    q_tokens = set(_tokenize(query))

    scored = []
    for c in index["chunks"]:
        score = 0.0
        for t in q_tokens:
            f = c["tf"].get(t)
            if not f:
                continue
            idf = math.log(1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
            score += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * c["len"] / avgdl))
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)

    return [
        {"page_path": c["path"], "snippet": c["text"][:800], "score": round(s, 3)}
        for s, c in scored[:top_k]
    ]
