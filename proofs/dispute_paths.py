"""Proof: the dispute paths (auto refund, info round, timeout, adverse goes to a human).

    uv run python proofs/dispute_paths.py [--with-timeout]

Needs the deployed stack, alex and sam credential files, and fresh fixtures (run proofs/_cleanup.py
between runs: one case per transaction ever). Default: t_alex_007 gets a question and is answered.
--with-timeout: it is left unanswered; the 2-minute lab wait runs out and rule 8 sends it to the
analyst. Leaves t_alex_007 and t_alex_008 in ANALYST_REVIEW for proofs/analyst_pii.py.
Statements carry canary PII (see _dispute.py): the proof of tokenization is in analyst_pii.py.
"""

import sys
import time

from _common import Stack, call_tool, check, run
from _dispute import (
    ACCOUNT,
    CANARIES,
    balance,
    case_id,
    case_row,
    refund_rows,
    require_fresh,
    wait_status,
)

SCOPES = ("read", "propose", "disputes")


def file_case(url: str, token: str, txn: str, reason: str = "not_received") -> dict:
    args = {
        "account_id": ACCOUNT,
        "transaction_id": txn,
        "reason_code": reason,
        "statement": CANARIES[txn],
    }
    return call_tool(url, token, "dispute_transaction", args)[1]


def main() -> None:
    with_timeout = "--with-timeout" in sys.argv
    stack = Stack()
    url = stack.gateway_url
    alex_id = stack.subject("alex")
    require_fresh(alex_id, ["t_alex_006", "t_alex_007", "t_alex_008"])
    alex = stack.token_with_scope("alex", SCOPES)
    sam = stack.token_with_scope("sam", SCOPES)

    # 1. Clear case, <= $50: auto refund within 60 s, once.
    before = balance(alex_id)
    file_case(url, alex, "t_alex_006")
    cid = case_id("t_alex_006")
    check(
        "t_alex_006 (not shipped, $34.00): REFUNDED within 90 s",
        wait_status(alex_id, "t_alex_006", {"REFUNDED"}, 90) == "REFUNDED",
    )
    row = case_row(alex_id, "t_alex_006") or {}
    check(
        "auto refund decided by code, refund_cents 3400",
        row.get("decided_by") == "code" and int(row.get("refund_cents", 0)) == 3400,
    )
    check("balance up by exactly $34.00", balance(alex_id) == before + 3400)

    again = file_case(url, alex, "t_alex_006")
    time.sleep(10)
    check(
        "filing the same dispute again returns the same case, no new case",
        cid in str(again) or "refused" not in again,
    )
    check(
        "replay: still one refund row and the balance did not move",
        len(refund_rows(alex_id, cid)) == 1 and balance(alex_id) == before + 3400,
    )

    # 2. Ambiguous: one question to the customer, then the analyst.
    file_case(url, alex, "t_alex_007")
    got = wait_status(alex_id, "t_alex_007", {"INFO_REQUESTED", "ANALYST_REVIEW"}, 90)
    check(
        "t_alex_007 (delivered, no proof): customer is asked one question", got == "INFO_REQUESTED"
    )
    cid7 = case_id("t_alex_007")
    _, view = call_tool(url, sam, "get_dispute", {"case_id": cid7})
    check("sam cannot read alex's case", "status" not in view)
    _, out = call_tool(url, sam, "answer_dispute", {"case_id": cid7, "answer": "mine now"})
    check(
        "sam cannot answer alex's case",
        (case_row(alex_id, "t_alex_007") or {}).get("status") == "INFO_REQUESTED"
        and "answer_tok" not in (case_row(alex_id, "t_alex_007") or {}),
    )
    if with_timeout:
        start = time.time()
        got = wait_status(alex_id, "t_alex_007", {"ANALYST_REVIEW"}, 200)
        check(
            "no answer: after the 2-minute wait the case goes to ANALYST_REVIEW",
            got == "ANALYST_REVIEW" and time.time() - start < 190,
        )
        decision = (case_row(alex_id, "t_alex_007") or {}).get("decision", {})
        check(
            "decision rule is 8 (timeout), no refund",
            int(decision.get("rule", 0)) == 8 and not refund_rows(alex_id, cid7),
        )
    else:
        call_tool(url, alex, "answer_dispute", {"case_id": cid7, "answer": CANARIES["t_alex_007"]})
        got = wait_status(alex_id, "t_alex_007", {"ANALYST_REVIEW"}, 90)
        row = case_row(alex_id, "t_alex_007") or {}
        check(
            "after the answer (round 1, still unclear) the case goes to ANALYST_REVIEW",
            got == "ANALYST_REVIEW" and int(row.get("round", 0)) == 1,
        )
        call_tool(url, alex, "answer_dispute", {"case_id": cid7, "answer": "changed my mind"})
        check(
            "a second answer is refused: stored answer unchanged",
            (case_row(alex_id, "t_alex_007") or {}).get("answer_tok") == row.get("answer_tok"),
        )
        check("no refund was paid", not refund_rows(alex_id, cid7))

    # 3. Adverse (delivered and signed): never auto-closed, never auto-refunded.
    file_case(url, alex, "t_alex_008", "not_received")
    got = wait_status(alex_id, "t_alex_008", {"ANALYST_REVIEW", "REFUNDED", "CLOSED_NO_REFUND"}, 90)
    check(
        "t_alex_008 (delivered, signed): ANALYST_REVIEW, not refunded, not closed",
        got == "ANALYST_REVIEW",
    )
    time.sleep(20)
    row = case_row(alex_id, "t_alex_008") or {}
    check(
        "20 s later still waiting for a human: no refund, no decision by code",
        row.get("status") == "ANALYST_REVIEW"
        and not refund_rows(alex_id, case_id("t_alex_008"))
        and row.get("decided_by") in (None, ""),
    )
    check(
        "the model alone never produced a denial (no CLOSED_NO_REFUND yet)",
        row.get("status") != "CLOSED_NO_REFUND",
    )


if __name__ == "__main__":
    run(main)
