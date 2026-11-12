"""Proof: PII is tokenized everywhere, the analyst can reveal it (audited), nobody else can.

    uv run python proofs/analyst_pii.py

Run right after `dispute_paths.py --keep` (cases t_alex_006/007/008 exist; 007 and 008 wait in
ANALYST_REVIEW). Needs admin AWS credentials that may assume agentic-bank-app-analyst, and about a
minute of waiting for log delivery. Ends by deciding both cases as the analyst, then resets
the fixtures.
"""

import json
import time

import boto3
from _common import REGION, Stack, check, run
from _dispute import (
    MACHINE,
    RAW_VALUES,
    TABLE,
    TOKEN_RE,
    VAULT,
    analyst_call,
    analyst_session,
    case_id,
    case_row,
    ddb,
    refund_rows,
    self_cleaning,
    wait_status,
)
from botocore.exceptions import ClientError


def history_text(txn: str, customer: str) -> str:
    sfn = boto3.client("stepfunctions", region_name=REGION)
    machine = next(
        m["stateMachineArn"]
        for m in sfn.list_state_machines()["stateMachines"]
        if m["name"] == MACHINE
    )
    execution = (case_row(customer, txn) or {})["execution"]  # case_id plus a per-run suffix
    arn = machine.replace(":stateMachine:", ":execution:") + f":{execution}"
    events: list = []
    for page in sfn.get_paginator("get_execution_history").paginate(executionArn=arn):
        events += page["events"]
    return json.dumps(events, default=str)


def log_hits(raw: str) -> list[str]:
    """Log groups (any name containing agentic-bank) holding the raw string in the last 2 hours."""
    logs = boto3.client("logs", region_name=REGION)
    start = int((time.time() - 2 * 3600) * 1000)
    hits = []
    for page in logs.get_paginator("describe_log_groups").paginate(
        logGroupNamePattern="agentic-bank"
    ):
        for group in page["logGroups"]:
            pages = logs.get_paginator("filter_log_events").paginate(
                logGroupName=group["logGroupName"], filterPattern=f'"{raw}"', startTime=start
            )
            # An empty page can still carry nextToken: follow pages until one has an event.
            if any(p["events"] for p in pages):
                hits.append(group["logGroupName"])
    return hits


def main() -> None:
    stack = Stack()
    customer = stack.subject("alex")
    txns = ["t_alex_006", "t_alex_007", "t_alex_008"]
    cases = {t: case_row(customer, t) or {} for t in txns}
    check("fixture cases exist (run dispute_paths.py first)", all(cases.values()))

    # Tokenization everywhere.
    stored = json.dumps(
        {t: [c.get("statement_tok"), c.get("answer_tok")] for t, c in cases.items()}
    )
    check(
        "case rows hold tokens, none of the raw values",
        bool(TOKEN_RE.search(stored)) and not any(v in stored for v in RAW_VALUES),
    )
    history = {t: history_text(t, customer) for t in txns}
    check(
        "Step Functions history (includes the model's input) holds tokens, no raw value",
        all(TOKEN_RE.search(h) for h in history.values())
        and not any(v in h for h in history.values() for v in RAW_VALUES),
    )
    check(
        "the review input (case file) carries the statement as tokens",
        "<PHONE_" in history["t_alex_006"] or "<NUMBER_" in history["t_alex_006"],
    )
    time.sleep(45)  # CloudWatch ingestion delay
    leaks = {v: log_hits(v) for v in RAW_VALUES}
    check(
        "CloudWatch filter for each raw phone, address, card, email returns nothing in any "
        "agentic-bank log group",
        not any(leaks.values()),
    )

    # Analyst reveal.
    analyst = analyst_session()
    _, queue = analyst_call(analyst, {"action": "queue"})
    check(
        "analyst queue lists t_alex_007 and t_alex_008",
        all(case_id(t) in json.dumps(queue) for t in ("t_alex_007", "t_alex_008")),
    )
    cid8, cid7 = case_id("t_alex_008"), case_id("t_alex_007")
    tokens8 = TOKEN_RE.findall(cases["t_alex_008"]["statement_tok"])
    tokens7 = TOKEN_RE.findall(
        cases["t_alex_007"]["statement_tok"] + cases["t_alex_007"].get("answer_tok", "")
    )
    revealed = []
    for tok in tokens8:
        status, out = analyst_call(
            analyst, {"action": "reveal", "case_id": cid8, "token": tok, "reason": "proof"}
        )
        revealed.append((status, json.dumps(out)))
    check(
        "analyst reveal returns the raw email and phone of t_alex_008",
        all(s == 200 for s, _ in revealed)
        and any("canary.alex@example.com" in o for _, o in revealed)
        and any("512-555-0188" in o for _, o in revealed),
    )
    audit = [
        i
        for i in ddb()
        .Table(TABLE)
        .scan(
            FilterExpression="begins_with(sk, :p) AND case_id = :c",
            ExpressionAttributeValues={":p": "AUDIT#", ":c": cid8},
        )["Items"]
    ]
    check(
        "every reveal wrote an AUDIT row (actor is an analyst session, token and reason kept)",
        len(audit) >= len(tokens8)
        and all("analyst" in str(a.get("actor")) for a in audit)
        and {a.get("token") for a in audit} >= set(tokens8),
    )
    if tokens7:
        status, _ = analyst_call(
            analyst,
            {"action": "reveal", "case_id": cid8, "token": tokens7[0], "reason": "wrong case"},
        )
        check("a token from another case is refused (403)", status == 403)
    status, _ = analyst_call(
        analyst_session(False),
        {"action": "reveal", "case_id": cid8, "token": tokens8[0], "reason": "admin"},
    )
    check("the same call as admin (not the analyst role) is refused (403)", status == 403)

    # KMS: a non-analyst principal cannot decrypt, even with the right context and ciphertext.
    row = ddb().Table(VAULT).get_item(Key={"token": tokens8[0]})["Item"]
    try:
        boto3.client("kms", region_name=REGION).decrypt(
            CiphertextBlob=bytes(row["ciphertext"]),
            EncryptionContext={"case_id": cid8, "token": tokens8[0]},
        )
        denied = False
    except ClientError as error:
        denied = error.response["Error"]["Code"] in ("AccessDeniedException", "AccessDenied")
    check("admin kms:Decrypt on a vault ciphertext: AccessDenied (key policy deny)", denied)

    # Only a human decides.
    analyst_call(
        analyst, {"action": "decide", "case_id": cid7, "decision": "approve", "reason": "proof"}
    )
    check(
        "analyst approves t_alex_007: REFUNDED once",
        wait_status(customer, "t_alex_007", {"REFUNDED"}, 60) == "REFUNDED"
        and len(refund_rows(customer, cid7)) == 1,
    )
    status, _ = analyst_call(
        analyst, {"action": "decide", "case_id": cid7, "decision": "approve", "reason": "again"}
    )
    check(
        "a second decision on the same case is refused (409), still one refund",
        status == 409 and len(refund_rows(customer, cid7)) == 1,
    )
    analyst_call(
        analyst,
        {"action": "decide", "case_id": cid8, "decision": "deny", "reason": "delivered and signed"},
    )
    wait_status(customer, "t_alex_008", {"CLOSED_NO_REFUND"}, 60)
    row = case_row(customer, "t_alex_008") or {}
    check(
        "analyst denies t_alex_008: CLOSED_NO_REFUND, decided_by names a staff session, no refund",
        row.get("status") == "CLOSED_NO_REFUND"
        and str(row.get("decided_by", "")).startswith("staff:")
        and not refund_rows(customer, cid8)
        and "analyst_queue" not in row,
    )


if __name__ == "__main__":
    with self_cleaning(start=False):
        run(main)
