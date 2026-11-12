"""Settlement: the one place a dispute refund is paid. One TransactWriteItems, all or nothing.

Items (in this order; the slot claim must stay last, see `_lost_slot_race`):
  0 case        status IN (REVIEWING, ANALYST_REVIEW) -> REFUNDED  (stops a second settle)
  1 balance     + refund_cents, read from the TXN row, never from the caller
  2 refund row  attribute_not_exists (a second refund row is impossible)
  3 slot        auto refunds only: REFUNDQ#1 or #2, claimed if absent or older than 90 days

Two slots = at most 2 automatic refunds per 90 days, enforced inside the transaction, not by a
read beforehand. The writer client must be assumed per customer (see worker.writer_client).
"""

from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError

from agentic_bank.disputes.decide import AUTO_REFUND_MAX_CENTS
from agentic_bank.shared.ddb import get as _get
from agentic_bank.shared.ddb import ser as _v

WINDOW_SECONDS = 90 * 24 * 3600
SLOTS = (1, 2)
ANALYST: dict[str, Any] = {
    "settled": False,
    "route": "analyst",
    "reason": "auto refund cap reached",
}


class SettleRefused(Exception):
    """A rule stopped the refund. Nothing was written."""


def free_slot(client: Any, table: str, customer: str, now: datetime) -> int | None:
    """First slot empty or older than 90 days. A choice, not a claim."""
    cutoff = int(now.timestamp()) - WINDOW_SECONDS
    for slot in SLOTS:
        row = _get(client, table, customer, f"REFUNDQ#{slot}")
        if row is None or int(row["refunded_on"]) < cutoff:
            return slot
    return None


def settle_items(
    *,
    table: str,
    customer: str,
    case: dict[str, Any],
    cents: int,
    decided_by: str,
    slot: int | None,
    now: datetime,
) -> list[dict[str, Any]]:
    case_id = case["case_id"]
    refund_sk = f"TXN#{case['account_id']}#{now.date().isoformat()}#r_{case_id}"
    items: list[dict[str, Any]] = [
        {
            "Update": {
                "TableName": table,
                "Key": {"customer": _v(customer), "sk": _v(f"CASE#{case_id}")},
                "UpdateExpression": "SET #s = :done, refund_cents = :c, decided_by = :by "
                "REMOVE analyst_queue",
                "ConditionExpression": "#s IN (:r, :a)",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {
                    ":done": _v("REFUNDED"),
                    ":r": _v("REVIEWING"),
                    ":a": _v("ANALYST_REVIEW"),
                    ":c": _v(cents),
                    ":by": _v(decided_by),
                },
            }
        },
        {
            "Update": {
                "TableName": table,
                "Key": {"customer": _v(customer), "sk": _v(f"ACCT#{case['account_id']}")},
                "UpdateExpression": "SET balance_cents = balance_cents + :c",
                "ConditionExpression": "attribute_exists(sk)",
                "ExpressionAttributeValues": {":c": _v(cents)},
            }
        },
        {
            "Put": {
                "TableName": table,
                "Item": {
                    "customer": _v(customer),
                    "sk": _v(refund_sk),
                    "description": _v("Dispute refund"),
                    "channel": _v("refund"),  # list_transactions shows every TXN row's channel
                    "amount_cents": _v(cents),
                    "refund_of": _v(case["transaction_id"]),
                    "case_id": _v(case_id),
                },
                "ConditionExpression": "attribute_not_exists(sk)",
            }
        },
    ]
    if slot is not None:
        items.append(
            {
                "Put": {
                    "TableName": table,
                    "Item": {
                        "customer": _v(customer),
                        "sk": _v(f"REFUNDQ#{slot}"),
                        "case_id": _v(case_id),
                        "refunded_on": _v(int(now.timestamp())),
                    },
                    "ConditionExpression": "attribute_not_exists(sk) OR refunded_on < :cutoff",
                    "ExpressionAttributeValues": {
                        ":cutoff": _v(int(now.timestamp()) - WINDOW_SECONDS)
                    },
                }
            }
        )
    return items


def _lost_slot_race(exc: ClientError, count: int) -> bool:
    """Only the last item (the slot) failed: someone else claimed it between our read and write."""
    reasons = exc.response.get("CancellationReasons") or []
    return (
        len(reasons) == count
        and reasons[0].get("Code") == "None"
        and reasons[-1].get("Code") == "ConditionalCheckFailed"
    )


def settle(
    client: Any,
    table: str,
    customer: str,
    case: dict[str, Any],
    txn: dict[str, Any],
    *,
    auto: bool,
    decided_by: str,
    now: datetime,
) -> dict[str, Any]:
    """Pay the refund, or report ANALYST (auto, no free slot). Idempotent: a replay returns the
    stored result. `txn` is the disputed TXN row (amount_cents negative for a debit)."""
    if case["status"] == "REFUNDED":
        return {"settled": True, "replayed": True, "amount_cents": int(case["refund_cents"])}
    cents = -int(txn["amount_cents"])
    if cents <= 0:
        raise SettleRefused("only a charge can be refunded")
    if auto and cents > AUTO_REFUND_MAX_CENTS:
        raise SettleRefused("above the automatic limit")
    for _ in range(len(SLOTS) if auto else 1):  # a lost race retries once on a fresh read
        slot = free_slot(client, table, customer, now) if auto else None
        if auto and slot is None:
            return dict(ANALYST)
        items = settle_items(
            table=table,
            customer=customer,
            case=case,
            cents=cents,
            decided_by=decided_by,
            slot=slot,
            now=now,
        )
        try:
            client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            again = _get(client, table, customer, f"CASE#{case['case_id']}")
            if again and again["status"] == "REFUNDED":  # replay: already settled
                return {
                    "settled": True,
                    "replayed": True,
                    "amount_cents": int(again["refund_cents"]),
                }
            if slot is not None and _lost_slot_race(exc, len(items)):
                continue
            raise SettleRefused("the case could not be settled") from None
        return {"settled": True, "replayed": False, "amount_cents": cents}
    return dict(ANALYST)
