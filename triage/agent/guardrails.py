"""Guardrails: deterministic checks around the LLM steps. No LLM calls in here.

Defence in layers (no single layer is enough on its own):
  1. Input:   scan ticket text for prompt-injection patterns (this module)
  2. Prompt:  instructions in the system message, user text only inside <ticket>/<kb> tags
  3. Output:  Pydantic schemas; the LLM never chooses the incident number; no state field
              exists, so it cannot propose closing or resolving a ticket
  4. Policy:  every proposed action is checked against the catalogue and the classification
              before a human sees it (this module)
  5. Human:   nothing is written without approval, and execute re-checks that exactly the
              approved actions are the ones about to run (approval hash, this module)
  6. Server:  the MCP tool allowlist and the integration user's least-privilege ACLs

The injection scanner is a heuristic: it catches common, lazy attacks and gives the
reviewer a warning. It does NOT make injection impossible (paraphrases, other languages,
encodings get through). That's why layers 3-6 exist and don't depend on it.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

# (name, weight, pattern). Weight 3 = a strong sign of an attack on its own;
# weight 1 = suspicious only in combination (a user may legitimately write "assign this to").
_SIGNALS: list[tuple[str, int, re.Pattern[str]]] = [
    (
        "override_instructions",
        3,
        re.compile(
            r"\b(ignore|disregard|forget|override)\b[\w\s,]{0,30}"
            r"\b(instructions?|prompts?|rules|guidelines|context)\b",
            re.I,
        ),
    ),
    (
        "role_override",
        3,
        re.compile(
            r"\byou are now\b|\bact as (an?|the)\s+\w+|\bpretend to be\b|"
            r"\byou are (an?|the)\s+[\w\s]{0,20}\b(bot|assistant|ai|agent|model)\b|"
            r"\b(admin|developer|god|debug|jailbreak)\s+mode\b",
            re.I,
        ),
    ),
    (
        "prompt_exfiltration",
        3,
        re.compile(
            r"\b(system|hidden|initial)\s+prompt\b|\breveal\b[\w\s]{0,20}\binstructions\b", re.I
        ),
    ),
    (
        "chat_template_tokens",
        3,
        re.compile(
            r"<\|im_start\|>|<\|im_end\|>|\[INST\]|<</?SYS>>|^\s*(system|assistant)\s*:",
            re.I | re.M,
        ),
    ),
    (
        "priority_directive",
        1,
        re.compile(r"\b(set|make|change)\b[\w\s]{0,15}\b(priority|urgency|impact)\b", re.I),
    ),
    (
        "assignment_directive",
        1,
        re.compile(r"\b(assign|route|escalate|send)\s+(this|it|the ticket)\s+to\b", re.I),
    ),
    (
        "bulk_or_closing_directive",
        2,
        re.compile(
            r"\b(clos|resolv|delet|cancel)\w*\b[\w\s]{0,15}\b(all|every|other)\b[\w\s]{0,15}"
            r"\b(incidents?|tickets?)\b",
            re.I,
        ),
    ),
]


@dataclass
class InjectionScan:
    score: int
    signals: list[str] = field(default_factory=list)
    flagged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def scan_for_injection(text: str, threshold: int = 3) -> InjectionScan:
    """Weighted pattern match. Flagged when the summed weight reaches `threshold`."""
    signals = [name for name, _, pattern in _SIGNALS if pattern.search(text)]
    score = sum(weight for name, weight, _ in _SIGNALS if name in signals)
    return InjectionScan(score=score, signals=signals, flagged=score >= threshold)


def actions_hash(actions: list[dict[str, Any]]) -> str:
    """Fingerprint of exactly what a reviewer approved (canonical JSON -> SHA-256)."""
    canonical = json.dumps(actions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class PolicyResult:
    actions: list[dict[str, Any]]  # the actions that passed
    violations: list[str]  # human-readable, shown to the reviewer


def check_proposal(
    actions: list[dict[str, Any]],
    classification: dict[str, Any],
    group_names: list[str],
) -> PolicyResult:
    """Drop any proposed action that breaks policy, and say why. Fail closed: a bad action
    is removed, never "fixed" silently."""
    known_groups = {g.lower(): g for g in group_names}
    kept: list[dict[str, Any]] = []
    violations: list[str] = []
    seen_tools: set[str] = set()

    for action in actions:
        tool, args = action["tool"], action["args"]
        if tool in seen_tools:
            violations.append(f"Removed a second {tool} action (one of each is allowed)")
            continue

        if tool == "assign_group":
            group = str(args.get("group", ""))
            if group.lower() not in known_groups:
                violations.append(f"Removed assign_group: '{group}' is not an assignment group")
                continue
            if group.lower() != str(classification.get("assignment_group", "")).lower():
                violations.append(
                    f"Removed assign_group: '{group}' differs from the classified group "
                    f"'{classification.get('assignment_group')}'"
                )
                continue

        if tool == "update_incident":
            mismatched = [
                f"{name}={args[name]!r} (classified {classification.get(name)!r})"
                for name in ("category", "impact", "urgency")
                if name in args and args[name] != classification.get(name)
            ]
            if mismatched:
                violations.append(
                    "Removed update_incident: it disagrees with the classification on "
                    + ", ".join(mismatched)
                )
                continue

        seen_tools.add(tool)
        kept.append(action)

    if classification.get("assignment_group", "").lower() not in known_groups:
        violations.append(
            f"Classified group '{classification.get('assignment_group')}' is not a real group"
        )
    return PolicyResult(kept, violations)
