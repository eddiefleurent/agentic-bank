"""Offline tests for the decision-model second opinion. The HTTP layer is stubbed."""

import json

import pytest

from agentic_bank.aml.decision import (
    MODEL,
    DecisionHTTPError,
    route_band,
    second_opinion,
)

KEY = "test-key-not-a-secret"
NAME = "Quorina Vexley"
MEMO = "memo-secret-phrase-larkspur"
NOTES = "notes-secret-phrase-brindle"
ACCOUNT = "424242424242"
LINKED = "Brindle Holworth"


def planted() -> dict:
    return {
        "case_id": "CASE#A-0001",
        "alert": {"rule_id": "R4", "txn_ids": ["T000001"]},
        "customer": {"customer_id": "C001", "name": NAME},
        "kyc": {
            "occupation": "delivery driver",
            "account_open_date": "2024-01-15",
            "expected_countries": ["US"],
            "notes": f"{NOTES} paid from {ACCOUNT}",
        },
        "flagged_transactions": [
            {
                "txn_id": "T000001",
                "ts": "2026-09-01T10:00:00",
                "direction": "in",
                "amount_cents": 900_000,
                "memo": MEMO,
            }
        ],
        "linked_customers": [{"customer_id": "C002", "name": LINKED, "hop": 1}],
        "counterparties": [
            {"counterparty_id": "X0001", "name": "Selworth Foods", "country": "Belmoria"},
            {"counterparty_id": "X0002", "name": "Hearthside Charity", "country": "US"},
        ],
    }


def ok_body(p_escalate: float = 0.95, p_suspicion: float = 0.9) -> dict:
    return {
        "answers": {
            "disposition": {
                "type": "choice",
                "choice": "escalate" if p_escalate >= 0.5 else "close",
                "probabilities": {"close": round(1 - p_escalate, 6), "escalate": p_escalate},
            },
            "suspicion": {"type": "noul", "noul": p_suspicion},
        },
        "model": MODEL,
        "provider": "Morph",
        "usage": {"cost": 0.0004},
    }


class Transport:
    def __init__(self, results: list) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, body: bytes, headers: dict[str, str], timeout: float) -> bytes:
        self.calls.append({"body": json.loads(body), "headers": dict(headers), "timeout": timeout})
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item).encode()


def test_sent_state_tokenizes_names_and_keeps_notes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    transport = Transport([ok_body()])
    opinion = second_opinion(planted(), transport=transport)
    assert opinion is not None and opinion.p_escalate == 0.95
    body = transport.calls[0]["body"]
    blob = json.dumps(body["state"])
    assert body["model"] == "morph/reflex"
    assert body["provider"] == {"zdr": True}
    for secret in (NAME, LINKED, "Selworth Foods", "Hearthside Charity", ACCOUNT):
        assert secret not in blob
    assert "<NAME_" in blob and "<NUMBER_" in blob
    assert NOTES in blob and MEMO in blob
    assert body["state"]["kyc"]["expected_countries"] == ["US"]
    assert body["state"]["kyc"]["account_open_date"] == "2024-01-15"
    assert KEY not in blob
    assert "Authorization" not in json.dumps(
        {k: v for k, v in transport.calls[0].items() if k != "headers"}
    )


def test_route_bands():
    assert route_band(0.9) == "auto-escalate band"
    assert route_band(1) == "auto-escalate band"
    close = "likely-close band (still human-reviewed, never auto-close)"
    assert route_band(0.1) == close
    assert route_band(0) == close
    assert route_band(0.11) == "unsure → human"
    assert route_band(0.89) == "unsure → human"


def test_errors_are_no_opinion_and_one_stderr_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert second_opinion(planted(), transport=Transport([ok_body()])) is None
    assert capsys.readouterr().err == ""

    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    assert second_opinion(planted(), transport=Transport([DecisionHTTPError(403)])) is None
    err = capsys.readouterr().err
    assert err.strip() == "aml decision: no opinion (HTTP 403)"
    assert KEY not in err

    assert second_opinion(planted(), transport=Transport([TimeoutError(), TimeoutError()])) is None
    err = capsys.readouterr().err
    assert err.strip() == "aml decision: no opinion (TimeoutError)"
    assert KEY not in err
