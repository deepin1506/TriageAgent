"""Retrieval evaluation: does the right article come back for a realistic question?

Metrics, at ARTICLE level over the top-k chunks the agent would actually see:
  hit@k  share of questions where a relevant article appears in the top k
  MRR    mean of 1/rank of the first relevant article (1.0 = always ranked first)
"""

import json
from dataclasses import dataclass
from pathlib import Path

from .embeddings import Embedder
from .store import KBIndex


@dataclass
class CaseResult:
    query: str
    relevant: list[str]
    retrieved: list[str]  # article ids in rank order
    rank: int | None  # 1-based rank of the first relevant article, None = miss


@dataclass
class RetrievalReport:
    k: int
    hit_rate: float
    mrr: float
    cases: list[CaseResult]

    @property
    def misses(self) -> list[CaseResult]:
        return [c for c in self.cases if c.rank is None]


def load_cases(path: str | Path) -> list[dict]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def evaluate(index: KBIndex, embedder: Embedder, cases: list[dict], k: int = 4) -> RetrievalReport:
    results = []
    for case in cases:
        hits = index.search(embedder.query(case["query"]), k=k)
        articles = list(dict.fromkeys(h.article_id for h in hits))  # de-duplicate, keep order
        rank = next((i + 1 for i, a in enumerate(articles) if a in case["relevant"]), None)
        results.append(CaseResult(case["query"], case["relevant"], articles, rank))
    n = len(results)
    hit_rate = sum(r.rank is not None for r in results) / n
    mrr = sum(1 / r.rank for r in results if r.rank) / n
    return RetrievalReport(k, round(hit_rate, 3), round(mrr, 3), results)
