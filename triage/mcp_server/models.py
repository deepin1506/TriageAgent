"""Tool contracts: argument types and return shapes.

These types become the JSON Schemas the model sees (tools/list -> input_schema and
output_schema), and the SDK validates arguments against them before a handler runs.
Values mirror mock_snow/schema.py; tests check the two stay in sync.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

Category = Literal["inquiry", "software", "hardware", "network", "database", "access", "security"]
Level = Literal["1", "2", "3"]  # impact / urgency: 1 = High, 2 = Medium, 3 = Low
# state: 1 New, 2 In Progress, 3 On Hold, 6 Resolved, 7 Closed, 8 Canceled
State = Literal["1", "2", "3", "6", "7", "8"]

IncidentNumber = Annotated[
    str, Field(pattern=r"^INC\d{7}$", description="Incident number, e.g. INC0010001")
]
WorkNote = Annotated[
    str | None,
    Field(
        max_length=4000,
        description="Internal work note appended to the incident (not visible to the caller)",
    ),
]


class IncidentView(BaseModel):
    """What tools return about an incident. References are resolved to names."""

    number: str
    short_description: str
    description: str
    category: str = Field(description="'' when not yet triaged")
    impact: str = Field(description="1 High, 2 Medium, 3 Low")
    urgency: str = Field(description="1 High, 2 Medium, 3 Low")
    priority: str = Field(description="Derived from impact x urgency: 1 Critical ... 5 Planning")
    state: str = Field(
        description="1 New, 2 In Progress, 3 On Hold, 6 Resolved, 7 Closed, 8 Canceled"
    )
    assignment_group: str = Field(description="Group name, '' when unassigned")
    caller: str = Field(description="Caller's name")
    correlation_id: str
    updated_on: str = Field(description="UTC, 'YYYY-MM-DD HH:MM:SS'")


class GroupView(BaseModel):
    name: str
    description: str
