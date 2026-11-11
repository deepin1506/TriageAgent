"""Split KB articles into heading-aware chunks that carry their own metadata.

Why by heading: a KB section ("Symptoms", "Resolution", ...) is already one idea, so it
makes a better retrieval unit than a fixed character window that cuts mid-step.
Each chunk's text starts with "<title> - <section>" (a contextual header) so the
embedding knows what the passage is about even when the section body doesn't say it.
"""

import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    category: str
    group: str
    tags: list[str]
    sections: list[tuple[str, str]]  # (heading, body)
    path: str


@dataclass(frozen=True)
class Chunk:
    id: str  # "KB0001#resolution" (stable, used as the citation key)
    article_id: str
    title: str
    section: str
    category: str
    group: str
    text: str  # what gets embedded and shown

    def to_dict(self) -> dict:
        return asdict(self)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_article(path: Path) -> Article:
    """Parse `---` frontmatter (simple `key: value` lines), the `# ` title and `## ` sections."""
    raw = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    if not match:
        raise ValueError(f"{path}: missing --- frontmatter ---")
    meta = dict(line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)
    meta = {k.strip(): v.strip() for k, v in meta.items()}
    for required in ("id", "title", "category", "group"):
        if not meta.get(required):
            raise ValueError(f"{path}: frontmatter needs '{required}'")

    sections: list[tuple[str, str]] = []
    heading, lines = None, []
    for line in match.group(2).splitlines():
        if line.startswith("## "):
            if heading is not None:
                sections.append((heading, "\n".join(lines).strip()))
            heading, lines = line[3:].strip(), []
        elif heading is not None:
            lines.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(lines).strip()))

    return Article(
        id=meta["id"],
        title=meta["title"],
        category=meta["category"],
        group=meta["group"],
        tags=[t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
        sections=[s for s in sections if s[1]],
        path=str(path),
    )


def _windows(body: str, max_chars: int) -> list[str]:
    """Split an over-long section on paragraph breaks, repeating the last paragraph of
    each window at the start of the next (overlap keeps steps from losing context)."""
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    windows, current = [], []
    for para in paragraphs:
        if current and len("\n\n".join([*current, para])) > max_chars:
            windows.append("\n\n".join(current))
            current = [current[-1]]  # 1-paragraph overlap
        current.append(para)
    windows.append("\n\n".join(current))
    return windows


def chunk_article(article: Article, max_chars: int = 1200) -> list[Chunk]:
    chunks = []
    for heading, body in article.sections:
        parts = [body] if len(body) <= max_chars else _windows(body, max_chars)
        for i, part in enumerate(parts):
            suffix = f"-{i + 1}" if len(parts) > 1 else ""
            chunks.append(
                Chunk(
                    id=f"{article.id}#{_slug(heading)}{suffix}",
                    article_id=article.id,
                    title=article.title,
                    section=heading,
                    category=article.category,
                    group=article.group,
                    text=f"{article.title} - {heading}\n\n{part}",
                )
            )
    return chunks


def load_chunks(kb_dir: str | Path, max_chars: int = 1200) -> list[Chunk]:
    paths = sorted(Path(kb_dir).glob("*.md"))
    if not paths:
        raise FileNotFoundError(f"No .md articles in {kb_dir}")
    chunks = [c for p in paths for c in chunk_article(parse_article(p), max_chars)]
    ids = [c.id for c in chunks]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"Duplicate chunk ids (repeated headings?): {sorted(duplicates)}")
    return chunks
