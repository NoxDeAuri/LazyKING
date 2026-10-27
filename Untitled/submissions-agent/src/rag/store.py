"""
Vector store — ChromaDB wrapper for submission history.

ChromaDB runs fully locally; data lives at data/chroma/ (gitignored).
Embeddings are pre-computed by embedder.py and passed directly to Chroma
(no built-in embedding function needed, so chromadb alone is sufficient
without the heavier chromadb[default] extras).

Each document represents one work+call pairing with its outcome:
  text     = synopsis + themes + call description (what we embed and search)
  metadata = work_id, call, listing_url, outcome, date
  id       = "{work_id}::{listing_url or call}"  (upsert-safe)
"""

from pathlib import Path
from typing import Optional

import chromadb
from ..telemetry.spans import span as _span

_CHROMA_DIR = Path(__file__).parent.parent.parent / "data" / "chroma"
_COLLECTION = "submissions"

_client:     Optional[chromadb.PersistentClient] = None
_collection: Optional[chromadb.Collection]       = None


def _get_collection() -> chromadb.Collection:
    global _client, _collection
    if _collection is None:
        _CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        _client     = chromadb.PersistentClient(path=str(_CHROMA_DIR))
        _collection = _client.get_or_create_collection(
            name=_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def upsert(
    doc_id:    str,
    text:      str,
    embedding: list[float],
    metadata:  dict,
) -> None:
    """Insert or replace a document. Call after every response_received event."""
    col = _get_collection()
    col.upsert(
        ids=[doc_id],
        documents=[text],
        embeddings=[embedding],
        metadatas=[metadata],
    )


def search(
    query_embedding: list[float],
    k:               int = 5,
    where:           Optional[dict] = None,
) -> list[dict]:
    """
    Return the k most similar past submissions.

    Each result dict:
      id, text, metadata (work_id, call, outcome, date), distance
    """
    with _span("rag.search", rag_k=k) as s:
        col = _get_collection()
        if col.count() == 0:
            s.set_attribute("rag.result_count", 0)
            return []

        kwargs: dict = {
            "query_embeddings": [query_embedding],
            "n_results":        min(k, col.count()),
            "include":          ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        results = col.query(**kwargs)

        output = []
        for i, doc_id in enumerate(results["ids"][0]):
            output.append({
                "id":       doc_id,
                "text":     results["documents"][0][i],
                "metadata": results["metadatas"][0][i],
                "distance": results["distances"][0][i],
            })

        s.set_attribute("rag.result_count", len(output))
        s.set_attribute("rag.store_size", col.count())
        return output


def count() -> int:
    return _get_collection().count()
