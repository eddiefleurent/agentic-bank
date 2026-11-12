"""The four dispute tools: explain_charge, dispute_transaction, get_dispute, answer_dispute.

Reads go through the customer's data-reader session. Writes go through the case-writer role.
That role's policy allows only case attributes, so omitting the session Policy still cannot name
balance_cents, description or channel. Customer isolation (LeadingKeys) is still only the session
policy. The customer's free text is tokenized before it is stored or sent anywhere; the tool
never logs its arguments.
"""

import json
import os
import re
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from agentic_bank.disputes import questions
from agentic_bank.disputes.waits import AlreadyResumed, CallbackUnconfirmed, resume
from agentic_bank.network.order_insight import FixtureOrderInsight
from agentic_bank.shared.data import format_cents
from agentic_bank.shared.ddb import ser
from agentic_bank.shared.pii import TokenizeError

ID = re.compile(r"^[a-z0-9_]{1,32}$")
CASE_ID = re.compile(r"^dsp_[a-z0-9_]{1,32}$")
REASONS = ("not_received", "not_recognised", "other")
TEXT_MAX = 1000
WINDOW_DAYS = 120
# Attributes the tools session may name. The case-writer role policy enforces this same allowlist
# even when the caller omits Policy. Which customer (LeadingKeys) is still session-policy only.
CASE_ATTRIBUTES = [
    "customer",
    "sk",
    "case_id",
    "account_id",
    "transaction_id",
    "amount_cents",
    "reason_code",
    "statement_tok",
    "answer_tok",
    "status",
    "round",
    "created_at",
    "execution",
    "task_token",
    "analyst_queue",
]
NO_TXN = {"error": "no such transaction"}
NO_CASE = {"error": "no such case"}
NO_QUESTION = {"error": "no question is waiting on this case"}
TRY_AGAIN = {"error": "the dispute could not be recorded right now; try again"}
DELIVERY_UNCONFIRMED = {"error": "answer delivery is unconfirmed; check the dispute status"}
STATUS_TEXT = {
    "REVIEWING": "Under review. Check again in about 20 seconds.",
    "ANALYST_REVIEW": "A specialist is reviewing your case.",
    "CLOSED_NO_REFUND": "A specialist closed the case without a refund.",
}


def find_txn(table: Any, customer: str, transaction_id: str, account_id: str = "") -> dict | None:
    """The customer's TXN row for this id (sort key TXN#<account>#<date>#<id>), or None."""
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("customer").eq(customer)
        & Key("sk").begins_with(f"TXN#{account_id}#" if account_id else "TXN#")
    }
    while True:
        page = table.query(**kwargs)
        for item in page["Items"]:
            _, account, posted_on, txn_id = item["sk"].split("#", 3)
            if txn_id == transaction_id:
                return {
                    **item,
                    "account_id": account,
                    "posted_on": posted_on,
                    "transaction_id": txn_id,
                }
        if "LastEvaluatedKey" not in page:
            return None
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def explain_charge(reader: Any, customer: str, event: dict[str, Any]) -> dict[str, Any]:
    txn_id = event.get("transaction_id")
    if not isinstance(txn_id, str) or not ID.fullmatch(txn_id):
        return {"error": "invalid input: transaction_id"}
    txn = find_txn(reader, customer, txn_id)  # None if it is not this customer's
    if txn is None:
        return NO_TXN
    order = FixtureOrderInsight().lookup(txn["network_ref"]) if txn.get("network_ref") else None
    return {
        "transaction_id": txn["transaction_id"],
        "amount": format_cents(txn["amount_cents"]),
        "order": order.to_facts() if order else None,
        "posted_on": txn["posted_on"],
    }


def _text(event: dict[str, Any], field: str) -> str | None:
    value = event.get(field)
    if isinstance(value, str) and 0 < len(value.strip()) and len(value) <= TEXT_MAX:
        return value
    return None


def start(sfn: Any, customer: str, case: dict[str, Any]) -> None:
    """Start the case's workflow. A repeat with the same name is success, so a crash between the
    case write and this call heals on the customer's retry."""
    try:
        sfn.start_execution(
            stateMachineArn=os.environ["DISPUTE_STATE_MACHINE_ARN"],
            name=case["execution"],
            input=json.dumps({"customer": customer, "case_id": case["case_id"]}),
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ExecutionAlreadyExists":
            raise


def dispute_transaction(
    reader: Any, writer: Any, sfn: Any, tokenizer: Any, customer: str, event: dict[str, Any]
) -> dict[str, Any]:
    account_id, txn_id = event.get("account_id"), event.get("transaction_id")
    if not isinstance(account_id, str) or not ID.fullmatch(account_id):
        return {"error": "invalid input: account_id"}
    if not isinstance(txn_id, str) or not ID.fullmatch(txn_id):
        return {"error": "invalid input: transaction_id"}
    reason = event.get("reason_code")
    if reason not in REASONS:
        return {"error": "invalid input: reason_code"}
    statement = _text(event, "statement")
    if statement is None:
        return {"error": "invalid input: statement (1 to 1000 characters)"}
    case_id = f"dsp_{txn_id}"
    existing = reader.get_item(Key={"customer": customer, "sk": f"CASE#{case_id}"}).get("Item")
    if existing:  # one case per transaction ever; a retry restarts nothing that already ran
        start(sfn, customer, existing)
        return {"case_id": case_id, "status": existing["status"], "note": "already disputed"}
    txn = find_txn(reader, customer, txn_id, account_id)
    if txn is None:
        return NO_TXN
    if txn.get("channel") != "card" or int(txn["amount_cents"]) >= 0:
        return {"error": "only card charges can be disputed here"}
    if date.fromisoformat(txn["posted_on"]) < datetime.now(UTC).date() - timedelta(WINDOW_DAYS):
        return {"error": f"charges older than {WINDOW_DAYS} days cannot be disputed"}
    try:
        statement_tok = tokenizer.tokenize(statement, case_id)
    except TokenizeError:
        return TRY_AGAIN  # fail closed: no case, nothing stored in clear
    case = {
        "customer": customer,
        "sk": f"CASE#{case_id}",
        "case_id": case_id,
        "account_id": account_id,
        "transaction_id": txn_id,
        "amount_cents": -int(txn["amount_cents"]),
        "reason_code": reason,
        "statement_tok": statement_tok,
        "status": "REVIEWING",
        "round": 0,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        # Standard workflow names stay taken for 90 days: a suffix lets a reset fixture run again.
        "execution": f"{case_id}-{secrets.token_hex(4)}",
    }
    try:
        writer.put_item(
            TableName=os.environ["TABLE_ARN"].rpartition("/")[2],
            Item={k: ser(v) for k, v in case.items()},
            ConditionExpression="attribute_not_exists(sk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        case = reader.get_item(
            Key={"customer": customer, "sk": f"CASE#{case_id}"}, ConsistentRead=True
        )["Item"]  # a concurrent call won: continue with its case
    start(sfn, customer, case)
    return {
        "case_id": case_id,
        "status": "REVIEWING",
        "next": "Call get_dispute with this case_id in about 20 seconds.",
    }


def get_dispute(reader: Any, customer: str, event: dict[str, Any]) -> dict[str, Any]:
    case_id = event.get("case_id")
    if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
        return {"error": "invalid input: case_id"}
    case = reader.get_item(Key={"customer": customer, "sk": f"CASE#{case_id}"}).get("Item")
    if case is None:
        return NO_CASE
    status = case["status"]
    out = {
        "case_id": case_id,
        "transaction_id": case["transaction_id"],
        "amount": format_cents(case["amount_cents"]),
        "status": status,
    }
    if status == "INFO_REQUESTED":
        out["question"] = questions.question_text(case["question_id"])
        out["next"] = "Ask the customer, then call answer_dispute with their reply."
    elif status == "REFUNDED":
        out["message"] = (
            f"Refunded {format_cents(case['refund_cents'])} to account {case['account_id']}."
        )
    else:
        out["message"] = STATUS_TEXT.get(status, "")
    return out  # never task_token, execution or the decision record


def answer_dispute(
    reader: Any, writer: Any, sfn: Any, tokenizer: Any, customer: str, event: dict
) -> dict[str, Any]:
    case_id = event.get("case_id")
    if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
        return {"error": "invalid input: case_id"}
    answer = _text(event, "answer")
    if answer is None:
        return {"error": "invalid input: answer (1 to 1000 characters)"}
    case = reader.get_item(Key={"customer": customer, "sk": f"CASE#{case_id}"}).get("Item")
    if case is None:
        return NO_CASE
    if case["status"] != "INFO_REQUESTED":
        return NO_QUESTION  # checked before tokenizing: no Comprehend call for a refused answer
    try:
        answer_tok = tokenizer.tokenize(answer, case_id)
    except TokenizeError:
        return TRY_AGAIN
    try:
        resume(
            writer,
            sfn,
            os.environ["TABLE_ARN"].rpartition("/")[2],
            customer,
            case_id,
            expected_status="INFO_REQUESTED",
            sets={"status": "REVIEWING", "answer_tok": answer_tok, "round": 1},
            output={"answered": True},
        )
    except CallbackUnconfirmed:
        return DELIVERY_UNCONFIRMED  # delivery is unknown; do not tell the caller to retry
    except AlreadyResumed:
        return NO_QUESTION
    return {
        "case_id": case_id,
        "status": "REVIEWING",
        "next": "Call get_dispute in about 20 seconds.",
    }
