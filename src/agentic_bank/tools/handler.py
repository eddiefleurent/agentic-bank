"""Bank tools Lambda: reads, proposing a transfer (never executing one), disputes, policy answers.

The gateway passes the tool arguments as the event (including `customer`, set by the interceptor)
and the tool name in the client context."""

import os
import secrets
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from agentic_bank.knowledge import service as knowledge
from agentic_bank.shared.data import format_cents, scoped_session, table_for
from agentic_bank.shared.pii import Tokenizer
from agentic_bank.shared.transfers import MAX_CENTS, now_iso, transfer_hash
from agentic_bank.tools import disputes

ROUTING_NUMBER = "990000013"
NO_SUCH_ACCOUNT = {"error": "no such account"}
NO_SUCH_PAIR = {"error": "no such account or payee"}  # one message so IDs cannot be probed
PROPOSAL_LIFETIME_SECONDS = 1800
MEMO_MAX = 80  # the ACH addenda limit


def get_accounts(table: Any, customer: str) -> dict[str, Any]:
    items = table.query(
        KeyConditionExpression=Key("customer").eq(customer) & Key("sk").begins_with("ACCT#")
    )["Items"]
    return {
        "accounts": [
            {
                "account_id": item["sk"].removeprefix("ACCT#"),
                "name": item["name"],
                "type": item["type"],
                "routing_number": ROUTING_NUMBER,
                "account_last4": item["account_number"][-4:],
                "balance": format_cents(item["balance_cents"]),
                "currency": item["currency"],
            }
            for item in items
        ]
    }


def list_transactions(table: Any, customer: str, event: dict[str, Any]) -> dict[str, Any]:
    account_id = event.get("account_id")
    if not isinstance(account_id, str) or not disputes.ID.fullmatch(account_id):
        return {"error": "invalid input: account_id"}
    limit = event.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return {"error": "invalid input: limit"}
    limit = min(limit, 50)

    # Someone else's account is just an account we cannot find in this customer's partition.
    if "Item" not in table.get_item(Key={"customer": customer, "sk": f"ACCT#{account_id}"}):
        return NO_SUCH_ACCOUNT
    items = table.query(
        KeyConditionExpression=Key("customer").eq(customer)
        & Key("sk").begins_with(f"TXN#{account_id}#"),
        ScanIndexForward=False,  # sort key starts with the date: newest first
        Limit=limit,
    )["Items"]
    transactions = []
    for item in items:
        _, _, posted_on, transaction_id = item["sk"].split("#", 3)
        txn = {
            "transaction_id": transaction_id,
            "posted_on": posted_on,
            "description": item["description"],
            "amount": format_cents(item["amount_cents"]),
            "currency": "USD",
            "channel": item["channel"],
        }
        if item.get("card_last4"):
            txn["card_last4"] = item["card_last4"]
        transactions.append(txn)
    return {"account_id": account_id, "transactions": transactions}


def list_payees(table: Any, customer: str) -> dict[str, Any]:
    items = table.query(
        KeyConditionExpression=Key("customer").eq(customer) & Key("sk").begins_with("PAYEE#")
    )["Items"]
    keys = ("name", "bank_name", "routing_number", "account_last4")
    return {
        "payees": [
            {"payee_id": item["sk"].removeprefix("PAYEE#"), **{k: item[k] for k in keys}}
            for item in items
        ]
    }


def parse_proposal(event: dict[str, Any]) -> dict[str, Any]:
    """Validated arguments of propose_transfer, or {"error": ...} naming the field only."""
    for field in ("from_account_id", "payee_id"):
        value = event.get(field)
        if not isinstance(value, str) or not disputes.ID.fullmatch(value):
            return {"error": f"invalid input: {field}"}
    amount = event.get("amount_cents")
    # bool is an int subclass in Python: refuse it explicitly.
    if not isinstance(amount, int) or isinstance(amount, bool) or not 1 <= amount <= MAX_CENTS:
        return {"error": "invalid input: amount_cents"}
    memo = event.get("memo", "")
    if not isinstance(memo, str) or len(memo) > MEMO_MAX or not memo.isprintable():
        return {"error": "invalid input: memo"}
    return {
        "from_account_id": event["from_account_id"],
        "payee_id": event["payee_id"],
        "amount_cents": amount,
        "memo": memo,
    }


def propose_transfer(
    reader: Any, proposer: Any, customer: str, proposal: dict[str, Any]
) -> dict[str, Any]:
    """Record a PENDING transfer. Nothing moves: only the customer's approval executes it."""
    account = reader.get_item(
        Key={"customer": customer, "sk": f"ACCT#{proposal['from_account_id']}"}
    )
    payee = reader.get_item(Key={"customer": customer, "sk": f"PAYEE#{proposal['payee_id']}"})
    if "Item" not in account or "Item" not in payee:
        return NO_SUCH_PAIR
    transfer_id = f"xfer_{secrets.token_hex(8)}"
    expires_at = now_iso(PROPOSAL_LIFETIME_SECONDS)
    proposer.put_item(
        Item={
            "customer": customer,
            "sk": f"XFER#{transfer_id}",
            **proposal,
            "status": "PENDING",
            "created_at": now_iso(),
            "expires_at": expires_at,
            "request_hash": transfer_hash(customer, transfer_id, **proposal, expires_at=expires_at),
        },
        ConditionExpression="attribute_not_exists(sk)",
    )
    return {
        "transfer_id": transfer_id,
        "status": "pending_approval",
        "amount": format_cents(proposal["amount_cents"]),
        "payee_name": payee["Item"]["name"],
        "expires_at": expires_at,
        "next": "Ask the customer to approve it in the Agentic Bank app.",
    }


def _tokenizer() -> Tokenizer:
    vault = boto3.resource("dynamodb").Table(os.environ["VAULT_TABLE"])
    return Tokenizer(
        boto3.client("comprehend"), boto3.client("kms"), vault, os.environ["PII_KEY_ID"]
    )


CASE_WRITES = ["dynamodb:PutItem", "dynamodb:UpdateItem"]


def _case_writer(customer: str) -> Any:
    """Low-level client for this customer's partition, limited to case attributes."""
    arn = os.environ["TABLE_ARN"]
    session = scoped_session(
        "DATA_WRITER_ROLE_ARN", customer, arn, CASE_WRITES, disputes.CASE_ATTRIBUTES
    )
    return session.client("dynamodb")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    customer = event.get("customer")
    if not isinstance(customer, str) or not customer:
        return {"error": "invalid input: customer"}
    tool = context.client_context.custom["bedrockAgentCoreToolName"].rpartition("___")[2]
    if tool == "ask_policy":  # bank documents, not customer rows: no per-customer role session
        return knowledge.ask_policy_tool(event)
    if tool == "propose_transfer":
        proposal = parse_proposal(event)
        if "error" in proposal:
            return proposal
        proposer = table_for(customer, "PROPOSER_ROLE_ARN", ["dynamodb:PutItem"])
        return propose_transfer(table_for(customer), proposer, customer, proposal)
    table = table_for(customer)
    if tool == "explain_charge":
        return disputes.explain_charge(table, customer, event)
    if tool == "get_dispute":
        return disputes.get_dispute(table, customer, event)
    if tool == "dispute_transaction":
        sfn = boto3.client("stepfunctions")
        writer = _case_writer(customer)
        return disputes.dispute_transaction(table, writer, sfn, _tokenizer(), customer, event)
    if tool == "answer_dispute":
        sfn = boto3.client("stepfunctions")
        writer = _case_writer(customer)
        return disputes.answer_dispute(table, writer, sfn, _tokenizer(), customer, event)
    if tool == "list_payees":
        return list_payees(table, customer)
    if tool == "get_accounts":
        return get_accounts(table, customer)
    if tool == "list_transactions":
        return list_transactions(table, customer, event)
    return {"error": "unknown tool"}
