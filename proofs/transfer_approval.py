"""Proof: the agent can propose but never move money; only a fresh MFA sign-in in the bank app
executes, exactly once, and only for exactly what was proposed.

    uv run python proofs/transfer_approval.py alex

Needs the deployed stack, admin AWS credentials (DynamoDB read/write, SSM, Cognito,
API Gateway read) and credential files for alex and sam (scripts/setup_customer.py). Takes a few
minutes: each sign-in waits for a fresh TOTP window. Seed balances are create-only;
proofs assert deltas rather than starting balances.
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any
from urllib.parse import quote

import boto3
from _common import REGION, Stack, bank_app, call_tool, check, mcp_post, rpc_error_code, run

TABLE = "agentic-bank-data"
LANDLORD = "pay_alex_landlord"
ddb = boto3.client("dynamodb", region_name=REGION)


def balance(sub: str, account: str) -> int:
    item = ddb.get_item(
        TableName=TABLE, Key={"customer": {"S": sub}, "sk": {"S": f"ACCT#{account}"}}
    )["Item"]
    return int(item["balance_cents"]["N"])


def sk_rows(sub: str, prefix: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "TableName": TABLE,
        "KeyConditionExpression": "customer = :c AND begins_with(sk, :p)",
        "ExpressionAttributeValues": {":c": {"S": sub}, ":p": {"S": prefix}},
        "ConsistentRead": True,
    }
    while True:
        page = ddb.query(**kwargs)
        rows += page["Items"]
        if "LastEvaluatedKey" not in page:
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def xfer(sub: str, transfer_id: str) -> dict[str, Any]:
    return ddb.get_item(
        TableName=TABLE,
        Key={"customer": {"S": sub}, "sk": {"S": f"XFER#{transfer_id}"}},
        ConsistentRead=True,
    )["Item"]


def debit_rows(sub: str, account: str, transfer_id: str) -> list[dict[str, Any]]:
    return [
        r for r in sk_rows(sub, f"TXN#{account}#") if r["sk"]["S"].endswith(f"#t_{transfer_id}")
    ]


def propose(
    stack: Stack,
    token: str,
    account: str,
    payee: str,
    cents: Any,
    memo: str = "proof transfer",
) -> tuple[int, dict[str, Any]]:
    return call_tool(
        stack.gateway_url,
        token,
        "propose_transfer",
        {"from_account_id": account, "payee_id": payee, "amount_cents": cents, "memo": memo},
    )


def listed(token: str, transfer_id: str) -> dict[str, Any] | None:
    """This customer's transfer, from GET /transfers/{id}. The token's sub is the owner."""
    status, reply = bank_app.api("GET", f"/transfers/{quote(transfer_id, safe='')}", token)
    if status != 200 or not isinstance(reply, dict):
        return None
    item = reply.get("transfer")
    if not isinstance(item, dict) or item.get("transfer_id") != transfer_id:
        return None
    return item


def must_listed(token: str, transfer_id: str) -> dict[str, Any]:
    """The transfer the proof is about. A missing row is a failed proof, not a later KeyError."""
    item = listed(token, transfer_id)
    if item is None:
        raise SystemExit(f"transfer {transfer_id} was not returned by GET /transfers/{{id}}")
    return item


def approve(token: str, transfer_id: str, request_hash: str) -> tuple[int, Any]:
    return bank_app.api(
        "POST", f"/transfers/{transfer_id}/approve", token, {"request_hash": request_hash}
    )


def error_of(reply: Any) -> str:
    return str(reply.get("error", "")) if isinstance(reply, dict) else ""


def new_proposal(stack: Stack, agent: str, account: str, payee: str, cents: int) -> str:
    _, out = propose(stack, agent, account, payee, cents)
    return str(out.get("transfer_id", ""))


def main(user: str) -> None:
    stack = Stack()
    sub = stack.subject(user)
    acct = f"acc_{user}_chk"
    agent = stack.token_with_scope(user)
    app = stack.app_token(user)

    # 1. Propose: pending, nothing moves, the app lists it with an intact hash.
    start = balance(sub, acct)
    _, out = propose(stack, agent, acct, LANDLORD, 50000, "December rent")
    tid = str(out.get("transfer_id", ""))
    check(
        "agent proposes $500: status pending_approval, amount 500.00",
        out.get("status") == "pending_approval" and out.get("amount") == "500.00" and bool(tid),
    )
    check("proposal moves no money", balance(sub, acct) == start)
    item = listed(app, tid)
    check(
        "app reads it by id: PENDING, intact, payee from the bank's own record",
        item is not None
        and item["transfer_id"] == tid
        and item["status"] == "PENDING"
        and item["intact"] is True
        and item["payee_name"] == "Riverside Property Mgmt"
        and item["payee_last4"] == "7781",
    )
    check(
        "CLI recomputes the hash from the displayed fields and it matches",
        item is not None and bank_app.recompute_hash(sub, item) == item["request_hash"],
    )
    h = item["request_hash"] if item else "0" * 64

    # 2. Approve once: executed, debited exactly once, one ACH row.
    status, reply = approve(app, tid, h)
    check("approve with fresh MFA sign-in: 200", status == 200)
    check("balance fell by exactly 50000", balance(sub, acct) == start - 50000)
    rows = debit_rows(sub, acct, tid)
    check(
        "exactly one ACH TRANSFER TO RIVERSIDE row, -50000",
        len(rows) == 1
        and rows[0]["description"]["S"].startswith("ACH TRANSFER TO RIVERSIDE")
        and int(rows[0]["amount_cents"]["N"]) == -50000
        and rows[0]["channel"]["S"] == "ach",
    )

    # 3. Replay: same approve again changes nothing.
    after = balance(sub, acct)
    status, reply = approve(app, tid, h)
    check(
        "replay of the same approve: 409 already decided",
        status == 409 and "already decided" in error_of(reply),
    )
    check(
        "replay: balance unchanged, still one debit row",
        balance(sub, acct) == after and len(debit_rows(sub, acct, tid)) == 1,
    )

    # 4. Race: two simultaneous approvals of one fresh proposal.
    race = new_proposal(stack, agent, acct, LANDLORD, 10000)
    before = balance(sub, acct)
    barrier = Barrier(2)

    def go() -> int:
        barrier.wait()
        return approve(app, race, must_listed(app, race)["request_hash"])[0]

    with ThreadPoolExecutor(2) as pool:
        codes = sorted(f.result() for f in [pool.submit(go), pool.submit(go)])
    check("two simultaneous approvals: exactly one 200 and one 409", codes == [200, 409])
    check(
        "race: exactly one debit of 10000 and one TXN row",
        balance(sub, acct) == before - 10000 and len(debit_rows(sub, acct, race)) == 1,
    )

    # 5. Edited amount: an admin raises amount_cents after the proposal.
    edited = new_proposal(stack, agent, acct, LANDLORD, 10000)
    listed_hash = must_listed(app, edited)["request_hash"]
    ddb.update_item(
        TableName=TABLE,
        Key={"customer": {"S": sub}, "sk": {"S": f"XFER#{edited}"}},
        UpdateExpression="SET amount_cents = :a",
        ExpressionAttributeValues={":a": {"N": "500000"}},
    )
    before = balance(sub, acct)
    item = listed(app, edited)
    check(
        "edited amount: listed intact false, and the CLI's recomputed hash differs",
        item is not None
        and item["transfer_id"] == edited
        and item["intact"] is False
        and item["amount_cents"] == 500000
        and bank_app.recompute_hash(sub, item) != item["request_hash"],
    )
    status, reply = approve(app, edited, listed_hash)
    check(
        "edited amount: approve with the listed hash is 409 request changed",
        status == 409 and "request changed" in error_of(reply),
    )
    check(
        "edited amount: nothing moved, still PENDING",
        balance(sub, acct) == before and xfer(sub, edited)["status"]["S"] == "PENDING",
    )

    # 6. Wrong hash (what the customer did not see).
    wrong = new_proposal(stack, agent, acct, LANDLORD, 10000)
    before = balance(sub, acct)
    status, reply = approve(app, wrong, "0" * 64)
    check(
        "wrong request_hash: 409 request changed, nothing moved",
        status == 409 and "request changed" in error_of(reply) and balance(sub, acct) == before,
    )

    # 7. Reject, then approve.
    status, _ = bank_app.api("POST", f"/transfers/{wrong}/reject", app, {})
    check("reject a pending proposal: 200", status == 200)
    status, reply = approve(app, wrong, must_listed(app, wrong)["request_hash"])
    check(
        "approve after reject: 409 already decided, nothing moved",
        status == 409 and "already decided" in error_of(reply) and balance(sub, acct) == before,
    )

    # 8. Limits and unknown payee at propose time.
    count = len(sk_rows(sub, "XFER#"))
    _, out = propose(stack, agent, acct, LANDLORD, 100001)
    status, reply = mcp_post(
        stack.gateway_url,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "bank___propose_transfer",
                "arguments": {
                    "from_account_id": acct,
                    "payee_id": LANDLORD,
                    "amount_cents": 100001,
                    "memo": "too much",
                },
            },
        },
        agent,
    )
    check(
        "$1,000.01 proposal: Cedar refusal (-32002)",
        "transfer_id" not in out and rpc_error_code(reply) == -32002,
    )
    check("$1,000.01 proposal: no XFER item created", len(sk_rows(sub, "XFER#")) == count)
    _, out = propose(stack, agent, acct, "pay_sam_gym", 100)
    check(
        "another customer's payee: 'no such account or payee'",
        out.get("error") == "no such account or payee" and len(sk_rows(sub, "XFER#")) == count,
    )

    # 9. Walls between the two doors.
    status, _ = bank_app.api("GET", "/transfers", agent)
    check("agent token on the bank app API: 401", status == 401)
    status, _ = mcp_post(
        stack.gateway_url, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, app
    )
    # AgentCore refuses a client outside allowed_clients with 403 insufficient_scope (not 401),
    # before the interceptor runs; a token the gateway accepts gets 200 for tools/list.
    check("app token at the MCP gateway: refused by the gateway (403)", status in (401, 403))

    # 10. Insufficient funds (sam, $310.25 balance): 409 and the proposal stays PENDING.
    sam_agent = stack.token_with_scope("sam")
    sam_app = stack.app_token("sam")
    sam_sub = stack.subject("sam")
    sam_id = new_proposal(stack, sam_agent, "acc_sam_chk", "pay_sam_gym", 50000)
    sam_item = listed(sam_app, sam_id)
    sam_before = balance(sam_sub, "acc_sam_chk")
    status, reply = approve(sam_app, sam_id, sam_item["request_hash"]) if sam_item else (0, None)
    check(
        "approve $500 from sam's $310.25: 409 insufficient funds",
        sam_item is not None
        and sam_item["transfer_id"] == sam_id
        and status == 409
        and "insufficient funds" in error_of(reply),
    )
    check(
        "insufficient funds: balance unchanged, proposal still PENDING, no TXN row",
        balance(sam_sub, "acc_sam_chk") == sam_before
        and xfer(sam_sub, sam_id)["status"]["S"] == "PENDING"
        and not debit_rows(sam_sub, "acc_sam_chk", sam_id),
    )
    bank_app.api("POST", f"/transfers/{sam_id}/reject", sam_app, {})  # tidy up


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: transfer_approval.py <user>   (alex)")
    run(lambda: main(sys.argv[1]))
