"""Proof: the cap of 2 automatic refunds per 90 days holds under a race.

    uv run python proofs/refund_cap.py

Files disputes on t_alex_009 (-$12), 010 (-$20), 011 (-$30) at the same moment: all three are
clear and under $50, so each wants an auto refund. Needs fresh fixtures: no cases for these
transactions and free REFUNDQ#1/#2 slots (run proofs/_cleanup.py after dispute_paths.py).
"""

import time
from concurrent.futures import ThreadPoolExecutor

from _common import Stack, call_tool, check, run
from _dispute import ACCOUNT, TABLE, balance, case_id, case_row, ddb, refund_rows, require_fresh

TXNS = ["t_alex_009", "t_alex_010", "t_alex_011"]
DONE = {"REFUNDED", "ANALYST_REVIEW", "CLOSED_NO_REFUND"}


def main() -> None:
    stack = Stack()
    url = stack.gateway_url
    customer = stack.subject("alex")
    require_fresh(customer, TXNS)
    table = ddb().Table(TABLE)
    for n in (1, 2):
        slot = table.get_item(Key={"customer": customer, "sk": f"REFUNDQ#{n}"}).get("Item")
        if slot:
            raise SystemExit(f"REFUNDQ#{n} is taken: run proofs/_cleanup.py first")
    token = stack.token_with_scope("alex", ("read", "propose", "disputes"))
    before = balance(customer)

    def file(txn: str) -> None:
        call_tool(
            url,
            token,
            "dispute_transaction",
            {
                "account_id": ACCOUNT,
                "transaction_id": txn,
                "reason_code": "not_received",
                "statement": "It never arrived.",
            },
        )

    with ThreadPoolExecutor(3) as pool:
        list(pool.map(file, TXNS))
    end = time.time() + 120
    rows = {}
    while time.time() < end:
        rows = {t: case_row(customer, t) or {} for t in TXNS}
        if all(r.get("status") in DONE for r in rows.values()):
            break
        time.sleep(3)
    status = sorted(r.get("status", "?") for r in rows.values())
    check(
        "3 concurrent auto-refund disputes: exactly 2 REFUNDED and 1 ANALYST_REVIEW",
        status == ["ANALYST_REVIEW", "REFUNDED", "REFUNDED"],
    )
    refunded = [t for t, r in rows.items() if r.get("status") == "REFUNDED"]
    paid = sum(abs(int(rows[t]["amount_cents"])) for t in refunded)
    check("balance up by exactly the two refunded amounts", balance(customer) == before + paid)
    check(
        "exactly 2 refund rows exist",
        sum(len(refund_rows(customer, case_id(t))) for t in TXNS) == 2,
    )
    loser = [t for t, r in rows.items() if r.get("status") == "ANALYST_REVIEW"]
    check(
        "the loser has no refund row and waits for a human",
        all(not refund_rows(customer, case_id(t)) for t in loser),
    )


if __name__ == "__main__":
    run(main)
