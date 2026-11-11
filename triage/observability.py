"""Run records, the JSONL run log, and the numbers behind /metrics.

One line per finished run (applied, rejected, blocked or crashed), built from the
checkpointed state and its trace, so the CLI and the API log exactly the same thing.
Crashed runs are logged too: a log that only records successes hides the failures.
"""

import json
import logging
import statistics
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from triage.config import Settings

log = logging.getLogger(__name__)


def outcome(state: dict[str, Any], error: str | None = None) -> str:
    if error:
        return "error"
    if state.get("guard", {}).get("blocked"):
        return "blocked"
    decision = state.get("decision", {})
    if not decision:
        return "incomplete"
    if not decision.get("approved"):
        return "rejected"
    results = state.get("results", [])
    if not results:
        return "nothing_to_apply"
    return "applied" if all(r["ok"] for r in results) else "failed"


def _end(event: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(event["started_at"]) + timedelta(milliseconds=event["ms"])


def approval_wait_s(trace: list[dict[str, Any]]) -> float | None:
    """Seconds between the proposal being ready (check_policy ends) and the human's answer
    (approve re-runs on resume). None if the run never reached a decision."""
    ready = next((e for e in trace if e["node"] == "check_policy"), None)
    answered = next((e for e in trace if e["node"] == "approve"), None)
    if not ready or not answered:
        return None
    return round((datetime.fromisoformat(answered["started_at"]) - _end(ready)).total_seconds(), 1)


def usage(trace: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    """Agent time (excluding the human), tokens and cost for one run."""
    calls = [c for e in trace for c in e.get("llm", [])]
    tokens_in = sum(c["input_tokens"] for c in calls)
    tokens_out = sum(c["output_tokens"] for c in calls)
    p_in, p_out = settings.llm_price_input_per_mtok, settings.llm_price_output_per_mtok
    cost = None
    if p_in is not None and p_out is not None:
        cost = round((tokens_in * p_in + tokens_out * p_out) / 1_000_000, 6)
    return {
        "agent_ms": round(sum(e["ms"] for e in trace), 1),
        "llm_calls": len(calls),
        "llm_attempts": sum(c["attempts"] for c in calls),  # >calls = repair retries
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": cost,
    }


def run_record(
    thread_id: str, state: dict[str, Any], settings: Settings, *, error: str | None = None
) -> dict[str, Any]:
    trace = state.get("trace", [])
    decision = state.get("decision", {})
    c = state.get("classification") or {}
    return {
        "thread_id": thread_id,
        "number": state.get("number"),
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": f"{settings.llm_provider}:{settings.llm_model}",
        "outcome": outcome(state, error),
        "error": error,
        "summary": state.get("summary"),
        "classification": {k: c.get(k) for k in ("category", "impact", "urgency")}
        | {"group": c.get("assignment_group")},
        "injection_flagged": bool(state.get("guard", {}).get("injection", {}).get("flagged")),
        "policy_violations": len(state.get("policy", {}).get("violations", [])),
        "reviewer": decision.get("reviewer"),
        "approval_wait_s": approval_wait_s(trace),
        **usage(trace, settings),
        "steps": [
            {"node": e["node"], "ms": e["ms"]}
            | ({"tokens": sum(x["total_tokens"] for x in e["llm"])} if e.get("llm") else {})
            for e in trace
        ],
    }


class RunLog:
    """Append-only JSONL file. One writer per process (a lock), one line per run."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records = []
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:  # a half-written line must not break /metrics
                log.warning("Skipping unreadable line %d in %s", n, self.path)
        return records


def _pct(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; None when there is nothing to measure."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = Counter(r["outcome"] for r in records)
    decided = [r for r in records if r["outcome"] not in ("blocked", "error", "incomplete")]
    approved = [r for r in decided if r["outcome"] != "rejected"]
    agent_s = [r["agent_ms"] / 1000 for r in records if r["outcome"] != "error"]
    waits = [r["approval_wait_s"] for r in records if r.get("approval_wait_s") is not None]
    costs = [r["cost_usd"] for r in records if r.get("cost_usd") is not None]
    return {
        "runs": len(records),
        "outcomes": dict(outcomes),
        "approval_rate": round(len(approved) / len(decided), 3) if decided else None,
        "agent_latency_s": {"p50": _pct(agent_s, 0.5), "p95": _pct(agent_s, 0.95)},
        "approval_wait_s": {"p50": _pct(waits, 0.5), "p95": _pct(waits, 0.95)},
        "tokens": {
            "input": sum(r.get("input_tokens", 0) for r in records),
            "output": sum(r.get("output_tokens", 0) for r in records),
        },
        "cost_usd": {
            "total": round(sum(costs), 6) if costs else None,
            "per_run": round(statistics.mean(costs), 6) if costs else None,
        },
        "llm_repair_retries": sum(
            r.get("llm_attempts", 0) - r.get("llm_calls", 0) for r in records
        ),
        "injection_flagged": sum(1 for r in records if r.get("injection_flagged")),
        "runs_with_policy_violations": sum(1 for r in records if r.get("policy_violations")),
        "errors": outcomes.get("error", 0),
    }
