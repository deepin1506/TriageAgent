"""Contract tests for the mock ServiceNow Table API."""

import pytest
from fastapi.testclient import TestClient

from mock_snow.seed import GROUP_IDS, INCIDENT_IDS, USER_IDS
from tests.conftest import TOKEN

INC = "/api/now/table/incident"
NEW_INC = INCIDENT_IDS["INC0010001"]


def assert_envelope(resp, status: int) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["status"] == "failure"
    assert set(body["error"]) == {"message", "detail"}
    return body["error"]


# --- auth -------------------------------------------------------------------


def test_health_needs_no_auth(api: TestClient) -> None:
    assert api.get("/health", auth=None).json() == {"status": "ok"}


@pytest.mark.parametrize(
    "auth,headers",
    [(None, {}), (("tester", "wrong"), {}), (None, {"Authorization": "Bearer nope"})],
)
def test_rejects_missing_or_bad_credentials(api: TestClient, auth, headers) -> None:
    resp = api.get(INC, auth=auth, headers=headers)
    assert assert_envelope(resp, 401)["message"] == "User Not Authenticated"


def test_accepts_bearer_token(api: TestClient) -> None:
    resp = api.get(INC, auth=None, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200


# --- read -------------------------------------------------------------------


def test_list_envelope_total_count_and_paging(api: TestClient) -> None:
    resp = api.get(INC, params={"sysparm_limit": 3, "sysparm_offset": 2})
    assert resp.status_code == 200
    assert resp.headers["X-Total-Count"] == "8"
    assert [r["number"] for r in resp.json()["result"]] == [
        "INC0010003",
        "INC0010004",
        "INC0010005",
    ]


def test_encoded_query_filters_and_orders(api: TestClient) -> None:
    resp = api.get(INC, params={"sysparm_query": "active=true^categoryISEMPTY^ORDERBYDESCnumber"})
    numbers = [r["number"] for r in resp.json()["result"]]
    assert numbers == [f"INC001000{i}" for i in (6, 5, 4, 3, 2, 1)]


@pytest.mark.parametrize(
    "query,expected",
    [
        ("short_descriptionLIKEvpn", ["INC0010001"]),  # case-insensitive like ServiceNow
        ("numberININC0010007,INC0010008", ["INC0010007", "INC0010008"]),
        ("state!=1", ["INC0010007", "INC0010008"]),
        ("short_descriptionSTARTSWITHLaptop", ["INC0010005"]),
        ("priority<=3", ["INC0010008"]),
    ],
)
def test_query_operators(api: TestClient, query: str, expected: list[str]) -> None:
    resp = api.get(INC, params={"sysparm_query": query})
    assert [r["number"] for r in resp.json()["result"]] == expected


def test_unknown_query_field_is_a_400_not_silently_ignored(api: TestClient) -> None:
    resp = api.get(INC, params={"sysparm_query": "colour=red"})
    assert "colour" in assert_envelope(resp, 400)["detail"]


def test_get_by_sys_id_and_404(api: TestClient) -> None:
    assert api.get(f"{INC}/{NEW_INC}").json()["result"]["number"] == "INC0010001"
    err = assert_envelope(api.get(f"{INC}/{'0' * 32}"), 404)
    assert err["message"] == "No Record found"


def test_reference_fields_display_value_modes(api: TestClient) -> None:
    resolved = INCIDENT_IDS["INC0010008"]
    group_id = GROUP_IDS["Network Operations"]

    raw = api.get(f"{INC}/{resolved}").json()["result"]["assignment_group"]
    assert raw == {
        "link": f"http://testserver/api/now/table/sys_user_group/{group_id}",
        "value": group_id,
    }

    params = {"sysparm_exclude_reference_link": "true"}
    assert (
        api.get(f"{INC}/{resolved}", params=params).json()["result"]["assignment_group"] == group_id
    )

    shown = api.get(f"{INC}/{resolved}", params={"sysparm_display_value": "true"}).json()["result"]
    assert shown["assignment_group"] == "Network Operations"
    assert shown["priority"] == "3 - Moderate"
    assert shown["state"] == "Resolved"

    both = api.get(f"{INC}/{resolved}", params={"sysparm_display_value": "all"}).json()["result"]
    assert both["priority"] == {"display_value": "3 - Moderate", "value": "3"}
    assert both["assignment_group"]["display_value"] == "Network Operations"


def test_sysparm_fields_projection(api: TestClient) -> None:
    rec = api.get(f"{INC}/{NEW_INC}", params={"sysparm_fields": "number,priority"}).json()
    assert rec["result"] == {"number": "INC0010001", "priority": "5"}
    assert_envelope(api.get(INC, params={"sysparm_fields": "number,nope"}), 400)


def test_invalid_table_and_bad_params(api: TestClient) -> None:
    assert "Invalid table" in assert_envelope(api.get("/api/now/table/cmdb_ci"), 400)["message"]
    assert_envelope(api.get(INC, params={"sysparm_limit": "abc"}), 400)
    assert_envelope(api.get(INC, params={"sysparm_display_value": "maybe"}), 400)


# --- write ------------------------------------------------------------------


def test_create_incident_applies_business_rules(api: TestClient) -> None:
    resp = api.post(
        INC,
        json={
            "short_description": "Cannot connect to Wi-Fi",
            "caller_id": USER_IDS["asha.rao"],
            "impact": 2,
            "urgency": "1",
        },
    )
    assert resp.status_code == 201
    rec = resp.json()["result"]
    assert rec["number"] == "INC0010009"
    assert rec["priority"] == "2"  # 2 + 1 - 1
    assert rec["state"] == "1" and rec["active"] == "true"
    assert rec["sys_created_by"] == "tester"
    assert len(rec["sys_id"]) == 32
    assert resp.headers["Location"].endswith(f"/incident/{rec['sys_id']}")
    assert (
        api.post(INC, json={"short_description": "next"}).json()["result"]["number"] == "INC0010010"
    )


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"short_description": "x", "priority": "1"}, "Read-only field(s)"),
        ({"short_description": "x", "number": "INC1"}, "Read-only field(s)"),
        ({"short_description": "x", "colour": "red"}, "Invalid field(s)"),
        ({"short_description": "x", "category": "printers"}, "Invalid choice value"),
        ({"short_description": "x", "impact": "0"}, "Invalid choice value"),
        ({"short_description": "x", "assignment_group": "f" * 32}, "Invalid reference"),
        ({"short_description": {"nested": True}}, "Invalid value"),
        ({}, "Invalid request"),
    ],
)
def test_create_rejects_bad_payloads(api: TestClient, payload: dict, message: str) -> None:
    assert assert_envelope(api.post(INC, json=payload), 400)["message"] == message
    assert api.get(INC).headers["X-Total-Count"] == "8"  # nothing written


def test_body_must_be_an_object(api: TestClient) -> None:
    assert_envelope(api.post(INC, json=[{"short_description": "x"}]), 400)


def test_update_recomputes_priority_and_journals_work_notes(api: TestClient) -> None:
    resp = api.patch(
        f"{INC}/{NEW_INC}",
        json={
            "category": "network",
            "impact": "2",
            "urgency": "2",
            "assignment_group": GROUP_IDS["Network Operations"],
            "work_notes": "Triage agent: VPN instability, routed to Network Operations.",
        },
    )
    rec = resp.json()["result"]
    assert resp.status_code == 200
    assert rec["priority"] == "3"
    assert rec["work_notes"] == ""  # journal fields read back empty, like the real Table API
    assert rec["sys_updated_by"] == "tester"

    notes = api.get(
        "/api/now/table/sys_journal_field",
        params={"sysparm_query": f"element_id={NEW_INC}^element=work_notes"},
    ).json()["result"]
    assert [n["value"] for n in notes] == [
        "Triage agent: VPN instability, routed to Network Operations."
    ]


def test_put_is_partial_update_too(api: TestClient) -> None:
    rec = api.put(f"{INC}/{NEW_INC}", json={"urgency": "1"}).json()["result"]
    assert rec["urgency"] == "1" and rec["short_description"].startswith("VPN")


def test_resolve_requires_close_code_and_notes(api: TestClient) -> None:
    err = assert_envelope(api.patch(f"{INC}/{NEW_INC}", json={"state": "6"}), 400)
    assert "close_code" in err["detail"]

    rec = api.patch(
        f"{INC}/{NEW_INC}",
        json={
            "state": "6",
            "close_code": "Solved (Work Around)",
            "close_notes": "Reinstalled client.",
        },
    ).json()["result"]
    assert rec["state"] == "6" and rec["resolved_at"] and rec["active"] == "true"

    rec = api.patch(f"{INC}/{NEW_INC}", json={"state": "7"}).json()["result"]
    assert rec["active"] == "false"


def test_read_only_tables_return_403(api: TestClient) -> None:
    resp = api.post("/api/now/table/sys_user_group", json={"name": "Hackers"})
    assert assert_envelope(resp, 403)["message"] == "Insufficient rights"
    assert_envelope(api.post("/api/now/table/sys_journal_field", json={"value": "x"}), 403)


def test_delete(api: TestClient) -> None:
    assert api.delete(f"{INC}/{NEW_INC}").status_code == 204
    assert_envelope(api.delete(f"{INC}/{NEW_INC}"), 404)


# --- fault injection ----------------------------------------------------------


def test_fault_before_fails_without_side_effects(api: TestClient) -> None:
    api.post("/_mock/faults", json={"status": 503, "count": 2, "retry_after": 2})
    for _ in range(2):
        resp = api.post(INC, json={"short_description": "x"})
        assert_envelope(resp, 503)
        assert resp.headers["Retry-After"] == "2"
    assert api.get(INC).headers["X-Total-Count"] == "8"  # neither attempt wrote anything
    assert api.post(INC, json={"short_description": "x"}).status_code == 201


def test_fault_after_writes_then_fails(api: TestClient) -> None:
    api.post("/_mock/faults", json={"status": 504, "when": "after", "method": "POST"})
    assert_envelope(api.post(INC, json={"short_description": "lost response"}), 504)
    found = api.get(INC, params={"sysparm_query": "short_description=lost response"})
    assert found.headers["X-Total-Count"] == "1"  # the write happened anyway


def test_slow_response_after_write_returns_real_body(api: TestClient) -> None:
    api.post("/_mock/faults", json={"status": None, "when": "after", "delay_ms": 50})
    resp = api.post(INC, json={"short_description": "slow"})
    assert resp.status_code == 201
    assert resp.json()["result"]["short_description"] == "slow"


def test_fault_filters_by_method_and_path(api: TestClient) -> None:
    api.post("/_mock/faults", json={"status": 429, "method": "POST", "path_contains": "/incident"})
    assert api.get(INC).status_code == 200
    assert api.post(INC, json={"short_description": "x"}).status_code == 429


def test_request_log_and_reset(api: TestClient) -> None:
    api.get(INC, params={"sysparm_limit": 1})
    api.post(INC, json={"short_description": "x"})
    log = api.get("/_mock/requests").json()
    assert [(r["method"], r["path"]) for r in log] == [("GET", INC), ("POST", INC)]

    api.post("/_mock/reset")
    assert api.get("/_mock/requests").json() == []
    assert api.get(INC).headers["X-Total-Count"] == "8"
