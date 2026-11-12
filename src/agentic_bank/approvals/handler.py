"""Approvals API Lambda (API Gateway HTTP API, payload v2.0). Only the bank app's tokens reach it.

The customer is the token's subject, never the path or the body. Approval is one DynamoDB
transaction, so a replay, a double click and two concurrent approvals can execute at most once.
"""

import json
import logging
import os
import re
import time
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from agentic_bank.shared.data import format_cents, scoped_dynamodb
from agentic_bank.shared.transfers import TRANSFER_ID, now_iso, transfer_hash

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HASH = re.compile(r"^[0-9a-f]{64}$")
APPROVER_ACTIONS = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:PutItem"]
Response = tuple[int, dict[str, Any]]
ROUTES = {
    "GET /transfers",
    "POST /agent/revoke",
    "GET /transfers/{id}",
    "POST /transfers/{id}/reject",
    "POST /transfers/{id}/approve",
}


def is_fresh(auth_time: Any, now: int, window: int) -> bool:
    """True if the sign-in (token `auth_time`, epoch seconds) is at most `window` seconds old."""
    try:
        return 0 <= now - int(auth_time) <= window
    except TypeError, ValueError:
        return False


def _field(value: Any, kind: str = "S") -> Any:
    """CancellationReasons items may arrive still in DynamoDB wire format ({"S": "..."})."""
    return value.get(kind, value) if isinstance(value, dict) else value


def reason_for(cancellation_reasons: list[dict[str, Any]], now: str) -> str:
    """Why approve was cancelled (one reason per item, in order). Nothing was applied."""
    codes = [r.get("Code") for r in cancellation_reasons]
    if codes[0] == "ConditionalCheckFailed":
        old = cancellation_reasons[0].get("Item")
        if old:
            if _field(old.get("status")) != "PENDING":
                return "already decided"
            if _field(old.get("expires_at")) <= now:
                return "expired"
            return "request changed"
    elif codes[1] == "ConditionalCheckFailed":
        return "insufficient funds"
    raise ValueError(f"unexpected cancellation: {codes}")


def _session(customer: str) -> tuple[Any, Any]:
    """(data table, revocations table) through the approver role, pinned to this customer."""
    arns = [os.environ["TABLE_ARN"], os.environ["REVOCATIONS_TABLE_ARN"]]
    ddb = scoped_dynamodb("APPROVER_ROLE_ARN", customer, arns, APPROVER_ACTIONS)
    return ddb.Table(arns[0].rpartition("/")[2]), ddb.Table(arns[1].rpartition("/")[2])


def _recomputed(customer: str, item: dict[str, Any]) -> str:
    return transfer_hash(
        customer,
        item["sk"].removeprefix("XFER#"),
        item["from_account_id"],
        item["payee_id"],
        item["amount_cents"],
        item["memo"],
        item["expires_at"],
    )


def _view(
    customer: str, item: dict[str, Any], payee: dict[str, Any], account: dict[str, Any], now: str
) -> dict[str, Any]:
    expired = item["expires_at"] <= now
    intact = item["request_hash"] == _recomputed(customer, item)
    return {
        "transfer_id": item["sk"].removeprefix("XFER#"),
        "status": item["status"],
        "amount": format_cents(item["amount_cents"]),
        "amount_cents": int(item["amount_cents"]),
        "from_account_id": item["from_account_id"],
        "from_last4": account.get("account_number", "")[-4:],
        "payee_id": item["payee_id"],
        "payee_name": payee.get("name"),  # the bank's record, not the agent's words
        "payee_last4": payee.get("account_last4"),
        "memo_untrusted": item["memo"],
        "created_at": item["created_at"],
        "expires_at": item["expires_at"],
        "expired": expired,
        "request_hash": item["request_hash"],
        "intact": intact,
        "actionable": item["status"] == "PENDING"
        and not expired
        and intact
        and bool(payee)
        and bool(account),
    }


def get_transfer(table: Any, customer: str, transfer_id: str, now: str) -> Response:
    def get(sk: str) -> dict[str, Any]:
        key = {"customer": customer, "sk": sk}
        return table.get_item(Key=key, ConsistentRead=True).get("Item", {})

    item = get(f"XFER#{transfer_id}")
    if not item:
        return 404, {"error": "no such transfer"}
    payee, account = get(f"PAYEE#{item['payee_id']}"), get(f"ACCT#{item['from_account_id']}")
    return 200, {"transfer": _view(customer, item, payee, account, now)}


def list_transfers(table: Any, customer: str, now: str) -> Response:
    def rows(prefix: str) -> list[dict[str, Any]]:
        return table.query(
            KeyConditionExpression=Key("customer").eq(customer) & Key("sk").begins_with(prefix)
        )["Items"]

    payees = {p["sk"].removeprefix("PAYEE#"): p for p in rows("PAYEE#")}
    accounts = {a["sk"].removeprefix("ACCT#"): a for a in rows("ACCT#")}
    items = sorted(rows("XFER#"), key=lambda i: i["created_at"], reverse=True)
    items.sort(key=lambda i: i["status"] != "PENDING")  # stable: PENDING first, then newest
    return 200, {
        "transfers": [
            _view(
                customer,
                i,
                payees.get(i["payee_id"], {}),
                accounts.get(i["from_account_id"], {}),
                now,
            )
            for i in items[:20]
        ]
    }


def approve(
    table: Any, customer: str, transfer_id: str, body: dict[str, Any], now: str
) -> Response:
    sent_hash = body.get("request_hash")
    if not isinstance(sent_hash, str) or not HASH.fullmatch(sent_hash):
        return 400, {"error": "invalid input: request_hash"}
    key = {"customer": customer, "sk": f"XFER#{transfer_id}"}
    item = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if item is None:
        return 404, {"error": "no such transfer"}
    # Stored (what the agent asked) == recomputed (nobody edited it since) == sent (what the
    # customer saw).
    if not item["request_hash"] == _recomputed(customer, item) == sent_hash:
        return 409, {"error": "request changed"}
    payee = table.get_item(Key={"customer": customer, "sk": f"PAYEE#{item['payee_id']}"}).get(
        "Item"
    )
    if payee is None:
        return 409, {"error": "request changed"}

    amount = item["amount_cents"]
    names = {"#s": "status"}
    txn_sk = f"TXN#{item['from_account_id']}#{now[:10]}#t_{transfer_id}"
    values = {
        ":p": "PENDING",
        ":x": "EXECUTED",
        ":now": now,
        ":h": sent_hash,
        ":a": amount,
        ":f": item["from_account_id"],
        ":pi": item["payee_id"],
        ":m": item["memo"],
        ":e": item["expires_at"],
    }
    name = table.name
    try:
        table.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": name,
                        "Key": key,
                        "UpdateExpression": "SET #s = :x, decided_at = :now",
                        "ConditionExpression": "#s = :p AND request_hash = :h AND amount_cents = :a"
                        " AND from_account_id = :f AND payee_id = :pi AND memo = :m"
                        " AND expires_at = :e AND expires_at > :now",
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": values,
                        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
                    }
                },
                {
                    "Update": {
                        "TableName": name,
                        "Key": {"customer": customer, "sk": f"ACCT#{item['from_account_id']}"},
                        "UpdateExpression": "SET balance_cents = balance_cents - :a",
                        "ConditionExpression": "balance_cents >= :a",
                        "ExpressionAttributeValues": {":a": amount},
                        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
                    }
                },
                {
                    "Put": {
                        "TableName": name,
                        "Item": {
                            "customer": customer,
                            "sk": txn_sk,
                            "description": f"ACH TRANSFER TO {payee['name'].upper()}",
                            "amount_cents": -amount,
                            "channel": "ach",
                        },
                        "ConditionExpression": "attribute_not_exists(sk)",
                    }
                },
            ],
        )
    except ClientError as err:
        if err.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        return 409, {"error": reason_for(err.response["CancellationReasons"], now)}
    account = table.get_item(Key={"customer": customer, "sk": f"ACCT#{item['from_account_id']}"})
    balance = format_cents(account["Item"]["balance_cents"])
    return 200, {"status": "EXECUTED", "transfer_id": transfer_id, "balance": balance}


def reject(table: Any, customer: str, transfer_id: str, now: str) -> Response:
    try:
        table.update_item(
            Key={"customer": customer, "sk": f"XFER#{transfer_id}"},
            UpdateExpression="SET #s = :r, decided_at = :now",
            ConditionExpression="#s = :p",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":r": "REJECTED", ":p": "PENDING", ":now": now},
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # An old item came back: it exists but is not PENDING. None: no such transfer.
        if err.response.get("Item"):
            return 409, {"error": "already decided"}
        return 404, {"error": "no such transfer"}
    return 200, {"status": "REJECTED", "transfer_id": transfer_id}


def revoke_agent(revocations: Any, customer: str, now_epoch: int) -> Response:
    try:
        revocations.put_item(
            Item={
                "customer": customer,
                "client_id": os.environ["AGENT_CLIENT_ID"],
                "revoked_at": now_epoch,
            },
            ConditionExpression="attribute_not_exists(revoked_at) OR revoked_at < :now",
            ExpressionAttributeValues={":now": now_epoch},
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Another revoke already installed this cutoff or a later one. Return the effective
        # cutoff from the failed conditional write; no read/write race or extra IAM needed.
        now_epoch = int(_field(err.response["Item"]["revoked_at"], "N"))
    return 200, {"status": "revoked", "revoked_at": now_epoch}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    customer = claims.get("sub")
    route = event.get("routeKey", "")
    transfer_id = (event.get("pathParameters") or {}).get("id")
    # The authorizer checked the audience; we re-check, so a wiring mistake cannot open the door.
    if (
        claims.get("client_id") != os.environ["APP_CLIENT_ID"]
        or claims.get("token_use") != "access"
        or not isinstance(customer, str)
        or not customer
    ):
        status, body = 403, {"error": "forbidden"}
    elif transfer_id is not None and not TRANSFER_ID.fullmatch(transfer_id):
        status, body = 404, {"error": "no such transfer"}
    else:
        status, body = _route(route, customer, claims, transfer_id, event)
    # One audit line per call. Never the token, the hash or the body.
    logger.info(
        json.dumps(
            {
                "subject": customer,
                "client_id": claims.get("client_id"),
                "action": route,
                "transfer_id": transfer_id,
                "outcome": status,
            }
        )
    )
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _route(
    route: str,
    customer: str,
    claims: dict[str, Any],
    transfer_id: str | None,
    event: dict[str, Any],
) -> Response:
    if route not in ROUTES:
        return 404, {"error": "not found"}  # before assuming the approver role
    now, epoch = now_iso(), _epoch()
    table, revocations = _session(customer)
    if route == "GET /transfers":
        return list_transfers(table, customer, now)
    if route == "POST /agent/revoke":
        return revoke_agent(revocations, customer, epoch)
    if transfer_id is None:
        return 404, {"error": "not found"}
    if route == "GET /transfers/{id}":
        return get_transfer(table, customer, transfer_id, now)
    if route == "POST /transfers/{id}/reject":
        return reject(table, customer, transfer_id, now)
    if route == "POST /transfers/{id}/approve":
        window = int(os.environ["FRESH_SIGN_IN_SECONDS"])
        if not is_fresh(claims.get("auth_time"), epoch, window):
            return 403, {"error": "sign in again"}
        try:
            body = json.loads(event.get("body") or "{}")
        except ValueError:
            return 400, {"error": "invalid input: body"}
        if not isinstance(body, dict):
            return 400, {"error": "invalid input: body"}
        return approve(table, customer, transfer_id, body, now)
    return 404, {"error": "not found"}


def _epoch() -> int:
    return int(time.time())
