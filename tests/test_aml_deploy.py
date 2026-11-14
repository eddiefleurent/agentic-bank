"""AML slice 3 without AWS: the DynamoDB case store, the check step and the queue row.

What only AWS can prove (IAM denies another case, the workflow runs) is in proofs/aml_triage.py.
"""

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from agentic_bank.aml import handler, snapshot, triage
from agentic_bank.aml.case_tools import DynamoCaseSource
from agentic_bank.shared.data import READ
from evals.aml import harness
from test_iam_boundaries import tf_list

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "infra/app"
BOOT = ROOT / "infra/bootstrap/main.tf"


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


def _hcl_block(text: str, marker: str) -> str:
    """From a Terraform block header to the next top-level block."""
    start = text.index(marker)
    rest = text[start + len(marker) :]
    nxt = re.search(r"\n(?:resource|data|locals|variable|output) ", rest)
    return text[start:] if nxt is None else text[start : start + len(marker) + nxt.start()]


def test_case_reader_identity_policy_reads_only_case_partitions():
    """GetItem and Query on the data table, LeadingKeys like CASE#A-*, no Scan and no writes."""
    aml = (APP / "aml.tf").read_text()
    role = _hcl_block(aml, 'resource "aws_iam_role" "case_reader"')
    policy = _hcl_block(aml, 'resource "aws_iam_role_policy" "case_reader"')
    trust = _hcl_block(aml, 'data "aws_iam_policy_document" "case_reader_trust"')
    assert re.search(r'name\s+=\s+"agentic-bank-app-case-reader"', role)
    assert "permissions_boundary = local.boundary_arn" in role
    assert 'aws_iam_role.lambda["aml-review"].arn' in trust
    assert "dynamodb:GetItem" in policy and "dynamodb:Query" in policy
    assert '["CASE#A-*"]' in policy
    assert "dynamodb:LeadingKeys" in policy
    assert '"dynamodb:LeadingKeys" = "false"' in policy  # a request with no key does not match
    for forbidden in ("Scan", "PutItem", "UpdateItem", "DeleteItem", "Batch"):
        assert forbidden not in policy


def test_reviewer_assumes_case_reader_and_data_reader_does_not_trust_it():
    aml = (APP / "aml.tf").read_text()
    grants = _hcl_block(aml, "aml_review_grants = [")
    assert "case_reader_role_arn" in grants
    assert "data_reader_role_arn" not in grants
    review = _hcl_block((APP / "gateway.tf").read_text(), "aml-review = {")
    assert "CASE_READER_ROLE_ARN" in review
    assert "DATA_READER_ROLE_ARN" not in review
    assert "case_reader_role_arn" in review
    data_tf = (APP / "data.tf").read_text()
    trust = _hcl_block(data_tf, 'data "aws_iam_policy_document" "data_reader_trust"')
    assert "aml-review" not in trust


def test_workload_boundary_allows_assuming_the_case_reader():
    text = BOOT.read_text()
    start = re.search(r'sid\s+=\s+"AssumeDataReader"', text)
    end = re.search(r'sid\s+=\s+"WriteBankData"', text)
    assert start is not None and end is not None
    block = text[start.start() : end.start()]
    names = re.search(r"\[for r in \[(.*?)\]", block, re.S)
    assert names is not None
    listed = set(re.findall(r'"([a-z-]+)"', names.group(1)))
    assert listed == {
        "data-reader",
        "proposer",
        "approver",
        "case-writer",
        "data-writer",
        "case-reader",
    }


def test_case_reader_session_still_pins_one_case(env, monkeypatch):
    """The role ceiling is CASE#A-*. The session policy still names this one alert."""
    monkeypatch.setenv(
        "CASE_READER_ROLE_ARN", "arn:aws:iam::ACCOUNT:role/agentic-bank-app-case-reader"
    )
    seen: dict[str, Any] = {}

    def fake_scoped(role_env, customer, table_arns, actions, attributes=None, session_name=None):
        seen.update(
            role_env=role_env,
            customer=customer,
            table_arns=table_arns,
            actions=actions,
            session_name=session_name,
        )

        class _Session:
            def client(self, name: str) -> str:
                return name

        return _Session()

    monkeypatch.setattr(handler, "scoped_session", fake_scoped)
    assert handler.case_reader("A-0001") == "dynamodb"
    assert seen == {
        "role_env": "CASE_READER_ROLE_ARN",
        "customer": "CASE#A-0001",
        "table_arns": "arn:aws:dynamodb:us-east-1:ACCOUNT:table/agentic-bank-data",
        "actions": READ,
        "session_name": "aml-review-A-0001",
    }


def test_proof_denies_a_customer_row_when_the_session_policy_is_omitted():
    """The denied key is a Cognito sub (the seeded partition), not a CASE# key."""
    proof = (ROOT / "proofs/aml_triage.py").read_text()
    assert 'ROLE = "agentic-bank-app-case-reader"' in proof
    assert "CASE_READER_ROLE_ARN" in proof
    assert "DATA_READER_ROLE_ARN" not in proof
    assert "no session policy" in proof
    assert 'Stack().subject("alex")' in proof
    assert '"ACCT#acc_alex_chk"' in proof
    assert "CUST#" not in proof


def test_record_queues_for_a_person_and_never_closes(env):
    ddb = FakeDdb()
    handler.record("A-0004", {"recommendation": "close", "failure": None, "advice": {}}, "e", ddb)
    row = ddb.rows[("CASE#A-0004", "RECOMMENDATION")]
    assert row["analyst_queue"] == {"S": "aml"} and row["status"] == {"S": "awaiting_analyst"}
    assert set(row) <= tf_list("aml.tf", "aml_attributes")

    handler.record("A-0004", {"recommendation": "closed"}, "e", ddb)  # garbage from upstream
    assert ddb.rows[("CASE#A-0004", "RECOMMENDATION")]["recommendation"] == {"S": "escalate"}
