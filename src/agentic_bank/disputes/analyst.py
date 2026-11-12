"""The analyst surface: Lambda behind a function URL (AWS_IAM). Body is JSON with an "action":

  queue                                      cases in ANALYST_REVIEW (sparse analyst_queue index)
  reveal  {case_id, token, reason}           decrypt one PII token of a case in review
  decide  {case_id, decision, reason}        decision = approve (refund) | deny (close, no refund)

Every call must come from a session of the analyst role. reveal and decide write an AUDIT row first:
no audit row, no decryption and no decision. Only a person can say no; the workflow never does.
"""

import json
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3

from agentic_bank.disputes.waits import AlreadyResumed, CallbackUnconfirmed, resume
from agentic_bank.shared.ddb import deser, ser
from agentic_bank.shared.pii import Tokenizer

TOKEN = re.compile(r"^<[A-Z_]+_[0-9a-f]{8}>$")
ANALYST_ROLE_NAME = "agentic-bank-app-analyst"
QUEUE_INDEX = "analyst_queue"


def _reply(code: int, body: Any) -> dict[str, Any]:
    return {"statusCode": code, "body": json.dumps(body)}


class Analyst:
    role = re.compile(rf"^arn:aws:sts::\d{{12}}:assumed-role/{ANALYST_ROLE_NAME}/[^/]+$")

    def __init__(self, ddb: Any, sfn: Any, tokenizer: Any, table: str) -> None:
        self.ddb, self.sfn, self.tokenizer, self.table = ddb, sfn, tokenizer, table

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        arn = (
            event.get("requestContext", {}).get("authorizer", {}).get("iam", {}).get("userArn", "")
        )
        if not self.role.fullmatch(arn):
            return _reply(403, {"error": "analyst role required"})
        try:
            body = json.loads(event.get("body") or "{}")
        except ValueError:
            return _reply(400, {"error": "body must be JSON"})
        if not isinstance(body, dict):
            return _reply(400, {"error": "body must be a JSON object"})
        match body.get("action"):
            case "queue":
                return _reply(200, {"cases": [self._public(c) for c in self._queue()]})
            case "reveal":
                return self._reveal(arn, body)
            case "decide":
                return self._decide(arn, body)
        return _reply(400, {"error": "unknown action"})

    def _queue(self) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "TableName": self.table,
            "IndexName": QUEUE_INDEX,
            "KeyConditionExpression": "analyst_queue = :o",
            "ExpressionAttributeValues": {":o": ser("open")},
        }
        items = []
        while True:
            page = self.ddb.query(**kwargs)
            items.extend(deser(item) for item in page["Items"])
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    @staticmethod
    def _public(case: dict[str, Any]) -> dict[str, Any]:  # never the task token
        keep = (
            "case_id",
            "transaction_id",
            "amount_cents",
            "reason_code",
            "statement_tok",
            "answer_tok",
        )
        out = {k: case[k] for k in keep if k in case}
        if "amount_cents" in out:  # a Decimal from DynamoDB: JSON number, not a string
            out["amount_cents"] = int(out["amount_cents"])
        return out

    def _open_case(self, case_id: Any) -> dict[str, Any] | None:
        return next((c for c in self._queue() if c["case_id"] == case_id), None)

    def _audit(self, arn: str, action: str, case_id: str, reason: str, **extra: str) -> None:
        now = datetime.now(UTC).isoformat()
        item = {
            "customer": "AUDIT",
            "sk": f"AUDIT#{now}#{uuid.uuid4().hex}",
            "actor": arn,
            "action": action,
            "case_id": case_id,
            "reason": reason,
            **extra,
        }
        self.ddb.put_item(TableName=self.table, Item={k: ser(v) for k, v in item.items()})

    def _reveal(self, arn: str, body: dict[str, Any]) -> dict[str, Any]:
        case_id, token, reason = body.get("case_id"), body.get("token"), body.get("reason")
        if not (
            isinstance(token, str)
            and TOKEN.fullmatch(token)
            and isinstance(reason, str)
            and reason.strip()
        ):
            return _reply(400, {"error": "case_id, token and reason are required"})
        if self._open_case(case_id) is None:
            return _reply(409, {"error": "case is not in analyst review"})
        self._audit(arn, "reveal", str(case_id), reason, token=token)  # before any decryption
        try:
            return _reply(
                200, {"token": token, "value": self.tokenizer.reveal(token, str(case_id))}
            )
        except KeyError:
            return _reply(403, {"error": "token does not belong to this case"})

    def _decide(self, arn: str, body: dict[str, Any]) -> dict[str, Any]:
        case_id, decision, reason = body.get("case_id"), body.get("decision"), body.get("reason")
        if decision not in ("approve", "deny") or not isinstance(reason, str) or not reason.strip():
            return _reply(400, {"error": "decision (approve|deny) and reason are required"})
        case = self._open_case(case_id)
        if case is None:
            return _reply(409, {"error": "case is not in analyst review"})
        self._audit(arn, "decide", str(case_id), reason, decision=str(decision))
        try:
            resume(
                self.ddb,
                self.sfn,
                self.table,
                case["customer"],
                case["case_id"],
                expected_status="ANALYST_REVIEW",
                sets={"analyst_reason": reason},
                output={"decision": decision, "decided_by": f"staff:{arn}"},
            )
        except CallbackUnconfirmed:
            return _reply(
                202,
                {
                    "case_id": case_id,
                    "message": (
                        "Decision delivery is unconfirmed; "
                        "inspect workflow history before retrying."
                    ),
                },
            )
        except AlreadyResumed:
            return _reply(409, {"error": "case already decided"})
        return _reply(200, {"case_id": case_id, "decision": decision})


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    vault = boto3.resource("dynamodb").Table(os.environ["VAULT_TABLE"])
    tokenizer = Tokenizer(None, boto3.client("kms"), vault, os.environ["PII_KEY_ID"])
    return Analyst(
        boto3.client("dynamodb"),
        boto3.client("stepfunctions"),
        tokenizer,
        os.environ["TABLE_ARN"].rpartition("/")[2],
    ).handle(event)
