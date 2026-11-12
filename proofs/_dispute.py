"""Helpers for the dispute proofs: admin reads of case rows, polling, the analyst function URL.

Assumed names: table `agentic-bank-data` (PK `customer`, SK `sk`), rows `CASE#<case_id>`,
`REFUNDQ#1|2`, `AUDIT#...`; vault table `agentic-bank-vault`; state machine `agentic-bank-dispute`;
Lambda `agentic-bank-analyst` (function URL, AWS_IAM); role `agentic-bank-app-analyst`.
Analyst actions: {"action": "queue"} | {"action": "reveal", case_id, token, reason} |
{"action": "decide", case_id, decision: approve|deny, reason}.
"""

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import boto3
from _common import REGION
from boto3.dynamodb.conditions import Key
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

TABLE = "agentic-bank-data"
VAULT = "agentic-bank-vault"
MACHINE = "agentic-bank-dispute"
ANALYST_FN = "agentic-bank-analyst"
ANALYST_ROLE = "agentic-bank-app-analyst"
TOKEN_RE = re.compile(r"<[A-Z_]+_[0-9a-f]{8}>")
ACCOUNT = "acc_alex_chk"

# Raw values the customer types. They must never appear anywhere after tokenization.
CANARIES = {
    "t_alex_006": (
        "I never got it. Ship to 1600 Example St, Austin TX 78701, call me at 512-555-0142."
    ),
    "t_alex_007": (
        "Yes, nothing. Call 512-555-0199 and use card 4111 1111 1111 1111 for the refund."
    ),
    "t_alex_008": "The lamp never came. Email me at canary.alex@example.com or call 512-555-0188.",
}
RAW_VALUES = [
    "512-555-0142",
    "1600 Example St",
    "512-555-0199",
    "4111 1111 1111 1111",
    "canary.alex@example.com",
    "512-555-0188",
]


def ddb() -> Any:
    return boto3.resource("dynamodb", region_name=REGION)


def case_id(txn: str) -> str:
    return f"dsp_{txn}"


def case_row(customer: str, txn: str) -> dict[str, Any] | None:
    key = {"customer": customer, "sk": f"CASE#{case_id(txn)}"}
    return ddb().Table(TABLE).get_item(Key=key, ConsistentRead=True).get("Item")


def require_fresh(customer: str, txns: list[str]) -> None:
    """One case per transaction ever: a second run needs proofs/_cleanup.py first."""
    used = [t for t in txns if case_row(customer, t)]
    if used:
        raise SystemExit(f"cases already exist for {used}: run proofs/_cleanup.py first")


@contextmanager
def self_cleaning(start: bool = True, end: bool = True) -> Iterator[None]:
    """Reset the dispute fixtures before the proof and again after it, even if it fails."""
    from _cleanup import main as cleanup  # _cleanup imports this module: import late

    if start:
        cleanup()
    try:
        yield
    finally:
        if end:
            cleanup()


def balance(customer: str, account: str = ACCOUNT) -> int:
    item = ddb().Table(TABLE).get_item(Key={"customer": customer, "sk": f"ACCT#{account}"})["Item"]
    return int(item["balance_cents"])


def refund_rows(customer: str, cid: str) -> list[dict[str, Any]]:
    rows = (
        ddb()
        .Table(TABLE)
        .query(
            KeyConditionExpression=Key("customer").eq(customer) & Key("sk").begins_with("TXN#"),
            ConsistentRead=True,
        )["Items"]
    )
    return [r for r in rows if r["sk"].endswith(f"#r_{cid}")]


def wait_status(customer: str, txn: str, wanted: set[str], timeout: float = 90) -> str | None:
    """Poll the case row until its status is in `wanted`; None on timeout."""
    end = time.time() + timeout
    status = None
    while time.time() < end:
        row = case_row(customer, txn)
        status = row["status"] if row else None
        if status in wanted:
            return status
        time.sleep(3)
    return None


def analyst_url() -> str:
    lam = boto3.client("lambda", region_name=REGION)
    return lam.get_function_url_config(FunctionName=ANALYST_FN)["FunctionUrl"]


def analyst_session(as_role: bool = True) -> boto3.Session:
    """Credentials of the human analyst role (or, with as_role=False, the caller's own)."""
    if not as_role:
        return boto3.Session(region_name=REGION)
    sts = boto3.client("sts", region_name=REGION)
    account = sts.get_caller_identity()["Account"]
    creds = sts.assume_role(
        RoleArn=f"arn:aws:iam::{account}:role/{ANALYST_ROLE}", RoleSessionName="proof-analyst"
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=REGION,
    )


def analyst_call(session: boto3.Session, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """SigV4-signed POST to the analyst function URL. Returns (HTTP status, JSON body)."""
    url = analyst_url()
    body = json.dumps(payload)
    request = AWSRequest(
        method="POST", url=url, data=body, headers={"Content-Type": "application/json"}
    )
    SigV4Auth(session.get_credentials(), "lambda", REGION).add_auth(request)
    http = urllib.request.Request(
        url, data=body.encode(), headers=dict(request.headers.items()), method="POST"
    )
    try:
        with urllib.request.urlopen(http) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"raw": raw.decode(errors="replace")[:200]}
