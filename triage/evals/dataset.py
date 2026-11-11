"""The labelled eval tickets (evals/tickets.csv): load and validate them.

CSV on purpose: you can open it in Excel or VS Code, change a label, and re-run.
Labels are the ground truth, so every row is validated before any LLM call.
"""

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from mock_snow.rules import calc_priority
from mock_snow.seed import GROUPS
from triage.mcp_server.models import Category, Level

COLUMNS = [
    "id",
    "short_description",
    "description",
    "category",
    "impact",
    "urgency",
    "assignment_group",
    "kb",
    "injection",
    "why",
]


@dataclass(frozen=True)
class EvalCase:
    id: str
    short_description: str
    description: str
    category: str
    impact: str
    urgency: str
    assignment_group: str
    kb: tuple[str, ...]  # articles a good answer could cite; empty = none expected
    injection: bool  # ticket text contains a prompt-injection attempt
    why: str  # the labeller's reasoning ("DEBATABLE: ..." marks a judgement call)

    @property
    def priority(self) -> str:
        return calc_priority(self.impact, self.urgency)


def load_cases(path: str | Path = "evals/tickets.csv") -> list[EvalCase]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            raise ValueError(f"{path}: columns must be {COLUMNS}, got {reader.fieldnames}")
        rows = list(reader)

    problems: list[str] = []
    cases: list[EvalCase] = []
    for n, row in enumerate(rows, start=2):  # line 1 is the header
        where = f"line {n} ({row.get('id')})"
        if row["category"] not in get_args(Category):
            problems.append(f"{where}: unknown category {row['category']!r}")
        for field in ("impact", "urgency"):
            if row[field] not in get_args(Level):
                problems.append(f"{where}: {field} must be 1, 2 or 3")
        if row["assignment_group"] not in GROUPS:
            problems.append(f"{where}: unknown group {row['assignment_group']!r}")
        if row["injection"] not in ("yes", "no"):
            problems.append(f"{where}: injection must be yes or no")
        cases.append(
            EvalCase(
                id=row["id"],
                short_description=row["short_description"],
                description=row["description"],
                category=row["category"],
                impact=row["impact"],
                urgency=row["urgency"],
                assignment_group=row["assignment_group"],
                kb=tuple(k.strip() for k in row["kb"].split(";") if k.strip()),
                injection=row["injection"] == "yes",
                why=row["why"],
            )
        )
    ids = [c.id for c in cases]
    problems += [f"duplicate id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    if problems:
        raise ValueError(f"{path} has invalid labels:\n  " + "\n  ".join(problems))
    return cases
