"""Table metadata.

Every table and column the API can touch is declared here. Besides driving the generic
Table API, this is the SQL allowlist: identifiers in SQL only ever come from this module,
values are always bound parameters.
"""

from dataclasses import dataclass, field

SYS_FIELDS = ("sys_id", "sys_created_on", "sys_created_by", "sys_updated_on", "sys_updated_by")


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[str, ...]  # stored columns, excluding SYS_FIELDS
    display_field: str  # what a reference to this table shows with sysparm_display_value
    references: dict[str, str] = field(default_factory=dict)  # column -> referenced table
    choices: dict[str, dict[str, str]] = field(default_factory=dict)  # column -> {value: label}
    journal_fields: tuple[str, ...] = ()  # write-only; stored as sys_journal_field rows
    read_only_columns: tuple[str, ...] = ()  # set by business rules, never by clients
    writable: bool = True  # False = integration user only has read ACL
    number_prefix: str | None = None

    @property
    def stored(self) -> tuple[str, ...]:
        return SYS_FIELDS + self.columns

    @property
    def output_fields(self) -> tuple[str, ...]:
        return self.stored + self.journal_fields


IMPACT_URGENCY = {"1": "1 - High", "2": "2 - Medium", "3": "3 - Low"}
PRIORITY = {
    "1": "1 - Critical",
    "2": "2 - High",
    "3": "3 - Moderate",
    "4": "4 - Low",
    "5": "5 - Planning",
}
STATE = {
    "1": "New",
    "2": "In Progress",
    "3": "On Hold",
    "6": "Resolved",
    "7": "Closed",
    "8": "Canceled",
}
# First five are ServiceNow's out-of-box incident categories; access/security are ours.
CATEGORY = {
    "": "",
    "inquiry": "Inquiry / Help",
    "software": "Software",
    "hardware": "Hardware",
    "network": "Network",
    "database": "Database",
    "access": "Access",
    "security": "Security",
}

INCIDENT = Table(
    name="incident",
    columns=(
        "number",
        "short_description",
        "description",
        "category",
        "subcategory",
        "impact",
        "urgency",
        "priority",
        "state",
        "active",
        "assignment_group",
        "assigned_to",
        "caller_id",
        "correlation_id",
        "close_code",
        "close_notes",
        "opened_at",
        "resolved_at",
    ),
    display_field="number",
    references={
        "assignment_group": "sys_user_group",
        "assigned_to": "sys_user",
        "caller_id": "sys_user",
    },
    choices={
        "category": CATEGORY,
        "impact": IMPACT_URGENCY,
        "urgency": IMPACT_URGENCY,
        "priority": PRIORITY,
        "state": STATE,
    },
    journal_fields=("work_notes", "comments"),
    read_only_columns=("number", "priority", "active", "opened_at", "resolved_at"),
    number_prefix="INC",
)

SYS_USER_GROUP = Table(
    name="sys_user_group",
    columns=("name", "description", "email", "active"),
    display_field="name",
    writable=False,
)

SYS_USER = Table(
    name="sys_user",
    columns=("user_name", "name", "email", "department", "active"),
    display_field="name",
    writable=False,
)

SYS_JOURNAL_FIELD = Table(
    name="sys_journal_field",
    columns=("name", "element", "element_id", "value"),
    display_field="value",
    writable=False,
)

TABLES: dict[str, Table] = {
    t.name: t for t in (INCIDENT, SYS_USER_GROUP, SYS_USER, SYS_JOURNAL_FIELD)
}
