import json
from types import SimpleNamespace

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from agentic_bank.disputes.analyst import Analyst
from test_callback_recovery import MemoryDdb

ser = TypeSerializer().serialize
ANALYST = f"arn:aws:sts::{'1' * 12}:assumed-role/agentic-bank-app-analyst/ana"
ADMIN = f"arn:aws:sts::{'1' * 12}:assumed-role/Admin/test-session"
TOKEN = "<NAME_0a1b2c3d>"


def row(**kw):
    return {k: ser(v) for k, v in kw.items()}


class Ddb(MemoryDdb):
    """Records writes in a shared log. The queue holds case dc1 in review, with a task token."""

    def __init__(self, log, status="ANALYST_REVIEW", queued=True):
        super().__init__({})
        self.log, self.queued = log, queued
        self.case = {"customer": "alex", "case_id": "dc1", "status": status, "task_token": "TT-1"}
        self.case["analyst_queue"] = "open"

    def query(self, **_):
        return {"Items": [row(**self.case)] if self.queued else []}

    def put_item(self, **kw):
        self.log.append("audit")


class Sfn:
    def __init__(self, log, fail=False):
        self.log, self.fail, self.tokens = log, fail, []

    def send_task_success(self, taskToken, output):  # noqa: N803
        if self.fail:
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "SendTaskSuccess")
        self.log.append("send")
        self.tokens.append(taskToken)


def analyst(log, **kw):
    reveal = SimpleNamespace(reveal=lambda token, case_id: log.append("decrypt") or "Alex Doe")
    sfn = Sfn(log, kw.pop("fail", False))
    return Analyst(Ddb(log, **kw), sfn, reveal, "t"), sfn


def call(a, arn, **body):
    event = {"requestContext": {"authorizer": {"iam": {"userArn": arn}}}, "body": json.dumps(body)}
    return a.handle(event)["statusCode"]


REVEAL = {"action": "reveal", "case_id": "dc1", "token": TOKEN, "reason": "verify identity"}


def test_reveal_audits_before_decrypting():
    log = []
    a, _ = analyst(log)
    assert call(a, ANALYST, **REVEAL) == 200
    assert log == ["audit", "decrypt"]


def test_reveal_refuses_wrong_role_and_wrong_state_without_decrypting():
    log = []
    a, _ = analyst(log)
    assert call(a, ADMIN, **REVEAL) == 403
    b, _ = analyst(log, queued=False)  # case no longer in ANALYST_REVIEW
    assert call(b, ANALYST, **REVEAL) == 409
    assert log == []


def test_decide_resumes_with_stored_token_exactly_once():
    log = []
    a, sfn = analyst(log)
    body = {"action": "decide", "case_id": "dc1", "decision": "approve", "reason": "ok"}
    assert call(a, ANALYST, **body) == 200
    a.ddb.queued = True  # even if a stale queue view still shows it
    assert call(a, ANALYST, **body) == 409
    assert sfn.tokens == ["TT-1"]
    assert log == ["audit", "send", "audit"]


@pytest.mark.parametrize("body", [[], None, "queue", 1])
def test_non_object_json_is_a_bad_request(body):
    a, _ = analyst([])
    event = {
        "requestContext": {"authorizer": {"iam": {"userArn": ANALYST}}},
        "body": json.dumps(body),
    }
    assert a.handle(event)["statusCode"] == 400


def test_queue_follows_pagination_and_finds_later_case():
    a, _ = analyst([])
    cursor = row(customer="sam", sk="CASE#earlier")
    calls = []

    def query(**kwargs):
        calls.append(kwargs)
        if "ExclusiveStartKey" not in kwargs:
            return {"Items": [], "LastEvaluatedKey": cursor}
        assert kwargs["ExclusiveStartKey"] == cursor
        return {"Items": [row(customer="alex", case_id="later", status="ANALYST_REVIEW")]}

    a.ddb.query = query
    assert a._open_case("later")["customer"] == "alex"
    assert len(calls) == 2


def test_failed_send_is_unconfirmed_and_never_resent():
    a, _ = analyst([], fail=True)
    body = {"action": "decide", "case_id": "dc1", "decision": "approve", "reason": "ok"}
    assert call(a, ANALYST, **body) == 202
    assert "task_token" not in a.ddb.case  # consumed: a retry cannot send it twice
    assert call(a, ANALYST, **body) == 409
