"""TriageService: start, resume and record triage runs. The CLI and the HTTP API both use it,
so they behave and log identically.

Two stores, two jobs:
- LangGraph's checkpointer holds each run's STATE (what the agent knows and decided).
- The run registry (a small table in the same SQLite file) holds each run's STATUS, so we
  can list pending approvals cheaply and enforce two rules atomically in SQL:
    * one active run per incident (no two agents triaging the same ticket), and
    * one decision per run (two reviewers clicking "approve" at once can't both execute).

    running -> pending_approval -> running -> done
         \\-> done (blocked)            \\-> error
          \\-> error
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from triage.config import Settings
from triage.observability import RunLog, run_record, usage

from .runtime import AgentDeps
from .schemas import Decision

ACTIVE = ("running", "pending_approval")


class RunConflict(Exception):
    """The incident already has an active run."""

    def __init__(self, number: str, thread_id: str | None) -> None:
        super().__init__(f"{number} already has an active run ({thread_id})")
        self.number = number
        self.thread_id = thread_id


class NotPending(Exception):
    """A decision arrived for a run that is not waiting for one."""

    def __init__(self, thread_id: str, status: str | None) -> None:
        super().__init__(f"Run {thread_id} is not waiting for approval (status: {status})")
        self.thread_id = thread_id
        self.status = status


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RunRegistry:
    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS triage_runs (
            thread_id  TEXT PRIMARY KEY,
            number     TEXT NOT NULL,
            status     TEXT NOT NULL,
            error      TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS triage_runs_by_number ON triage_runs (number, status);
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    @asynccontextmanager
    async def open(cls, path: str | Path) -> AsyncIterator["RunRegistry"]:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(cls._SCHEMA)
            await conn.commit()
            yield cls(conn)

    async def create(self, thread_id: str, number: str) -> bool:
        """Insert a 'running' row unless the incident already has an active run.
        One statement, so two simultaneous requests can't both pass the check."""
        now = _now()
        cursor = await self._conn.execute(
            """INSERT INTO triage_runs (thread_id, number, status, created_at, updated_at)
               SELECT ?, ?, 'running', ?, ? WHERE NOT EXISTS (
                   SELECT 1 FROM triage_runs WHERE number = ? AND status IN (?, ?))""",
            (thread_id, number, now, now, number, *ACTIVE),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def claim(self, thread_id: str) -> bool:
        """pending_approval -> running, atomically (compare-and-set). Only one caller wins."""
        cursor = await self._conn.execute(
            "UPDATE triage_runs SET status = 'running', updated_at = ? "
            "WHERE thread_id = ? AND status = 'pending_approval'",
            (_now(), thread_id),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def set_status(self, thread_id: str, status: str, error: str | None = None) -> None:
        await self._conn.execute(
            "UPDATE triage_runs SET status = ?, error = ?, updated_at = ? WHERE thread_id = ?",
            (status, error, _now(), thread_id),
        )
        await self._conn.commit()

    async def get(self, thread_id: str) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT * FROM triage_runs WHERE thread_id = ?", (thread_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def active_for(self, number: str) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT * FROM triage_runs WHERE number = ? AND status IN (?, ?)", (number, *ACTIVE)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def count(self, *, status: str | None = None, since: str | None = None) -> int:
        query, args = "SELECT COUNT(*) FROM triage_runs WHERE 1 = 1", []
        if status:
            query, args = query + " AND status = ?", [*args, status]
        if since:
            query, args = query + " AND created_at >= ?", [*args, since]
        async with self._conn.execute(query, args) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def list(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM triage_runs", ()
        if status:
            query, args = query + " WHERE status = ?", (status,)
        async with self._conn.execute(
            query + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit)
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


class TriageService:
    def __init__(
        self,
        graph: CompiledStateGraph,
        deps: AgentDeps,
        registry: RunRegistry,
        run_log: RunLog,
        settings: Settings,
    ) -> None:
        self.graph = graph
        self.deps = deps
        self.registry = registry
        self.run_log = run_log
        self.settings = settings

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    async def create(self, number: str, thread_id: str | None = None) -> str:
        """Register a new run (status 'running') without starting it."""
        thread = thread_id or f"{number}-{uuid.uuid4().hex[:8]}"
        if await self.registry.get(thread):
            raise RunConflict(number, thread)
        if not await self.registry.create(thread, number):
            active = await self.registry.active_for(number)
            raise RunConflict(number, active["thread_id"] if active else None)
        return thread

    async def advance(self, thread_id: str, value: Any) -> dict[str, Any]:
        """Run the graph until it pauses for approval or finishes; record the outcome."""
        config = self._config(thread_id)
        try:
            out = await self.graph.ainvoke(value, config, context=self.deps)
        except Exception as err:
            message = f"{type(err).__name__}: {err}"
            state = (await self.graph.aget_state(config)).values or {}
            await self.registry.set_status(thread_id, "error", message)
            self.run_log.append(run_record(thread_id, state, self.settings, error=message))
            raise
        if out.get("__interrupt__"):
            await self.registry.set_status(thread_id, "pending_approval")
        else:
            await self.registry.set_status(thread_id, "done")
            self.run_log.append(run_record(thread_id, out, self.settings))
        view = await self.view(thread_id)
        assert view is not None
        return view

    async def start(self, number: str, thread_id: str | None = None) -> dict[str, Any]:
        thread = await self.create(number, thread_id)
        return await self.advance(thread, {"number": number})

    async def decide(self, thread_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        Decision.model_validate(decision)  # reject bad input before claiming the run
        if not await self.registry.claim(thread_id):
            row = await self.registry.get(thread_id)
            raise NotPending(thread_id, row["status"] if row else None)
        return await self.advance(thread_id, Command(resume=decision))

    async def view(self, thread_id: str) -> dict[str, Any] | None:
        """Everything a reviewer or a UI needs about one run."""
        row = await self.registry.get(thread_id)
        if row is None:
            return None
        snapshot = await self.graph.aget_state(self._config(thread_id))
        values = snapshot.values or {}
        pending = snapshot.interrupts[0].value if snapshot.interrupts else None
        return {
            **row,
            "pending": pending if row["status"] == "pending_approval" else None,
            "classification": values.get("classification"),
            "decision": values.get("decision"),
            "results": values.get("results"),
            "summary": values.get("summary"),
            "usage": usage(values.get("trace", []), self.settings),
        }

    async def reconcile(self) -> int:
        """At startup: a run left 'running' by a crash or restart has no worker any more.
        If it had reached the approval point, it is still pending; otherwise mark it failed."""
        fixed = 0
        for row in await self.registry.list(status="running", limit=1000):
            snapshot = await self.graph.aget_state(self._config(row["thread_id"]))
            if snapshot.interrupts:
                await self.registry.set_status(row["thread_id"], "pending_approval")
            else:
                await self.registry.set_status(
                    row["thread_id"], "error", "Interrupted by a restart; start a new run"
                )
            fixed += 1
        return fixed
