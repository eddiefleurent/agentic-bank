"""Case facts: what CODE knows about a dispute. The model reads the case file, never writes it."""

import hashlib
import json
from typing import Any

from agentic_bank.network.order_insight import Order


def applicable_ids(reason_code: str) -> list[str]:
    """Criteria that apply: C1 delivery, C2 device, C3 3-D Secure (code facts), C4 statement."""
    return {"not_received": ["C1"], "not_recognised": ["C2", "C3"]}.get(reason_code, []) + ["C4"]


def code_scores(reason_code: str, order: Order | None) -> dict[str, int]:
    """-1 against the customer, 0 unclear, +1 for. Only C1-C3; C4 is the model's alone."""
    if order is None:
        return {}
    scores: dict[str, int] = {}
    if reason_code == "not_received":
        scores["C1"] = {"not_shipped": 1, "in_transit": 1, "delivered_signed": -1}.get(
            order.delivery, 0
        )
    if reason_code == "not_recognised":
        scores["C2"] = (
            1 if order.device_prior_orders == 0 else -1 if order.device_prior_orders >= 2 else 0
        )
        scores["C3"] = {"not_performed": 1, "authenticated": -1}.get(order.three_ds, 0)
    return scores


def build_case_file(case: dict[str, Any], order: Order | None) -> dict[str, Any]:
    """Facts for the review. `case` holds tokens only (statement_tok), never the narrative."""
    return {
        "reason_code": case["reason_code"],
        "amount_cents": case["amount_cents"],
        "statement": case["statement_tok"]
        + (f"\n{case['answer_tok']}" if case.get("answer_tok") else ""),
        "order": order.to_facts() if order else None,
    }  # not code_scores: the model scores C1-C3 blind, so rule 2 is a real cross-check


def case_file_sha256(case_file: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(case_file, sort_keys=True).encode()).hexdigest()
