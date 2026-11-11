"""FAISS vector index over KB chunks, saved to disk with a manifest.

IndexFlatIP = exact inner-product search. On normalised vectors that's cosine similarity.
Exact search is right for ~50 chunks; at millions you'd switch to an approximate index
(IVF / HNSW) and trade a little recall for speed, or move to a managed store such as
Azure AI Search, which adds hybrid (keyword + vector) search and metadata filters.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import faiss
import numpy as np

from triage.config import Settings

from .chunking import Chunk
from .embeddings import Embedder


def index_dir(settings: Settings) -> Path:
    """One index per embedding provider (data/kb_index/gemini, data/kb_index/hash, ...),
    so an offline build never overwrites the real one."""
    return Path(settings.kb_index_dir) / settings.embed_provider


class IndexMismatchError(RuntimeError):
    """The saved index was built with a different embedder: vectors aren't comparable."""


@dataclass
class Hit:
    id: str
    article_id: str
    title: str
    section: str
    category: str
    group: str
    score: float
    text: str


class KBIndex:
    def __init__(self, index: faiss.Index, chunks: list[dict[str, Any]], manifest: dict) -> None:
        self._index = index
        self.chunks = chunks
        self.manifest = manifest

    @classmethod
    def build(cls, chunks: list[Chunk], embedder: Embedder) -> "KBIndex":
        vectors = embedder.documents([c.text for c in chunks])
        index = faiss.IndexFlatIP(vectors.shape[1])
        index.add(vectors)
        digest = hashlib.sha256("\n".join(c.text for c in chunks).encode()).hexdigest()[:16]
        manifest = {
            "embedder": embedder.name,
            "dims": int(vectors.shape[1]),
            "chunks": len(chunks),
            "articles": len({c.article_id for c in chunks}),
            "kb_hash": digest,  # changes when any article text changes: rebuild then
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        return cls(index, [c.to_dict() for c in chunks], manifest)

    def save(self, directory: str | Path) -> None:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(path / "index.faiss"))
        (path / "chunks.json").write_text(json.dumps(self.chunks, indent=1), encoding="utf-8")
        (path / "manifest.json").write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, directory: str | Path, embedder: Embedder) -> "KBIndex":
        path = Path(directory)
        if not (path / "manifest.json").exists():
            raise FileNotFoundError(f"No KB index in {path}. Run: uv run triage-kb build")
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if manifest["embedder"] != embedder.name:
            raise IndexMismatchError(
                f"Index built with {manifest['embedder']}, but the configured embedder is "
                f"{embedder.name}. Rebuild with: uv run triage-kb build"
            )
        chunks = json.loads((path / "chunks.json").read_text(encoding="utf-8"))
        return cls(faiss.read_index(str(path / "index.faiss")), chunks, manifest)

    def search(
        self,
        query_vector: np.ndarray,
        k: int = 4,
        *,
        min_score: float = 0.0,
        per_article: int = 2,
    ) -> list[Hit]:
        """Top-k chunks by cosine similarity, at most `per_article` chunks from any one
        article (so one long article can't crowd out the others)."""
        candidates = min(len(self.chunks), k * 4)
        scores, ids = self._index.search(np.asarray(query_vector, "float32")[None, :], candidates)
        hits: list[Hit] = []
        taken: dict[str, int] = {}
        for score, i in zip(scores[0], ids[0], strict=True):
            if i < 0 or score < min_score:
                continue
            chunk = self.chunks[i]
            if taken.get(chunk["article_id"], 0) >= per_article:
                continue
            taken[chunk["article_id"]] = taken.get(chunk["article_id"], 0) + 1
            hits.append(Hit(score=round(float(score), 4), **chunk))
            if len(hits) == k:
                break
        return hits
