from datetime import UTC, datetime

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from agentic_bank.disputes.settle import SettleRefused, settle

NOW = datetime(2026, 11, 11, tzinfo=UTC)
CASE = {"case_id": "dsp_t1", "account_id": "acc_a", "transaction_id": "t1", "status": "REVIEWING"}
TXN = {"amount_cents": -3400}
ser = TypeSerializer().serialize


def cancelled(codes):
    reasons = [{"Code": c} for c in codes]
    return ClientError(
        {"Error": {"Code": "TransactionCanceledException"}, "CancellationReasons": reasons},
        "TransactWriteItems",
    )


class Dynamo:
    """Items by sk; transact_write_items replays scripted outcomes (None = success)."""

    def __init__(self, items=None, outcomes=()):
        self.items, self.outcomes, self.writes = dict(items or {}), list(outcomes), []

    def get_item(self, TableName, Key, ConsistentRead):  # noqa: N803
        row = self.items.get(Key["sk"]["S"])
        return {"Item": {k: ser(v) for k, v in row.items()}} if row else {}

    def transact_write_items(self, TransactItems):  # noqa: N803
        self.writes.append(TransactItems)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, ClientError):
            raise outcome
        if isinstance(outcome, dict):  # side effect of a racing settle
            self.items.update(outcome)


def run(db, case=CASE, txn=TXN, auto=True):
    return settle(db, "t", "cust", case, txn, auto=auto, decided_by="code", now=NOW)


def slot_key(item):
    return item["Put"]["Item"]["sk"]["S"]


def recent():
    return {"refunded_on": int(NOW.timestamp()) - 3600}


def test_auto_refund_claims_first_slot_last_in_the_transaction():
    db = Dynamo()
    assert run(db) == {"settled": True, "replayed": False, "amount_cents": 3400}
    items = db.writes[0]
    assert len(items) == 4 and slot_key(items[-1]) == "REFUNDQ#1"
    assert items[1]["Update"]["ExpressionAttributeValues"][":c"] == {"N": "3400"}
    assert "attribute_not_exists" in items[-1]["Put"]["ConditionExpression"]


def test_stale_slot_is_reused_and_recent_slot_skipped():
    old = {"refunded_on": int(NOW.timestamp()) - 91 * 86400}
    db = Dynamo({"REFUNDQ#1": recent(), "REFUNDQ#2": old})
    run(db)
    assert slot_key(db.writes[0][-1]) == "REFUNDQ#2"


def test_lost_slot_race_retries_the_other_slot_once():
    race = cancelled(["None", "None", "None", "ConditionalCheckFailed"])
    db = Dynamo(outcomes=[race])
    # first attempt picks slot 1; a rival took it meanwhile, so the re-read sends us to slot 2
    orig = db.transact_write_items

    def rival(TransactItems):  # noqa: N803
        try:
            orig(TransactItems)
        finally:
            db.items["REFUNDQ#1"] = recent()

    db.transact_write_items = rival
    assert run(db)["settled"] is True
    assert [slot_key(w[-1]) for w in db.writes] == ["REFUNDQ#1", "REFUNDQ#2"]


def test_both_slots_full_routes_to_analyst_without_writing():
    db = Dynamo({"REFUNDQ#1": recent(), "REFUNDQ#2": recent()})
    assert run(db)["route"] == "analyst"
    assert db.writes == []


def test_analyst_approval_uses_no_slot():
    db = Dynamo({"REFUNDQ#1": recent(), "REFUNDQ#2": recent()})
    case = {**CASE, "status": "ANALYST_REVIEW"}
    assert run(db, case=case, txn={"amount_cents": -8900}, auto=False)["amount_cents"] == 8900
    assert len(db.writes[0]) == 3


def test_replay_returns_stored_result_without_a_second_write():
    done = {**CASE, "status": "REFUNDED", "refund_cents": 3400}
    db = Dynamo()
    assert run(db, case=done)["replayed"] is True and db.writes == []
    # a concurrent settle won: our transaction is cancelled and the case now reads REFUNDED
    db = Dynamo(
        {"CASE#dsp_t1": done}, [cancelled(["ConditionalCheckFailed", "None", "None", "None"])]
    )
    assert run(db) == {"settled": True, "replayed": True, "amount_cents": 3400}


def test_auto_refund_above_limit_is_refused():
    with pytest.raises(SettleRefused):
        run(Dynamo(), txn={"amount_cents": -5001})
