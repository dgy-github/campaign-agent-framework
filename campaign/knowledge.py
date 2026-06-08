"""Minimal but REAL long-term retrieval (RAG) layer for the campaign framework.

⚠️  Honest scope: this is LEXICAL TF-IDF retrieval (pure Python), NOT neural embeddings.
    Neural embeddings need a model download + network dependency — out of scope.
    The Embedder ABC is the seam: swap in a dense embedder later without touching the store.

Core components:
  - Embedder (ABC)         — sparse term→weight vector interface
  - TfidfEmbedder          — incremental pure-Python TF-IDF with CJK-aware tokenizer
  - cosine(a, b)           — sparse cosine similarity
  - KnowledgeStore (ABC)   — add / search interface
  - SqliteKnowledgeStore   — stdlib sqlite3-backed, cross-process durable, deterministic

Design decisions (all intentional for this scope):
  - Vectors are sparse dicts (term→float), stored as JSON in sqlite.
    This keeps zero external deps and makes inspection/debugging trivial.
  - Incremental TF-IDF: each document's vector is computed with the df/N stats
    available at insertion time.  Older documents are NOT recomputed when new
    documents arrive.  This is standard online TF-IDF; for small-to-medium
    corpora the cosine ranking is still reasonable.
  - CJK tokenization: each CJK character is a separate token.  Latin text is
    split on non-alphanumeric boundaries.  This is a simple heuristic that
    works for mixed Chinese/English content without a dictionary.
  - Synchronous sqlite3 calls are fine for the expected document scale
    (thousands, not millions).  The caller (Retriever.handle) is async but
    the blocking is negligible.

No new dependencies: stdlib (abc, json, math, re, sqlite3) + existing pydantic only.
"""
from __future__ import annotations

import abc
import json
import math
import re
import sqlite3
from typing import Any


# ── CJK Unicode ranges used by the tokenizer ──────────────────────────────
_CJK_RANGES: list[tuple[int, int]] = [
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0xF900, 0xFAFF),   # CJK Compatibility Ideographs
]


def _is_cjk(c: str) -> bool:
    cp = ord(c)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def tokenize(text: str) -> list[str]:
    """Lowercase, split latin on non-alphanumeric boundaries, split each CJK char individually.

    Examples:
        "Hello World 你好世界"  ->  ["hello", "world", "你", "好", "世", "界"]
        "C++17 协程"           ->  ["c", "17", "协", "程"]
        "don't panic"          ->  ["don", "t", "panic"]   (known limitation: no stemming)
    """
    text = text.lower()
    # Build a character class that includes latin alphanumeric + CJK ranges.
    cjk_chars = "".join(
        chr(lo) + "-" + chr(hi) for lo, hi in _CJK_RANGES
    )
    split_re = re.compile(rf"[^a-z0-9{cjk_chars}]+")
    rough = split_re.split(text)

    tokens: list[str] = []
    for chunk in rough:
        if not chunk:
            continue
        buf: list[str] = []
        for c in chunk:
            if _is_cjk(c):
                if buf:
                    tokens.append("".join(buf))
                    buf.clear()
                tokens.append(c)
            else:
                buf.append(c)
        if buf:
            tokens.append("".join(buf))
    return [t for t in tokens if t]


# ── Embedder seam ──────────────────────────────────────────────────────────

class Embedder(abc.ABC):
    """Seam for future dense / neural embedders.  Current impl: sparse TF-IDF."""

    @abc.abstractmethod
    def embed(self, text: str) -> dict[str, float]:
        """Return a sparse term→weight vector for *text*."""
        ...


class TfidfEmbedder(Embedder):
    """Incremental pure-Python TF-IDF embedder with CJK-aware tokenizer.

    Usage pattern (standalone or driven by a KnowledgeStore):
        emb = TfidfEmbedder()
        emb.fit_add(doc1)   # update df + N (does NOT return a vector)
        emb.fit_add(doc2)
        v1 = emb.embed(doc1)  # now computes tf*idf with current stats
        v2 = emb.embed(doc2)
        score = cosine(v1, v2)

    idf formula:  log((N+1) / (df+1)) + 1   (add-1 smooth, avoids zeros)
    Output vectors are L2-normalized (unit vectors).
    """

    def __init__(self) -> None:
        self._df: dict[str, int] = {}   # term → document frequency
        self._N: int = 0                 # total documents seen

    # ── public API ────────────────────────────────────────────────────────

    def fit_add(self, text: str) -> None:
        """Incrementally incorporate *text* into the corpus statistics (df + N).

        Call this once per document BEFORE calling embed() for that document
        so the document contributes to its own idf computation.
        """
        tokens = tokenize(text)
        if not tokens:
            return
        self._N += 1
        seen: set[str] = set()
        for term in tokens:
            if term not in seen:
                seen.add(term)
                self._df[term] = self._df.get(term, 0) + 1

    def embed(self, text: str) -> dict[str, float]:
        """Compute L2-normalized tf*idf sparse vector for *text*.

        Returns {} when *text* yields zero tokens or the corpus is empty.
        """
        tokens = tokenize(text)
        if not tokens or self._N == 0:
            return {}

        # term frequency (raw count)
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1

        # tf*idf
        vector: dict[str, float] = {}
        for term, count in tf.items():
            df = self._df.get(term, 0)
            idf = math.log((self._N + 1) / (df + 1)) + 1.0
            vector[term] = count * idf

        # L2 normalization
        norm_sq = sum(v * v for v in vector.values())
        if norm_sq > 0:
            inv_norm = 1.0 / math.sqrt(norm_sq)
            for term in vector:
                vector[term] *= inv_norm

        return vector


# ── Dense-to-sparse conversion ──────────────────────────────────────────────

def dense_to_sparse(vec: list[float]) -> dict[str, float]:
    """将稠密向量转换为 L2 归一化稀疏向量 (keys "0", "1", ...)。

    零向量返回空 dict。
    """
    norm_sq = sum(v * v for v in vec)
    if norm_sq == 0.0:
        return {}
    inv_norm = 1.0 / math.sqrt(norm_sq)
    return {str(i): v * inv_norm for i, v in enumerate(vec)}


class LlmEmbedder:
    """异步神经 embedding，由 LLMClient 兼容对象驱动。

    不导入 campaign.llm.client —— 接受任意带 embed(inputs, tier) 方法的对象。
    """

    def __init__(self, client: object, tier: str) -> None:
        """
        Args:
            client: 带 ``async def embed(inputs, tier) -> list[list[float]]`` 的对象
            tier: 模型档位，传给 client.embed
        """
        self._client = client
        self._tier = tier

    async def aembed(self, text: str) -> dict[str, float]:
        """调用 client.embed 然后 dense_to_sparse。"""
        vectors = await self._client.embed(text, self._tier)  # type: ignore[union-attr]
        if not vectors:
            return {}
        return dense_to_sparse(vectors[0])


# ── Cosine similarity ─────────────────────────────────────────────────────

def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Sparse cosine similarity between two term→weight dicts.

    Both vectors are assumed L2-normalized (unit length), in which case
    cosine = dot(a, b).  The function is defensive and computes norms
    anyway so it also works with unnormalized vectors.
    """
    if not a or not b:
        return 0.0

    dot = sum(a[k] * b.get(k, 0.0) for k in a)

    norm_a_sq = sum(v * v for v in a.values())
    norm_b_sq = sum(v * v for v in b.values())
    if norm_a_sq == 0.0 or norm_b_sq == 0.0:
        return 0.0

    return dot / (math.sqrt(norm_a_sq) * math.sqrt(norm_b_sq))


# ── Knowledge store ───────────────────────────────────────────────────────

class KnowledgeStore(abc.ABC):
    """Abstract long-term document store with lexical retrieval."""

    @abc.abstractmethod
    def add(
        self,
        doc_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        vector: dict[str, float] | None = None,
    ) -> None:
        """Index a document.

        *metadata* may include ``"sensitive": true``.
        *vector* (opt-in): external pre-computed vector; when provided the store
        uses it directly instead of calling its internal embedder.
        """
        ...

    @abc.abstractmethod
    def search(
        self,
        query: str | None = None,
        k: int = 5,
        filters: dict[str, Any] | None = None,
        query_vector: dict[str, float] | None = None,
    ) -> list[dict[str, Any]]:
        """Return top-k items sorted by score desc.

        Each item is  ``{"doc_id", "text", "score", "metadata"}``.

        *query_vector* (opt-in): external pre-computed query vector; when provided
        the store uses it directly instead of embedding *query*.

        Supported *filters*:
          - ``sensitive_ok: bool``  (default False) — if False, sensitive docs are excluded.
        """
        ...


class SqliteKnowledgeStore(KnowledgeStore):
    """stdlib sqlite3-backed knowledge store with incremental TF-IDF indexing.

    - *db_path*: ``":memory:"`` for transient, or a file path for cross-process durable.
    - *embedder*: defaults to ``TfidfEmbedder()``; swap via the ``Embedder`` seam.

    Cross-process durability: when *db_path* is a file, closing and reopening
    with the same path restores the full corpus + tf-idf statistics; ``search()``
    returns identical results (deterministic for a given corpus).
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        embedder: Embedder | None = None,
    ) -> None:
        self.db_path = db_path
        self.embedder: Embedder = embedder if embedder is not None else TfidfEmbedder()
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")   # better concurrency for file dbs
        self._init_db()
        self._load_embedder_state()

    # ── schema ────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS docs (
                doc_id    TEXT PRIMARY KEY,
                text      TEXT NOT NULL,
                metadata  TEXT NOT NULL DEFAULT '{}',
                vector    TEXT NOT NULL DEFAULT '{}',
                sensitive INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self._conn.commit()

    # ── embedder state persistence ────────────────────────────────────────

    def _load_embedder_state(self) -> None:
        """Restore df/N from the meta table (only for TfidfEmbedder)."""
        if not isinstance(self.embedder, TfidfEmbedder):
            return  # custom embedder manages its own state

        row = self._conn.execute("SELECT value FROM meta WHERE key = 'N'").fetchone()
        if row is not None:
            self.embedder._N = int(row[0])

        row = self._conn.execute("SELECT value FROM meta WHERE key = 'df'").fetchone()
        if row is not None:
            self.embedder._df = json.loads(row[0])

    def _save_embedder_state(self) -> None:
        """Persist df/N to the meta table (only for TfidfEmbedder)."""
        if not isinstance(self.embedder, TfidfEmbedder):
            return

        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('N', ?)",
            (str(self.embedder._N),),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('df', ?)",
            (json.dumps(self.embedder._df, ensure_ascii=False),),
        )
        self._conn.commit()

    # ── public API ────────────────────────────────────────────────────────

    def add(
        self,
        doc_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        vector: dict[str, float] | None = None,
    ) -> None:
        """Index (or replace) a document.

        The ``"sensitive"`` key in *metadata* is extracted into a dedicated
        column for efficient filtering; it is removed from the stored metadata.

        *vector* (opt-in, M-E): external pre-computed vector. When provided,
        the store uses it directly — no internal embedder call and no TF-IDF
        stats update. Default (None) = current TF-IDF behavior unchanged.
        """
        meta = dict(metadata) if metadata else {}
        sensitive = int(bool(meta.pop("sensitive", False)))
        meta_json = json.dumps(meta, ensure_ascii=False)

        if vector is not None:
            # External vector: store directly, skip internal embedder entirely.
            vec = dict(vector)
            vector_json = json.dumps(vec, ensure_ascii=False)
        else:
            # Default path: internal embedder (TF-IDF or custom).
            fit_add = getattr(self.embedder, "fit_add", None)
            if fit_add is not None:
                fit_add(text)
            vec = self.embedder.embed(text)
            vector_json = json.dumps(vec, ensure_ascii=False)

        self._conn.execute(
            "INSERT OR REPLACE INTO docs (doc_id, text, metadata, vector, sensitive) "
            "VALUES (?, ?, ?, ?, ?)",
            (doc_id, text, meta_json, vector_json, sensitive),
        )
        self._conn.commit()

        # Persist updated df/N (only when using internal embedder)
        if vector is None:
            self._save_embedder_state()

    def search(
        self,
        query: str | None = None,
        k: int = 5,
        filters: dict[str, Any] | None = None,
        query_vector: dict[str, float] | None = None,
    ) -> list[dict[str, Any]]:
        """Return top-k documents sorted by cosine similarity.

        *query_vector* (opt-in, M-E): external pre-computed query vector; when
        provided the store uses it directly instead of embedding *query*.

        Supported *filters*:
          - ``sensitive_ok: bool`` — when False (default), documents marked sensitive are excluded.
        """
        if k <= 0:
            return []

        filters = dict(filters) if filters else {}
        sensitive_ok = bool(filters.get("sensitive_ok", False))

        if query_vector is not None:
            qvec = dict(query_vector)
        elif query is not None:
            qvec = self.embedder.embed(query)
        else:
            return []

        if not qvec:
            return []

        rows = self._conn.execute(
            "SELECT doc_id, text, metadata, vector, sensitive FROM docs"
        ).fetchall()

        results: list[dict[str, Any]] = []
        for doc_id, text, meta_json, vec_json, sensitive in rows:
            if not sensitive_ok and sensitive:
                continue
            doc_vec: dict[str, float] = json.loads(vec_json)
            score = cosine(qvec, doc_vec)
            meta: dict[str, Any] = json.loads(meta_json) if meta_json else {}
            results.append({
                "doc_id": doc_id,
                "text": text,
                "score": score,
                "metadata": meta,
            })

        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:k]

    # ── lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying sqlite3 connection."""
        self._conn.close()

    def __del__(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
