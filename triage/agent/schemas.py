"""What the LLM must return (structured outputs) and what a reviewer sends back.

Reusing the MCP `Literal` types means the LLM's vocabulary is exactly the tools' vocabulary.

Design choice worth defending in an interview: action args have NO incident number.
The LLM never chooses WHICH incident to write to; code fills in the number from state.
A prompt injection in ticket text therefore can't redirect a write to another incident.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from triage.mcp_server.models import Category, Level


class Classification(BaseModel):
    category: Category
    impact: Level = Field(description="1 = many people/critical process, 2 = several, 3 = one")
    urgency: Level = Field(description="1 = work stopped or safety/security risk, 3 = can wait")
    assignment_group: str = Field(description="Exactly one name from the group list")
    rationale: str = Field(description="One or two sentences: why these values")


class UpdateArgs(BaseModel):
    category: Category | None = None
    impact: Level | None = None
    urgency: Level | None = None
    work_note: str | None = Field(default=None, max_length=1000)


class UpdateAction(BaseModel):
    tool: Literal["update_incident"]
    args: UpdateArgs


class AssignArgs(BaseModel):
    group: str
    work_note: str | None = Field(default=None, max_length=1000)


class AssignAction(BaseModel):
    tool: Literal["assign_group"]
    args: AssignArgs


# A tagged union: the "tool" field decides which args shape applies (like a TS
# discriminated union). Typed args also suit providers that dislike free-form dicts.
Action = Annotated[UpdateAction | AssignAction, Field(discriminator="tool")]


class Proposal(BaseModel):
    actions: list[Action] = Field(min_length=1, max_length=3)
    rationale: str
    citations: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Ids of the knowledge-base excerpts you relied on, exactly as labelled "
        "(e.g. KB0001#resolution). Empty if none applied.",
    )


class Decision(BaseModel):
    """The resume value a reviewer sends to the approve node."""

    approved: bool
    reviewer: str = Field(min_length=1)
    comment: str | None = None
