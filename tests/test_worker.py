import pytest

from agentic_bank.disputes import worker
from agentic_bank.shared.ddb import ser


class Client:
    """DynamoDB returns numbers as Decimals: build must still hash and return JSON."""

    def __init__(self):
        self.case = {
            "customer": "alex",
            "sk": "CASE#dsp_t_alex_006",
            "case_id": "dsp_t_alex_006",
            "account_id": "acc_alex_chk",
            "transaction_id": "t_alex_006",
            "amount_cents": 3400,
            "reason_code": "not_received",
            "statement_tok": "I never got it. Call <PHONE_0a1b2c3d>.",
            "status": "REVIEWING",
            "round": 0,
        }
        self.txn = {
            "customer": "alex",
            "sk": "TXN#acc_alex_chk#2026-11-04#t_alex_006",
            "amount_cents": -3400,
            "network_ref": "nr_alex_006",
        }

    def get_item(self, **_):
        return {"Item": {k: ser(v) for k, v in self.case.items()}}

    def query(self, **_):
        return {"Items": [{k: ser(v) for k, v in self.txn.items()}]}


def test_build_from_dynamodb_rows():
    built = worker.build(Client(), "t", "alex", "dsp_t_alex_006")
    assert built["code_scores"] == {"C1": 1}
    assert built["amount_cents"] == 3400 and type(built["amount_cents"]) is int
    assert len(built["sha256"]) == 64


def test_staff_settle_needs_the_analyst_reason(monkeypatch):
    """A replayed task token (auto=false) cannot pay unless the analyst function wrote a reason."""
    case = Client().case
    paid = []
    monkeypatch.setenv("TABLE_ARN", "arn:aws:dynamodb:us-east-1:000000000000:table/t")
    monkeypatch.setattr(worker, "writer_client", lambda customer: None)
    monkeypatch.setattr(worker, "_case", lambda *_: case)
    monkeypatch.setattr(worker, "find_txn", lambda *_: {})
    monkeypatch.setattr(worker.settle, "settle", lambda *_, **kw: paid.append(kw) or {"settled": 1})
    event = {"action": "settle", "customer": "alex", "case_id": "dsp_t_alex_006", "auto": False}
    event["decided_by"] = "staff:x"
    with pytest.raises(worker.settle.SettleRefused):
        worker.handler(event)
    assert paid == []
    case["analyst_reason"] = "carrier has no proof"
    assert worker.handler(event) == {"settled": 1}
