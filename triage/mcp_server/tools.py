"""Tool and resource handlers.

Every handler follows one pattern:
    get the client -> call ServiceNow -> map the result -> map errors deliberately.

Docstrings here are NOT developer docs: the SDK sends each tool's docstring to the model
as its description. They're written for the model; notes for humans are comments.
Registration (names, annotations, the allowlist) lives in server.py.
"""

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from pydantic import Field

from triage.kb.embeddings import Embedder
from triage.kb.store import KBIndex
from triage.snow_client import SnowClient, SnowError

from .models import (
    Category,
    GroupView,
    IncidentNumber,
    IncidentView,
    KBHit,
    KBSearchResult,
    Level,
    State,
    WorkNote,
)

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Shared server state. The lifespan in server.py opens/closes the client."""

    snow: SnowClient | None = None
    kb: KBIndex | None = None  # None when the index isn't built or can't be loaded
    embedder: Embedder | None = None
    kb_top_k: int = 4
    kb_min_score: float = 0.0


def snow(ctx: Context) -> SnowClient:
    client = ctx.request_context.lifespan_context.snow
    if client is None:
        raise RuntimeError("SnowClient not initialised (server lifespan not running)")
    return client


# --------------------------------------------------------------------------- helpers


def to_tool_error(err: SnowError, *, number: str | None = None) -> ToolError:
    """Turn a SnowError into a message the MODEL can act on.

    The model never sees URLs, credentials, raw bodies or stack traces: only what it
    needs to decide its next step (fix the arguments, give up, or try later).
    Operators get the full detail in the server log (stderr).
    """
    log.warning("ServiceNow call failed: %s", err)
    if err.status == 404:
        return ToolError(f"{number or 'Record'} not found")
    if err.status == 400:
        # The detail (e.g. "close_code mandatory") is what lets the model fix its call.
        return ToolError(f"ServiceNow rejected the request: {err.message}: {err.detail}")
    if err.status in (401, 403):
        return ToolError(
            "ServiceNow rejected the integration's credentials or permissions. "
            "Retrying will not help; an administrator must fix the integration user."
        )
    # 429, 5xx, or 0 (no response): the client already retried.
    return ToolError("ServiceNow is unavailable right now. Try again later.")


def incident_view(record: dict[str, Any]) -> IncidentView:
    """Build an IncidentView from a record fetched with display_value="all".

    In that mode every field is {"display_value": ..., "value": ...}: raw values for
    codes (category, impact, ...), display values for references (group and caller names).
    """

    def value(field: str) -> str:
        return record[field]["value"]

    def shown(field: str) -> str:
        return record[field]["display_value"]

    return IncidentView(
        number=value("number"),
        short_description=value("short_description"),
        description=value("description"),
        category=value("category"),
        impact=value("impact"),
        urgency=value("urgency"),
        priority=value("priority"),
        state=value("state"),
        assignment_group=shown("assignment_group"),
        caller=shown("caller_id"),
        correlation_id=value("correlation_id"),
        updated_on=value("sys_updated_on"),
    )


async def _find(ctx: Context, number: str) -> dict[str, Any]:
    """The full record for an incident number (display_value="all"), or a ToolError."""
    try:
        rows = await snow(ctx).query("incident", f"number={number}", limit=1, display_value="all")
    except SnowError as err:
        raise to_tool_error(err, number=number) from err
    if not rows:
        raise ToolError(f"Incident {number} not found")
    return rows[0]


async def _patch(ctx: Context, number: str, changes: dict[str, Any]) -> IncidentView:
    """PATCH an incident by number, then re-read it so the view shows resolved names."""
    record = await _find(ctx, number)
    try:
        await snow(ctx).update_record("incident", record["sys_id"]["value"], changes)
    except SnowError as err:
        raise to_tool_error(err, number=number) from err
    # Writes return raw values (sys_ids, codes), so one extra GET gives the model the same
    # readable view as get_incident. Trade-off: +1 request per write.
    return incident_view(await _find(ctx, number))


# ------------------------------------------------------------------ resource


async def assignment_groups(client: SnowClient) -> list[GroupView]:
    """Active assignment groups. The valid values for assign_group's `group` argument."""
    # Static resources can't take a Context in SDK v2, so server.py passes the client in.
    try:
        rows = await client.query(
            "sys_user_group", "active=true^ORDERBYname", limit=100, fields=["name", "description"]
        )
    except SnowError as err:
        # Resources raise ResourceError (tools raise ToolError). Message is safe to show.
        raise ResourceError(f"Could not load assignment groups ({err.status})") from err
    return [GroupView(name=r["name"], description=r["description"]) for r in rows]


# ----------------------------------------------------------------------------- tools


async def get_incident(ctx: Context, number: IncidentNumber) -> IncidentView:
    """Get one incident by its number, with assignment group and caller as names."""
    return incident_view(await _find(ctx, number))


async def create_incident(
    ctx: Context,
    short_description: Annotated[str, Field(min_length=5, max_length=160)],
    description: Annotated[str, Field(max_length=4000)],
    correlation_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            description=(
                "Stable ID of the source, e.g. the email Message-ID. Calling again with the "
                "same correlation_id returns the existing incident instead of a duplicate."
            ),
        ),
    ],
    caller_user_name: Annotated[
        str | None,
        # The pattern also stops encoded-query injection ('^') when this goes into a query.
        Field(
            pattern=r"^[A-Za-z0-9._-]{1,60}$", description="The caller's user name, e.g. asha.rao"
        ),
    ] = None,
) -> IncidentView:
    """Create a new incident (state New). Safe to retry with the same correlation_id."""
    data: dict[str, Any] = {"short_description": short_description, "description": description}
    try:
        if caller_user_name:
            users = await snow(ctx).query(
                "sys_user", f"user_name={caller_user_name}", limit=1, fields=["sys_id"]
            )
            if not users:
                raise ToolError(f"Unknown caller user name '{caller_user_name}'")
            data["caller_id"] = users[0]["sys_id"]
        created = await snow(ctx).create_incident_idempotent(data, correlation_id)
    except SnowError as err:
        raise to_tool_error(err) from err
    return incident_view(await _find(ctx, created["number"]))


async def update_incident(
    ctx: Context,
    number: IncidentNumber,
    category: Category | None = None,
    impact: Level | None = None,
    urgency: Level | None = None,
    state: State | None = None,
    close_code: Annotated[str | None, Field(max_length=80)] = None,
    close_notes: Annotated[str | None, Field(max_length=4000)] = None,
    work_note: WorkNote = None,
) -> IncidentView:
    """Update triage fields on an incident. Only the fields you pass are changed.

    There is no priority argument: ServiceNow derives priority from impact x urgency.
    Resolving (state 6) or closing (state 7) requires close_code and close_notes.
    """
    fields = {
        "category": category,
        "impact": impact,
        "urgency": urgency,
        "state": state,
        "close_code": close_code,
        "close_notes": close_notes,
        "work_notes": work_note,  # ServiceNow's field name is plural
    }
    changes = {k: v for k, v in fields.items() if v is not None}
    if not changes:  # checked before any network call
        raise ToolError("Nothing to update: pass at least one field to change")
    return await _patch(ctx, number, changes)


async def assign_group(
    ctx: Context,
    number: IncidentNumber,
    group: Annotated[
        str,
        Field(
            min_length=2,
            max_length=100,
            description="Assignment group name, e.g. 'Network Operations' (case-insensitive)",
        ),
    ],
    work_note: WorkNote = None,
) -> IncidentView:
    """Assign an incident to a group by name. Fails with the list of valid groups if unknown."""
    try:
        groups = await snow(ctx).query(
            "sys_user_group", "active=true", limit=100, fields=["sys_id", "name"]
        )
    except SnowError as err:
        raise to_tool_error(err) from err

    # Matched in Python, not inside a query string: nothing for a bad name to inject into.
    wanted = group.strip().lower()
    match = next((g for g in groups if g["name"].lower() == wanted), None)
    if match is None:
        names = ", ".join(sorted(g["name"] for g in groups))
        raise ToolError(f"Unknown assignment group '{group}'. Valid groups: {names}")

    changes: dict[str, Any] = {"assignment_group": match["sys_id"]}
    if work_note:
        changes["work_notes"] = work_note
    return await _patch(ctx, number, changes)


async def search_kb(
    ctx: Context,
    query: Annotated[
        str, Field(min_length=3, max_length=2000, description="The problem, in plain words")
    ],
    top_k: Annotated[int | None, Field(ge=1, le=8)] = None,
) -> KBSearchResult:
    """Search the IT knowledge base. Returns the most relevant article sections, each with a
    citation id such as KB0001#resolution and the team that owns that kind of issue."""
    app: AppContext = ctx.request_context.lifespan_context
    if app.kb is None or app.embedder is None:
        raise ToolError("The knowledge base is not available right now.")
    try:
        # Embedding calls a network API synchronously; run it off the event loop.
        vector = await asyncio.to_thread(app.embedder.query, query)
    except Exception as err:  # the provider's own errors vary; log them, tell the model less
        log.warning("KB query embedding failed: %r", err)
        raise ToolError("The knowledge base search failed. Try again later.") from err
    hits = app.kb.search(vector, k=top_k or app.kb_top_k, min_score=app.kb_min_score)
    return KBSearchResult(hits=[KBHit(**asdict(h)) for h in hits])
