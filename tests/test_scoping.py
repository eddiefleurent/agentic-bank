import base64
import json
from decimal import Decimal

import pytest

from agentic_bank.shared.data import READ, format_cents, session_policy
from agentic_bank.tools import handler as tools
from agentic_bank.tools import interceptor
from agentic_bank.tools.interceptor import handler as intercept


def token(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"h.{payload}.s"


def call(headers: dict, arguments: dict) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "bank___get_accounts", "arguments": arguments},
    }
    return intercept({"mcp": {"gatewayRequest": {"headers": headers, "body": body}}}, None)


def test_interceptor_overwrites_caller_supplied_customer(monkeypatch):
    monkeypatch.setattr(interceptor, "revocation_row", lambda customer, client_id: None)
    claims = {"sub": "alex", "client_id": "agent", "auth_time": 1}
    out = call({"Authorization": f"Bearer {token(claims)}"}, {"customer": "sam"})
    sent = out["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert sent["customer"] == "alex"


def test_interceptor_refuses_token_without_subject():
    out = call({"authorization": f"Bearer {token({'client_id': 'x'})}"}, {"customer": "sam"})
    assert "transformedGatewayRequest" not in out["mcp"]
    assert out["mcp"]["transformedGatewayResponse"]["body"]["error"]["code"] == -32001


def test_session_policy_names_only_that_customers_partition():
    stmt = json.loads(session_policy("arn:aws:dynamodb:us-east-1:1:table/t", "alex", READ))[
        "Statement"
    ][0]
    assert stmt["Condition"]["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"] == ["alex"]
    assert stmt["Condition"]["Null"] == {"dynamodb:LeadingKeys": "false"}
    assert sorted(stmt["Action"]) == ["dynamodb:GetItem", "dynamodb:Query"]  # no Scan, no writes


class FakeTable:
    """Rows keyed by (customer, sk); get_item/query only see the asked customer's rows."""

    def __init__(self, rows: dict[tuple[str, str], dict]):
        self.rows = rows

    def get_item(self, Key: dict) -> dict:
        row = self.rows.get((Key["customer"], Key["sk"]))
        return {"Item": row} if row else {}

    def query(self, **kwargs) -> dict:
        return {"Items": []}


def test_other_customers_account_is_no_such_account():
    table = FakeTable({("sam", "ACCT#acc_sam_chk"): {"name": "Checking"}})
    out = tools.list_transactions(table, "alex", {"account_id": "acc_sam_chk"})
    assert out == {"error": "no such account"}


def test_account_id_with_key_separator_is_rejected():
    out = tools.list_transactions(FakeTable({}), "alex", {"account_id": "acc_alex_chk#x"})
    assert out == {"error": "invalid input: account_id"}


@pytest.mark.parametrize(
    ("cents", "text"),
    [
        (0, "0.00"),
        (5, "0.05"),
        (1299, "12.99"),
        (-1299, "-12.99"),
        (-5, "-0.05"),
        (Decimal("245000"), "2450.00"),
    ],
)
def test_format_cents(cents, text):
    assert format_cents(cents) == text


def test_account_id_with_trailing_newline_is_rejected_before_lookup():
    out = tools.list_transactions(None, "alex", {"account_id": "acc_alex_chk\n"})
    assert out == {"error": "invalid input: account_id"}
