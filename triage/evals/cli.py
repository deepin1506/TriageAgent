"""Evaluate the agent on the labelled tickets.

    uv run triage-eval                       # all tickets, real LLM, gate on evals/thresholds.json
    uv run triage-eval --only E018,E039      # just a few
    uv run triage-eval --limit 10 --sleep 4  # first 10, pausing between tickets (rate limits)
    uv run triage-eval --no-gate             # report only, never fail

Needs LLM_MODEL + GEMINI_API_KEY and a built KB index (uv run triage-kb build). It starts
its own mock ServiceNow, so mock-snow does not need to be running.
Writes evals/results/latest.json and evals/results/latest.md.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from triage.config import get_settings
from triage.llm import LLMConfigError, make_chat_model

from .dataset import load_cases
from .metrics import check, score
from .runner import run_eval

LABELS = {
    "category_accuracy": "Category accuracy",
    "impact_accuracy": "Impact accuracy",
    "urgency_accuracy": "Urgency accuracy",
    "priority_accuracy": "Priority accuracy (derived)",
    "group_accuracy": "Assignment group accuracy",
    "tool_call_accuracy": "Tool calls exactly right",
    "end_state_accuracy": "Ticket correct in ServiceNow afterwards",
    "kb_hit_rate": "Relevant KB article retrieved",
    "citation_precision": "Citations that are relevant",
    "citation_hallucination_rate": "Proposals citing sources not retrieved",
    "injection_recall": "Injection attempts flagged",
    "injection_false_positive_rate": "Clean tickets wrongly flagged",
    "injection_resistance": "Injected tickets still routed correctly",
    "latency_p50_s": "Latency p50 (s)",
    "latency_p95_s": "Latency p95 (s)",
    "errors": "Runs that crashed",
}


def _show(value: object) -> str:
    return "n/a" if value is None else str(value)


def markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        f"# Eval report ({report['model']}, {report['when']})",
        "",
        f"{s['cases']} labelled tickets, run end to end with automatic approval.",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    lines += [f"| {label} | {_show(s[key])} |" for key, label in LABELS.items()]
    misses = [r for r in report["rows"] if not (r["category"] and r["group"] and r["priority"])]
    if misses:
        lines += ["", "## Misses", "", "| Ticket | Predicted | Debatable label |", "|---|---|---|"]
        for r in misses:
            p = r["predicted"]
            got = f"{p['category']} / {p['group']} / impact {p['impact']}, urgency {p['urgency']}"
            lines.append(f"| {r['id']} | {r['error'] or got} | {'yes' if r['debatable'] else ''} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(prog="triage-eval", description=__doc__.split("\n")[0])
    parser.add_argument("--cases", default="evals/tickets.csv")
    parser.add_argument("--thresholds", default="evals/thresholds.json")
    parser.add_argument("--only", help="comma-separated ticket ids, e.g. E001,E018")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds between tickets")
    parser.add_argument("--out", default="evals/results")
    parser.add_argument("--no-gate", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    try:
        llm = make_chat_model(settings)
    except LLMConfigError as err:
        sys.exit(f"Config error: {err}")

    cases = load_cases(args.cases)
    if args.only:
        wanted = {i.strip() for i in args.only.split(",")}
        cases = [c for c in cases if c.id in wanted]
    if args.limit:
        cases = cases[: args.limit]

    results = asyncio.run(
        run_eval(cases, lambda _: llm, settings, sleep_s=args.sleep, progress=print)
    )
    scored = score(cases, results)
    report = {
        "model": f"{settings.llm_provider}:{settings.llm_model}",
        "embedder": f"{settings.embed_provider}:{settings.embed_model}",
        "when": datetime.now(UTC).isoformat(timespec="seconds"),
        **scored,
        "results": [r.to_dict() for r in results],
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "latest.md").write_text(markdown(report), encoding="utf-8")

    print()
    for key, label in LABELS.items():
        print(f"{label:<42} {_show(scored['summary'][key])}")
    print(f"\nReport: {out / 'latest.md'}")

    if args.no_gate:
        return
    thresholds = json.loads(Path(args.thresholds).read_text(encoding="utf-8"))
    failures = check(scored["summary"], thresholds)
    if failures:
        sys.exit("EVAL GATE FAILED:\n  " + "\n  ".join(failures))
    print("Eval gate passed.")


if __name__ == "__main__":
    main()
