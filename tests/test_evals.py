"""Phase 5: the eval dataset, the scoring, and the harness itself.

The harness is tested with an "oracle" LLM that answers each ticket with its own label:
if the agent, tools and scoring are wired correctly, every metric must come out perfect.
A deliberately bad LLM must then fail the gate. Real-LLM numbers come from `triage-eval`.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import REPO, settings_for
from tests.fakes import ScriptedLLM
from triage.evals.dataset import EvalCase, load_cases
from triage.evals.metrics import check, score
from triage.evals.runner import CaseResult, run_eval

pytestmark = pytest.mark.anyio
CSV = REPO / "evals" / "tickets.csv"
THRESHOLDS = json.loads((REPO / "evals" / "thresholds.json").read_text(encoding="utf-8"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def oracle(case: EvalCase) -> ScriptedLLM:
    """Answers with the case's own labels: a perfect model, for checking the harness."""
    return ScriptedLLM(
        {
            "category": case.category,
            "impact": case.impact,
            "urgency": case.urgency,
            "assignment_group": case.assignment_group,
            "rationale": "oracle",
            "actions": [
                {
                    "tool": "update_incident",
                    "args": {
                        "category": case.category,
                        "impact": case.impact,
                        "urgency": case.urgency,
                    },
                },
                {"tool": "assign_group", "args": {"group": case.assignment_group}},
            ],
        }
    )


def always_service_desk(_: EvalCase) -> ScriptedLLM:
    """A lazy model that files everything as a low-priority question."""
    return ScriptedLLM(
        {
            "category": "inquiry",
            "impact": "3",
            "urgency": "3",
            "assignment_group": "Service Desk",
            "rationale": "lazy",
            "actions": [
                {
                    "tool": "update_incident",
                    "args": {"category": "inquiry", "impact": "3", "urgency": "3"},
                },
                {"tool": "assign_group", "args": {"group": "Service Desk"}},
            ],
            "citations": ["KB0999#invented"],
        }
    )


# --- dataset ------------------------------------------------------------------------------


def test_the_dataset_is_valid_and_covers_every_category() -> None:
    cases = load_cases(CSV)
    assert len(cases) == 40
    assert {c.category for c in cases} == {
        "inquiry",
        "software",
        "hardware",
        "network",
        "database",
        "access",
        "security",
    }
    assert sum(c.injection for c in cases) == 3
    assert {c.priority for c in cases} >= {"1", "2", "3", "4", "5"}  # every priority occurs


def test_bad_labels_are_rejected_before_any_llm_call(tmp_path: Path) -> None:
    rows = CSV.read_text(encoding="utf-8").splitlines()
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "\n".join([rows[0], rows[1].replace(",network,", ",printers,", 1)]), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unknown category 'printers'"):
        load_cases(bad)


# --- scoring ------------------------------------------------------------------------------


def test_a_crashed_run_counts_as_wrong() -> None:
    case = load_cases(CSV)[0]
    summary = score([case], [CaseResult(case.id, error="boom")])["summary"]
    assert summary["errors"] == 1 and summary["category_accuracy"] == 0.0
    assert any("errors" in f for f in check(summary, {"max_errors": 0}))


# --- the harness, end to end ------------------------------------------------------------------


async def run(llm_for: Any, cases: list[EvalCase]) -> dict[str, Any]:
    results = await run_eval(cases, llm_for, settings_for("http://replaced-by-the-runner"))
    return score(cases, results)


async def test_a_perfect_model_scores_perfectly_and_passes_the_gate() -> None:
    cases = load_cases(CSV)
    s = (await run(oracle, cases))["summary"]
    for metric in (
        "category_accuracy",
        "priority_accuracy",
        "group_accuracy",
        "tool_call_accuracy",
        "end_state_accuracy",
        "injection_recall",
        "injection_resistance",
    ):
        assert s[metric] == 1.0, metric
    assert s["errors"] == 0 and s["injection_false_positive_rate"] == 0.0
    assert s["kb_hit_rate"] >= 0.8  # offline lexical embedder; the real one is measured live
    assert check(s, THRESHOLDS) == []


async def test_a_lazy_model_fails_the_gate() -> None:
    cases = load_cases(CSV)[:10]
    s = (await run(always_service_desk, cases))["summary"]
    assert s["category_accuracy"] < 0.5 and s["group_accuracy"] < 0.5
    assert s["citation_hallucination_rate"] == 1.0  # it cited a source it never saw
    failures = " ".join(check(s, THRESHOLDS))
    assert "category_accuracy" in failures and "citation_hallucination_rate" in failures
