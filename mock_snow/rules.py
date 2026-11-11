"""Business rules: the server-side logic ServiceNow runs on insert/update.

Kept small and explicit. The important one for the agent: priority is *derived* from
impact x urgency (ServiceNow's default priority lookup), so the agent should classify
impact and urgency, not write priority directly.
"""

from collections.abc import Callable

from .db import now
from .errors import SnowAPIError

RESOLVED_STATES = {"6", "7"}  # Resolved, Closed
INACTIVE_STATES = {"7", "8"}  # Closed, Canceled


def calc_priority(impact: str, urgency: str) -> str:
    """Default lookup: 1+1 -> 1 (Critical) ... 3+3 -> 5 (Planning)."""
    return str(int(impact) + int(urgency) - 1)


def _check_resolution(record: dict[str, str]) -> None:
    if record.get("state") in RESOLVED_STATES:
        missing = [f for f in ("close_code", "close_notes") if not record.get(f)]
        if missing:
            raise SnowAPIError(
                400,
                "Operation Failed",
                f"Data Policy Exception: {', '.join(missing)} mandatory when resolving",
            )


def incident_before_insert(values: dict[str, str]) -> None:
    values.setdefault("state", "1")
    values.setdefault("impact", "3")
    values.setdefault("urgency", "3")
    values["priority"] = calc_priority(values["impact"], values["urgency"])
    values["active"] = "false" if values["state"] in INACTIVE_STATES else "true"
    values["opened_at"] = now()
    _check_resolution(values)
    if values["state"] in RESOLVED_STATES:
        values["resolved_at"] = now()


def incident_before_update(current: dict[str, str], changes: dict[str, str]) -> None:
    merged = {**current, **changes}
    if "impact" in changes or "urgency" in changes:
        changes["priority"] = calc_priority(merged["impact"], merged["urgency"])
    if "state" in changes:
        _check_resolution(merged)
        if changes["state"] in RESOLVED_STATES and not current.get("resolved_at"):
            changes["resolved_at"] = now()
        changes["active"] = "false" if changes["state"] in INACTIVE_STATES else "true"


BEFORE_INSERT: dict[str, Callable[[dict[str, str]], None]] = {
    "incident": incident_before_insert,
}
BEFORE_UPDATE: dict[str, Callable[[dict[str, str], dict[str, str]], None]] = {
    "incident": incident_before_update,
}
