import json

import pytest
from botocore.exceptions import ClientError

from agentic_bank.disputes import review as review_module
from agentic_bank.disputes.review import parse_review, run_review

IDS = ["C1", "C4"]


def good(fields=("order.delivery",)):
    crit = [
        {"id": i, "score": 1, "rationale": "The order shows no shipment.", "fields": list(fields)}
        for i in IDS
    ]
    return json.dumps({"criteria": crit, "flags": [], "summary": "Not shipped."})


class Bedrock:
    def __init__(self, replies, grounding=0.9):
        self.replies, self.grounding, self.calls = list(replies), grounding, 0

    def converse(self, **_):
        self.calls += 1
        text = self.replies.pop(0)
        return {"output": {"message": {"content": [{"text": text}]}}}

    def apply_guardrail(self, **_):
        f = [{"type": "GROUNDING", "score": self.grounding}, {"type": "RELEVANCE", "score": 0.9}]
        return {"assessments": [{"contextualGroundingPolicy": {"filters": f}}]}


CASE_FILE = {"reason_code": "not_received", "order": {"delivery": "not_shipped"}}


def test_parse_repairs_a_doubled_opening_brace_once():
    assert parse_review("{\n" + good(), IDS) is not None
    assert parse_review("{{{" + good()[1:], IDS) is None


def test_parse_refuses_extra_fields_and_wrong_criteria():
    data = json.loads(good())
    data["amount"] = 5000  # the model must not name an amount
    assert parse_review(json.dumps(data), IDS) is None
    assert parse_review(good(), ["C1", "C2", "C4"]) is None


def test_parse_drops_criteria_that_do_not_apply():
    data = json.loads(good())
    data["criteria"].append({"id": "C2", "score": -1, "rationale": "Two orders.", "fields": []})
    review = parse_review(json.dumps(data), IDS)
    assert review is not None and [c["id"] for c in review["criteria"]] == IDS


def test_invalid_output_after_one_retry_is_a_failure_marker():
    out = run_review(Bedrock(["nope", "still nope"]), "m", "g", "1", CASE_FILE)
    assert out.review is None and out.failure


def test_cited_field_must_exist_in_case_file():
    out = run_review(Bedrock([good(("order.signature",))]), "m", "g", "1", CASE_FILE)
    assert out.failure and "order.signature" in out.failure


def test_grounding_below_threshold_fails_and_above_passes():
    assert run_review(Bedrock([good()], grounding=0.3), "m", "g", "1", CASE_FILE).failure
    assert run_review(Bedrock([good()], grounding=0.9), "m", "g", "1", CASE_FILE).failure is None


def test_review_refuses_non_integer_scores():
    for score in (True, 1.0, "1", None):
        data = json.loads(good())
        data["criteria"][0]["score"] = score
        assert parse_review(json.dumps(data), IDS) is None


def test_parse_accepts_tool_input_and_refuses_unknown_or_repeated_flags():
    data = json.loads(good())
    assert parse_review(dict(data, flags=["concedes_charge"]), IDS) is not None
    assert parse_review(dict(data, flags=["approve_refund"]), IDS) is None
    assert parse_review(dict(data, flags=["concedes_charge", "concedes_charge"]), IDS) is None
    del data["flags"]
    assert parse_review(data, IDS) is None  # flags are required, even when empty


def test_forced_tool_answer_is_used_and_a_rejected_tool_choice_falls_back_to_any():
    answer = json.loads(good())
    choices = []

    class Picky(Bedrock):
        def converse(self, **kwargs):
            choice = kwargs["toolConfig"]["toolChoice"]
            choices.append(next(iter(choice)))
            if "tool" in choice:
                error = {"Error": {"Code": "ValidationException", "Message": "toolChoice tool"}}
                raise ClientError(error, "Converse")
            tool_use = {"toolUse": {"toolUseId": "t1", "name": "submit_review", "input": answer}}
            return {"output": {"message": {"content": [tool_use]}}}

    review_module._choice_for.pop("picky", None)
    assert run_review(Picky([]), "picky", "g", "1", CASE_FILE).failure is None
    assert run_review(Picky([]), "picky", "g", "1", CASE_FILE).failure is None
    assert choices == ["tool", "any", "any"]  # the accepted choice is remembered per model


def test_other_bedrock_errors_are_not_swallowed():
    class Down(Bedrock):
        def converse(self, **_):
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "Converse")

    with pytest.raises(ClientError):
        run_review(Down([]), "m", "g", "1", CASE_FILE)
