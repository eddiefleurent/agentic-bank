import json
from typing import Any

import pytest

from agentic_bank.approvals import handler as approvals
from agentic_bank.shared.transfers import transfer_hash
from agentic_bank.tools.handler import parse_proposal

FIELDS: dict[str, Any] = {
    "customer": "alex",
    "transfer_id": "xfer_3f9a1c2e5b7d9a01",
    "from_account_id": "acc_alex_chk",
    "payee_id": "pay_alex_landlord",
    "amount_cents": 50000,
    "memo": "December rent",
    "expires_at": "2026-11-12T10:34:11Z",
}
CHANGED: dict[str, Any] = {
    "customer": "sam",
    "transfer_id": "xfer_3f9a1c2e5b7d9a02",
    "from_account_id": "acc_alex_sav",
    "payee_id": "pay_alex_power",
    "amount_cents": 500000,
    "memo": "December rent!",
    "expires_at": "2026-11-12T10:34:12Z",
}


def test_hash_is_pinned():
    # If this changes, the tool, the API and the CLI no longer agree: change them together.
    assert transfer_hash(**FIELDS) == (
        "c76d6c659d115f0da568a6550f63656fd6b5e80ab3e97529d2b0f89fbd4caf90"
    )


@pytest.mark.parametrize("field", list(FIELDS))
def test_editing_any_one_field_changes_the_hash(field):
    assert transfer_hash(**{**FIELDS, field: CHANGED[field]}) != transfer_hash(**FIELDS)


CHECK_FAILED = {"Code": "ConditionalCheckFailed"}
NONE = {"Code": "None"}
NOW = "2026-11-12T10:00:00Z"


def old(status: str, expires_at: str) -> dict:
    return {**CHECK_FAILED, "Item": {"status": status, "expires_at": expires_at}}


@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        ([old("EXECUTED", "2026-11-12T11:00:00Z"), NONE, NONE], "already decided"),
        ([old("REJECTED", "2026-11-12T09:00:00Z"), NONE, NONE], "already decided"),
        ([old("PENDING", "2026-11-12T09:00:00Z"), NONE, NONE], "expired"),
        ([old("PENDING", NOW), NONE, NONE], "expired"),
        ([old("PENDING", "2026-11-12T11:00:00Z"), NONE, NONE], "request changed"),
        ([NONE, CHECK_FAILED, NONE], "insufficient funds"),
        (
            [
                {
                    **CHECK_FAILED,
                    "Item": {
                        "status": {"S": "PENDING"},
                        "expires_at": {"S": "2026-11-12T11:00:00Z"},
                    },
                },
                NONE,
                NONE,
            ],
            "request changed",  # DynamoDB wire format, in case the resource layer leaves it
        ),
    ],
)
def test_cancellation_reason_maps_to_answer(reasons, expected):
    assert approvals.reason_for(reasons, NOW) == expected


def test_unexpected_cancellation_is_not_guessed():
    with pytest.raises(ValueError):
        approvals.reason_for([NONE, NONE, CHECK_FAILED], NOW)


def test_cancellation_reasons_become_http_409():
    class Client:
        def transact_write_items(self, **_):
            from botocore.exceptions import ClientError

            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [NONE, CHECK_FAILED, NONE],
                },
                "TransactWriteItems",
            )

    class Meta:
        client = Client()

    h = transfer_hash(**FIELDS)
    item = {
        "sk": f"XFER#{FIELDS['transfer_id']}",
        "from_account_id": FIELDS["from_account_id"],
        "payee_id": FIELDS["payee_id"],
        "amount_cents": FIELDS["amount_cents"],
        "memo": FIELDS["memo"],
        "expires_at": FIELDS["expires_at"],
        "request_hash": h,
        "status": "PENDING",
    }

    class Table:
        name = "t"
        meta = Meta()

        def get_item(self, Key, **_):
            return {"Item": item if Key["sk"].startswith("XFER#") else {"name": "Riverside"}}

    out = approvals.approve(Table(), "alex", str(FIELDS["transfer_id"]), {"request_hash": h}, NOW)
    assert out == (409, {"error": "insufficient funds"})


@pytest.mark.parametrize("amount", [0, -1, 100001, 12.5, "500", True, None])
def test_amount_cents_refused(amount):
    event = {"from_account_id": "a", "payee_id": "p", "amount_cents": amount}
    assert parse_proposal(event) == {"error": "invalid input: amount_cents"}


@pytest.mark.parametrize("amount", [1, 100000])
def test_amount_cents_limits_accepted(amount):
    event = {"from_account_id": "a", "payee_id": "p", "amount_cents": amount}
    assert parse_proposal(event)["amount_cents"] == amount


@pytest.mark.parametrize(
    ("auth_age", "fresh"), [(0, True), (299, True), (300, True), (301, False), (-5, False)]
)
def test_sign_in_freshness_boundary(auth_age, fresh):
    assert approvals.is_fresh(1000 - auth_age, 1000, 300) is fresh


@pytest.mark.parametrize("auth_time", [None, "", "abc"])
def test_missing_auth_time_is_not_fresh(auth_time):
    assert approvals.is_fresh(auth_time, 1000, 300) is False


def api_event(route: str, claims: dict, body: dict, path_id: str) -> dict:
    return {
        "routeKey": route,
        "pathParameters": {"id": path_id},
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
    }


def test_approve_stale_sign_in_is_403(monkeypatch):
    monkeypatch.setenv("APP_CLIENT_ID", "app")
    monkeypatch.setenv("FRESH_SIGN_IN_SECONDS", "300")
    monkeypatch.setattr(approvals, "_session", lambda customer: (None, None))
    monkeypatch.setattr(approvals, "_epoch", lambda: 10_000)
    claims = {"sub": "alex", "client_id": "app", "token_use": "access", "auth_time": "9699"}
    event = api_event("POST /transfers/{id}/approve", claims, {}, str(FIELDS["transfer_id"]))
    out = approvals.handler(event, None)
    assert out["statusCode"] == 403
    assert json.loads(out["body"]) == {"error": "sign in again"}


def test_approve_uses_token_subject_not_body_customer(monkeypatch):
    seen = []

    class Table:
        def get_item(self, Key, **_):
            seen.append(Key["customer"])
            return {}

    monkeypatch.setenv("APP_CLIENT_ID", "app")
    monkeypatch.setenv("FRESH_SIGN_IN_SECONDS", "300")
    monkeypatch.setattr(approvals, "_session", lambda customer: (Table(), None))
    monkeypatch.setattr(approvals, "_epoch", lambda: 10_000)
    claims = {"sub": "alex", "client_id": "app", "token_use": "access", "auth_time": "9990"}
    body = {"request_hash": "a" * 64, "customer": "sam"}
    event = api_event("POST /transfers/{id}/approve", claims, body, str(FIELDS["transfer_id"]))
    out = approvals.handler(event, None)
    assert out["statusCode"] == 404
    assert seen == ["alex"]


def test_agent_token_is_refused_by_handler(monkeypatch):
    monkeypatch.setenv("APP_CLIENT_ID", "app")
    claims = {"sub": "alex", "client_id": "agent", "token_use": "access"}
    out = approvals.handler(api_event("GET /transfers", claims, {}, "x"), None)
    assert out["statusCode"] == 403


@pytest.mark.parametrize("wire_format", [False, True])
def test_delayed_older_revoke_cannot_revive_intermediate_sign_in(monkeypatch, wire_format):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    from botocore.exceptions import ClientError

    from agentic_bank.tools.interceptor import is_revoked

    older_arrived, release_older = Event(), Event()
    lock = Lock()

    class Revocations:
        row = None

        def put_item(self, Item, **kwargs):
            if Item["revoked_at"] == 100:
                older_arrived.set()
                assert release_older.wait(5)
            with lock:
                # Model DynamoDB's atomic conditional PutItem, including ALL_OLD on failure.
                if "ConditionExpression" in kwargs:
                    if (
                        self.row
                        and self.row["revoked_at"] >= kwargs["ExpressionAttributeValues"][":now"]
                    ):
                        old = self.row.copy()
                        if wire_format:
                            old = {"revoked_at": {"N": str(old["revoked_at"])}}
                        raise ClientError(
                            {"Error": {"Code": "ConditionalCheckFailedException"}, "Item": old},
                            "PutItem",
                        )
                self.row = Item.copy()

    monkeypatch.setenv("AGENT_CLIENT_ID", "agent")
    table = Revocations()
    with ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(approvals.revoke_agent, table, "alex", 100)
        try:
            assert older_arrived.wait(5)
            newer = pool.submit(approvals.revoke_agent, table, "alex", 200)
            assert newer.result(timeout=5) == (200, {"status": "revoked", "revoked_at": 200})
        finally:
            release_older.set()
        assert older.result(timeout=5) == (200, {"status": "revoked", "revoked_at": 200})
    assert table.row is not None
    assert table.row["revoked_at"] == 200
    assert is_revoked(table.row, 150)  # An intervening sign-in stays revoked after older completes.
    assert approvals.revoke_agent(table, "alex", 200) == (
        200,
        {"status": "revoked", "revoked_at": 200},
    )
