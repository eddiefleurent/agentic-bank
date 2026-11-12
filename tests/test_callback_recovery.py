"""Callback interleavings against a small row store that evaluates the real DynamoDB expressions."""

import re
from types import SimpleNamespace

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError, ReadTimeoutError

from agentic_bank.disputes import waits

ser = TypeSerializer().serialize
de = TypeDeserializer().deserialize


class MemoryDdb:
    """Evaluate `a = :v`, attribute_(not_)exists, SET and REMOVE rather than assuming success."""

    def __init__(self, case):
        self.case = dict(case)

    def get_item(self, **_):
        return {"Item": {k: ser(v) for k, v in self.case.items()}}

    def update_item(self, **kw):
        names = kw.get("ExpressionAttributeNames", {})
        values = {k: de(v) for k, v in kw.get("ExpressionAttributeValues", {}).items()}

        def field(name):
            return names.get(name.strip(), name.strip())

        for term in kw.get("ConditionExpression", "").split(" AND "):
            if matched := re.fullmatch(r"attribute_(not_)?exists\(([^)]+)\)", term):
                ok = (field(matched[2]) in self.case) != bool(matched[1])
            else:
                name, value = term.split(" = ")
                ok = self.case.get(field(name)) == values[value]
            if not ok:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "Update")
        old = dict(self.case)
        parts = re.split(r"\b(SET|REMOVE)\b", kw["UpdateExpression"])[1:]
        for action, body in zip(parts[::2], parts[1::2], strict=True):
            for item in body.split(","):
                if action == "SET":
                    name, value = item.strip().split(" = ")
                    self.case[field(name)] = values[value]
                else:
                    self.case.pop(field(item), None)
        return {"Attributes": {k: ser(v) for k, v in old.items()}}


def question_case():
    return {"status": "INFO_REQUESTED", "task_token": "question-token", "question_id": "q1"}


def answer(ddb, send):
    waits.resume(
        ddb,
        SimpleNamespace(send_task_success=send),
        "t",
        "alex",
        "dc1",
        expected_status="INFO_REQUESTED",
        sets={"status": "REVIEWING", "answer_tok": "answer", "round": 1},
        output={"answered": True},
    )


def test_duplicate_send_is_refused_before_reaching_step_functions():
    ddb, sent = MemoryDdb(question_case()), []
    answer(ddb, lambda **kw: sent.append(kw["taskToken"]))
    with pytest.raises(waits.AlreadyResumed):
        answer(ddb, lambda **kw: sent.append(kw["taskToken"]))
    assert sent == ["question-token"]


def test_lost_response_after_acceptance_is_unconfirmed_and_never_resent():
    ddb = MemoryDdb(question_case())

    def send(**_):
        ddb.case["status"] = "REFUNDED"  # the workflow advanced; then the HTTP response is lost
        raise ReadTimeoutError(endpoint_url="https://example.invalid")

    with pytest.raises(waits.CallbackUnconfirmed):
        answer(ddb, send)
    assert ddb.case["status"] == "REFUNDED"
    assert "task_token" not in ddb.case
    with pytest.raises(waits.AlreadyResumed):
        answer(ddb, lambda **_: pytest.fail("must not resend an unconfirmed callback"))
