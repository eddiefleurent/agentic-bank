"""GET /transfers/{id}: scoped to the token's customer; actionable if pending, fresh, intact."""

import json

import pytest

from agentic_bank.approvals import handler as approvals
from agentic_bank.shared.transfers import transfer_hash

NOW = "2026-11-12T10:00:00Z"
ID = "xfer_0000000000000001"


def transfer(customer="alex", **changes):
    fields = {
        "customer": customer,
        "transfer_id": ID,
        "from_account_id": "checking",
        "payee_id": "landlord",
        "amount_cents": 100,
        "memo": "rent",
        "expires_at": "2026-11-12T11:00:00Z",
    }
    row = {
        **{k: v for k, v in fields.items() if k != "transfer_id"},
        "sk": f"XFER#{ID}",
        "request_hash": transfer_hash(**fields),
        "created_at": NOW,
        "status": "PENDING",
    }
    return {**row, **changes}


class Table:
    def __init__(self, items):
        self.items, self.reads = items, []

    def get_item(self, Key, **_):
        self.reads.append(Key)
        rows = self.items + [
            {
                "customer": "alex",
                "sk": "PAYEE#landlord",
                "name": "Landlord",
                "account_last4": "7781",
            },
            {"customer": "alex", "sk": "ACCT#checking", "account_number": "00004417"},
        ]
        return next(({"Item": r} for r in rows if all(r[k] == v for k, v in Key.items())), {})


def test_lookup_uses_token_customer_not_the_request(monkeypatch):
    table = Table([transfer("sam")])
    monkeypatch.setenv("APP_CLIENT_ID", "app")
    monkeypatch.setattr(approvals, "_session", lambda customer: (table, None))
    claims = {"sub": "alex", "client_id": "app", "token_use": "access"}
    event = {
        "routeKey": "GET /transfers/{id}",
        "pathParameters": {"id": ID},
        "queryStringParameters": {"customer": "sam"},
        "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
    }
    response = approvals.handler(event, None)
    assert response["statusCode"] == 404
    assert json.loads(response["body"]) == {"error": "no such transfer"}
    assert table.reads == [{"customer": "alex", "sk": f"XFER#{ID}"}]


@pytest.mark.parametrize(
    ("changes", "expired", "actionable"),
    [
        ({}, False, True),
        ({"expires_at": NOW}, True, False),
        ({"status": "EXECUTED"}, False, False),
        ({"amount_cents": 100000}, False, False),  # edited after the hash was taken
    ],
)
def test_only_a_pending_fresh_intact_transfer_is_actionable(changes, expired, actionable):
    status, reply = approvals.get_transfer(Table([transfer(**changes)]), "alex", ID, NOW)
    assert status == 200
    assert reply["transfer"]["expired"] is expired
    assert reply["transfer"]["actionable"] is actionable
