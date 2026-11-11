"""Knowledge-base commands.

    uv run triage-kb build                 # chunk kb/*.md, embed, save the FAISS index
    uv run triage-kb search "vpn keeps dropping"
    uv run triage-kb eval --min-hit-rate 0.9   # exit code 1 below the threshold (CI)

Add --provider hash to any command to run fully offline (lexical baseline, no API key).
"""

import argparse
import sys

from triage.config import Settings, get_settings
from triage.llm import LLMConfigError

from .chunking import load_chunks
from .embeddings import make_embedder
from .evaluate import evaluate, load_cases
from .store import KBIndex, index_dir


def _settings(args: argparse.Namespace) -> Settings:
    settings = get_settings()
    if args.provider:
        settings = settings.model_copy(update={"embed_provider": args.provider})
    return settings


def cmd_build(args: argparse.Namespace) -> None:
    settings = _settings(args)
    chunks = load_chunks(settings.kb_dir)
    embedder = make_embedder(settings)
    index = KBIndex.build(chunks, embedder)
    out = index_dir(settings)
    index.save(out)
    m = index.manifest
    print(f"Indexed {m['chunks']} chunks from {m['articles']} articles with {m['embedder']}")
    print(f"Saved to {out}")


def cmd_search(args: argparse.Namespace) -> None:
    settings = _settings(args)
    embedder = make_embedder(settings)
    index = KBIndex.load(index_dir(settings), embedder)
    for hit in index.search(embedder.query(args.query), k=args.k):
        print(f"{hit.score:.3f}  {hit.id:<32} {hit.title} / {hit.section}")


def cmd_eval(args: argparse.Namespace) -> None:
    settings = _settings(args)
    embedder = make_embedder(settings)
    index = KBIndex.load(index_dir(settings), embedder)
    report = evaluate(index, embedder, load_cases(args.cases), k=args.k)
    for case in report.cases:
        mark = "ok  " if case.rank == 1 else (f"r={case.rank} " if case.rank else "MISS")
        print(f"{mark} {case.query[:70]:<70} -> {', '.join(case.retrieved[:3])}")
    name = index.manifest["embedder"]
    print(f"\n{name}: hit@{report.k}={report.hit_rate:.2f}  MRR={report.mrr:.2f}")
    if report.hit_rate < args.min_hit_rate:
        sys.exit(
            f"hit@{report.k} {report.hit_rate:.2f} is below --min-hit-rate {args.min_hit_rate}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="triage-kb", description="Knowledge base for RAG")
    parser.add_argument("--provider", choices=["gemini", "azure_openai", "hash"])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("build", help="chunk, embed and index kb/*.md").set_defaults(fn=cmd_build)

    search = sub.add_parser("search", help="query the index")
    search.add_argument("query")
    search.add_argument("-k", type=int, default=4)
    search.set_defaults(fn=cmd_search)

    ev = sub.add_parser("eval", help="retrieval hit@k and MRR on labelled questions")
    ev.add_argument("--cases", default="evals/retrieval.jsonl")
    ev.add_argument("-k", type=int, default=4)
    ev.add_argument("--min-hit-rate", type=float, default=0.0)
    ev.set_defaults(fn=cmd_eval)

    args = parser.parse_args()
    try:
        args.fn(args)
    except (LLMConfigError, FileNotFoundError) as err:
        sys.exit(f"Error: {err}")


if __name__ == "__main__":
    main()
