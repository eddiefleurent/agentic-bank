"""AML slice 3 without AWS: the DynamoDB case store, the check step and the queue row.

What only AWS can prove (IAM denies another case, the workflow runs) is in proofs/aml_triage.py.
"""

import json
from functools import cache
from typing import Any

import pytest

from agentic_bank.aml import handler, snapshot, triage
from agentic_bank.aml.case_tools import DynamoCaseSource
from evals.aml import harness
from test_iam_boundaries import tf_list


@cache
def world():
    return harness.load_cases()


def case(label: str) -> tuple[str, dict[str, Any]]:
    cases, ds, alerts = world()
    c = next(c for c in cases if c.id == label)
    return c.alert.alert_id, snapshot.build_case_snapshot(c.alert, ds, alerts)


class FakeDdb:
    """put_item and query on (customer, sk), like the data table. No pagination."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, TableName: str, Item: dict[str, Any]) -> None:  # noqa: N803
        self.rows[(Item["customer"]["S"], Item["sk"]["S"])] = Item

    def query(self, **kw: Any) -> dict[str, Any]:
        pk = kw["ExpressionAttributeValues"][":c"]["S"]
        return {"Items": [v for (c, _), v in sorted(self.rows.items()) if c == pk]}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TABLE_ARN", "arn:aws:dynamodb:us-east-1:ACCOUNT:table/agentic-bank-data")
    monkeypatch.setenv("AML_STATE_MACHINE_ARN", "arn:aws:states:::stateMachine:agentic-bank-aml")


def stored(snap: dict[str, Any]) -> FakeDdb:
    ddb = FakeDdb()
    for item in snapshot.to_ddb(snap):
        ddb.put_item(TableName="t", Item=item)
    return ddb


def test_tool_spec_file_matches_the_pydantic_model():
    """The Lambda reads a frozen copy (no Strands there); it must not drift from `Triage`."""
    from strands.tools.structured_output.structured_output_utils import (
        convert_pydantic_to_tool_spec,
    )

    live = dict(convert_pydantic_to_tool_spec(triage.Triage)) | {"name": triage.TOOL_NAME}
    assert triage.tool_spec() == json.loads(json.dumps(live))


def test_dynamodb_round_trip_gives_the_reviewer_the_evals_evidence_pack():
    _, ds, alerts = world()
    for a in alerts:
        snap = snapshot.build_case_snapshot(a, ds, alerts)
        back = DynamoCaseSource(stored(snap), "t", a.alert_id).snapshot
        assert triage.case_text(back) == triage.case_text(snap)  # byte for byte, row order too


@pytest.mark.parametrize("bad", ["A-0001*", "A-*", "CASE#A-0001", "A-00011", "", None, "a-0001"])
def test_alert_id_that_could_widen_the_session_policy_is_refused(bad):
    with pytest.raises(ValueError):
        snapshot.case_key(bad)


def test_open_writes_only_its_case_partition_and_allowed_attributes(env):
    alert_id, _ = case("LABEL-001")

    class Sfn:
        def start_execution(self, **kw: Any) -> dict[str, str]:
            self.input = json.loads(kw["input"])
            return {"executionArn": "arn:x"}

    ddb, sfn = FakeDdb(), Sfn()
    handler.open_case(alert_id, ddb, sfn)
    assert {c for c, _ in ddb.rows} == {f"CASE#{alert_id}"}
    names = {k for row in ddb.rows.values() for k in row}
    assert names <= tf_list("aml.tf", "aml_attributes")
    assert sfn.input == {"alert_id": alert_id}  # the workflow carries an id, never case data


def test_check_step_uses_its_own_read_of_the_case(env):
    """A reviewer citing another case's transaction (or reporting a fake check) is escalated."""
    alert_id, snap = case("LABEL-004")
    _, other = case("LABEL-001")
    ddb = stored(snap)
    foreign = other["flagged_transactions"][0]["txn_id"]
    assert foreign not in {t["txn_id"] for t in snap["flagged_transactions"] + snap["transactions"]}
    answer = {
        "recommendation": "close",
        "typology": "none",
        "risk_factors": [],
        "mitigating_factors": [{"statement": "Fits the profile.", "txn_ids": [foreign]}],
        "narrative": "Looks fine.",
    }
    out = handler.check_review(alert_id, {"answer": answer}, ddb)
    assert out["recommendation"] == "escalate"
    assert out["failure"].startswith("cited ids not in case")

    own = snap["flagged_transactions"][0]["txn_id"]
    answer["mitigating_factors"][0]["txn_ids"] = [own]
    assert handler.check_review(alert_id, {"answer": answer}, ddb)["recommendation"] == "close"

    failed = handler.check_review(alert_id, {"answer": None, "failure": "baseline error: X"}, ddb)
    assert failed == {"recommendation": "escalate", "failure": "baseline error: X", "advice": None}


def test_record_queues_for_a_person_and_never_closes(env):
    ddb = FakeDdb()
    handler.record("A-0004", {"recommendation": "close", "failure": None, "advice": {}}, "e", ddb)
    row = ddb.rows[("CASE#A-0004", "RECOMMENDATION")]
    assert row["analyst_queue"] == {"S": "aml"} and row["status"] == {"S": "awaiting_analyst"}
    assert set(row) <= tf_list("aml.tf", "aml_attributes")

    handler.record("A-0004", {"recommendation": "closed"}, "e", ddb)  # garbage from upstream
    assert ddb.rows[("CASE#A-0004", "RECOMMENDATION")]["recommendation"] == {"S": "escalate"}
