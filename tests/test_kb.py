"""Phase 3: knowledge-base chunking, the FAISS index and the retrieval quality gate.

The gate uses the offline hash embedder (a lexical baseline), so CI needs no API key.
Real embeddings are measured locally with:  uv run triage-kb eval
"""

from collections import Counter
from pathlib import Path

import pytest

from mock_snow.schema import CATEGORY
from mock_snow.seed import GROUPS
from tests.conftest import REPO
from triage.config import Settings
from triage.kb.chunking import load_chunks, parse_article
from triage.kb.embeddings import HashEmbedder, LangChainEmbedder, make_embedder
from triage.kb.evaluate import evaluate, load_cases
from triage.kb.store import IndexMismatchError, KBIndex
from triage.llm import LLMConfigError

KB_DIR = REPO / "kb"
CASES = REPO / "evals" / "retrieval.jsonl"


@pytest.fixture(scope="module")
def index() -> KBIndex:
    return KBIndex.build(load_chunks(KB_DIR), HashEmbedder())


# --- articles and chunks ---------------------------------------------------------------


def test_every_article_has_valid_metadata() -> None:
    paths = sorted(KB_DIR.glob("*.md"))
    assert len(paths) >= 15
    for path in paths:
        article = parse_article(path)
        assert article.id.startswith("KB") and article.sections
        assert article.category in CATEGORY, f"{path.name}: unknown category"
        assert article.group in GROUPS, f"{path.name}: group must be a real assignment group"


def test_chunks_follow_sections_with_a_contextual_header() -> None:
    chunks = load_chunks(KB_DIR)
    safety = next(c for c in chunks if c.id == "KB0008#safety-first")
    assert safety.text.startswith("Swollen or overheating laptop battery - Safety first")
    assert (safety.article_id, safety.group) == ("KB0008", "End User Computing")
    assert len({c.id for c in chunks}) == len(chunks)  # citation ids are unique


def test_long_sections_are_split_with_overlap(tmp_path: Path) -> None:
    paragraphs = [f"Step {i}: " + "word " * 60 for i in range(1, 6)]  # ~310 chars each
    (tmp_path / "KB9001.md").write_text(
        "---\nid: KB9001\ntitle: Long one\ncategory: inquiry\ngroup: Service Desk\n---\n"
        "# Long one\n\n## Steps\n\n" + "\n\n".join(paragraphs) + "\n",
        encoding="utf-8",
    )
    chunks = load_chunks(tmp_path, max_chars=700)
    assert [c.id for c in chunks][:2] == ["KB9001#steps-1", "KB9001#steps-2"]
    first, second = chunks[0].text, chunks[1].text
    last_paragraph_of_first = first.split("\n\n")[-1]
    assert last_paragraph_of_first in second  # one paragraph of overlap


def test_frontmatter_is_required(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_text("# No frontmatter\n\n## A\n\ntext\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        load_chunks(tmp_path)


# --- index -----------------------------------------------------------------------------------


def test_index_survives_a_save_and_load(index: KBIndex, tmp_path: Path) -> None:
    embedder = HashEmbedder()
    index.save(tmp_path)
    loaded = KBIndex.load(tmp_path, embedder)
    hits = loaded.search(embedder.query("the printer on my floor says offline"), k=4)
    assert hits[0].article_id == "KB0009"
    assert loaded.manifest["chunks"] == len(loaded.chunks)


def test_one_article_cannot_fill_every_slot(index: KBIndex) -> None:
    hits = index.search(HashEmbedder().query("swollen battery bulging trackpad hot"), k=4)
    assert max(Counter(h.article_id for h in hits).values()) <= 2


def test_min_score_drops_weak_matches(index: KBIndex) -> None:
    assert index.search(HashEmbedder().query("zebra quantum violin"), k=4, min_score=0.2) == []


def test_an_index_from_another_embedder_is_refused(tmp_path: Path) -> None:
    KBIndex.build(load_chunks(KB_DIR), HashEmbedder(dims=1024)).save(tmp_path)
    with pytest.raises(IndexMismatchError, match="Rebuild"):
        KBIndex.load(tmp_path, HashEmbedder(dims=512))


def test_embedder_config() -> None:
    with pytest.raises(LLMConfigError, match="GEMINI_API_KEY"):
        make_embedder(Settings(_env_file=None, embed_provider="gemini"))
    gemini = make_embedder(Settings(_env_file=None, gemini_api_key="k"))
    assert (
        isinstance(gemini, LangChainEmbedder) and gemini.name == "gemini:gemini-embedding-001:768"
    )
    assert isinstance(make_embedder(Settings(_env_file=None, embed_provider="hash")), HashEmbedder)


# --- retrieval quality gate ---------------------------------------------------------------


def test_eval_cases_point_at_real_articles() -> None:
    ids = {c.article_id for c in load_chunks(KB_DIR)}
    for case in load_cases(CASES):
        assert set(case["relevant"]) <= ids, case


def test_retrieval_quality_gate(index: KBIndex) -> None:
    report = evaluate(index, HashEmbedder(), load_cases(CASES), k=4)
    misses = [(c.query, c.retrieved) for c in report.misses]
    assert report.hit_rate >= 0.9, misses
    assert report.mrr >= 0.85
