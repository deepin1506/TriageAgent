import pytest

from mock_snow.errors import SnowAPIError
from mock_snow.query import parse
from mock_snow.schema import INCIDENT


def test_empty_query() -> None:
    q = parse("", INCIDENT)
    assert q.where == [] and q.params == [] and q.order_by == []


def test_operator_precedence_and_ordering() -> None:
    q = parse("state!=7^priority<=2^categoryISNOTEMPTY^ORDERBYDESCpriority^ORDERBYnumber", INCIDENT)
    assert q.where == ["state != ?", "priority <= ?", "category != ''"]
    assert q.params == ["7", "2"]
    assert q.order_by == ["priority DESC", "number ASC"]


def test_like_escapes_wildcards_and_double_caret_is_literal() -> None:
    q = parse("short_descriptionLIKE50%_off^^now", INCIDENT)
    assert q.params == ["%50\\%\\_off^now%"]


def test_values_are_bound_never_interpolated() -> None:
    q = parse("number=x' OR '1'='1", INCIDENT)
    assert q.where == ["number = ?"]
    assert q.params == ["x' OR '1'='1"]


@pytest.mark.parametrize(
    "query", ["ORnumber=1", "number=1^NQstate=1", "Number=1", "nope=1", "ORDERBYnope", "number~1"]
)
def test_rejects_unsupported_or_unknown(query: str) -> None:
    with pytest.raises(SnowAPIError) as exc:
        parse(query, INCIDENT)
    assert exc.value.status == 400
