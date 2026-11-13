"""Two-stage reviewer and the blind variant, without AWS or a model. Stage reviewers are scripted
callables that return canned TriageRuns, so the tests exercise only the two-stage rule."""

import json
from functools import cache
from typing import Any, Literal

from agentic_bank.aml import blind, triage, two_stage
from agentic_bank.aml.snapshot import build_case_snapshot
from agentic_bank.aml.triage import TriageOutcome, TriageRun
from evals.aml import harness

# ---------------------------------------------------------------- two-stage rule


def snapshot() -> dict[str, Any]:
    return {
        "alert": {"alert_id": "A-1"},
        "kyc": {},
        "flagged_transactions": [{"txn_id": "T000001"}],
        "transactions": [{"txn_id": "T000002"}],
    }  # the evidence pack holds T000001 and T000002


def advice(*ids: str) -> dict[str, Any]:
    return {"risk_factors": [{"statement": "x", "txn_ids": list(ids)}], "typology": "none"}


def stage(
    rec: Literal["close", "escalate"],
    failure: str | None = None,
    cited=(),
    read=(),
    calls: list | None = None,
):
    def reviewer(_b: Any, _m: str, given: dict[str, Any]) -> TriageRun:
        if calls is not None:
            calls.append(given)
        return TriageRun(
            outcome=TriageOutcome(rec, failure, advice(*cited)),
            usage={"inputTokens": 100, "outputTokens": 10, "model_calls": 1},
            seconds=1.0,
            audit={"read_ids": list(read)},
        )

    return reviewer


def run(first, second):
    return two_stage.two_stage(first, second)(None, "m", snapshot())


def test_baseline_escalate_passes_through_and_stage_two_never_runs():
    calls: list = []
    out = run(stage("escalate", cited=["T000001"]), stage("close", calls=calls))
    assert out.outcome.recommendation == "escalate" and calls == []
    info = out.audit["two_stage"]
    assert info["decided_by"] == "stage1_escalate" and info["stage2"] is None


def test_baseline_failure_escalates_without_stage_two():
    calls: list = []
    out = run(stage("escalate", failure="baseline error: X"), stage("close", calls=calls))
    assert out.outcome.recommendation == "escalate" and calls == []
    assert out.outcome.failure == "baseline error: X"
    assert out.audit["two_stage"]["decided_by"] == "stage1_failure"


def test_close_then_agents_escalate_with_new_read_evidence_escalates():
    calls: list = []
    second = stage("escalate", cited=["T000900"], read=["T000900", "T000001"], calls=calls)
    out = run(stage("close"), second)
    assert calls == [{"alert": {"alert_id": "A-1"}}]  # stage 2 gets the alert alone
    assert out.outcome.recommendation == "escalate" and out.outcome.failure is None
    info = out.audit["two_stage"]
    assert info["decided_by"] == "stage2_escalate_new_evidence"
    assert info["new_evidence_ids"] == ["T000900"]
    assert out.usage["inputTokens"] == 200 and out.usage["model_calls"] == 2
    assert out.seconds == 2.0 and info["stage1"]["seconds"] == info["stage2"]["seconds"] == 1.0


def test_close_then_agents_escalate_on_pack_evidence_only_stays_closed():
    second = stage("escalate", cited=["T000001", "T000002"], read=["T000001", "T000002"])
    out = run(stage("close"), second)
    assert out.outcome.recommendation == "close"
    assert out.audit["two_stage"]["decided_by"] == "stage2_escalate_no_new_evidence_close_stands"
    assert out.outcome.advice["two_stage"]["stage2_advice"]["risk_factors"]  # kept for the analyst


def test_a_new_id_the_agents_did_not_read_does_not_count():
    second = stage("escalate", cited=["T000900"], read=["T000001"])
    assert run(stage("close"), second).outcome.recommendation == "close"


def test_stage_two_failure_leaves_the_close_flagged_for_the_analyst():
    out = run(stage("close"), stage("escalate", failure="turn limit"))
    assert out.outcome.recommendation == "close" and out.outcome.failure is None
    assert out.outcome.advice["two_stage"]["stage2_failed"] == "turn limit"
    assert out.audit["two_stage"]["decided_by"] == "stage2_failed_close_stands"


def test_agents_close_keeps_the_close():
    out = run(stage("close"), stage("close"))
    assert out.outcome.recommendation == "close"
    assert out.audit["two_stage"]["decided_by"] == "stage2_close"


def test_select_builds_two_stage_and_needs_a_world():
    import pytest

    with pytest.raises(ValueError):
        triage.select("two_stage")
    ds, alerts = world()[1:]
    assert callable(triage.select("two_stage", (ds, alerts)))


def test_harness_row_records_stage_costs():
    fields = harness.stage_fields(
        {
            "decided_by": "stage2_close",
            "stage1": {"input_tokens": 1000, "output_tokens": 0, "seconds": 2.0},
            "stage2": {"input_tokens": 0, "output_tokens": 1000, "seconds": 5.0},
        },
        "openai.gpt-oss-120b-1:0",
    )
    assert fields["stage1_cost_usd"] == 0.00015 and fields["stage2_cost_usd"] == 0.0006
    assert fields["stage2_latency_s"] == 5.0


# ---------------------------------------------------------------- blind variant


@cache
def world():
    return harness.load_cases(variant="blind")


def test_blind_import_is_deterministic_and_matches_its_manifest():
    ds, alerts, labels, ext = blind.build()
    again = blind.build()
    assert blind.manifest(ds, alerts, ext) == json.loads(
        (blind.CASES.parent / "manifest.json").read_text()
    )
    assert [a.alert_id for a in alerts] == [a.alert_id for a in again[1]]
    assert labels == again[2]


def test_blind_cases_use_fresh_ids_that_carry_no_label():
    cases, ds, alerts = world()
    assert len(cases) == 20 and sum(c.verdict == "suspicious" for c in cases) == 10
    assert all(c.depth == "blind" and c.typology == harness.NO_TYPOLOGY for c in cases)
    ids = [c.alert.customer_id for c in cases]
    assert len(set(ids)) == 20 and all(int(i[1:]) > 200 for i in ids)
    # the shuffle: the first ten ids are not all one label, and ids do not sort by label
    first = [c.verdict for c in sorted(cases, key=lambda c: c.alert.customer_id)][:10]
    assert 0 < first.count("suspicious") < 10
    assert all("S" not in c.id and "B0" not in c.id for c in cases)


def test_every_blind_case_has_an_alert_and_ext_is_used_only_when_no_rule_fired():
    cases, _, alerts = world()
    ext = [a for a in alerts if a.rule_id == "EXT"]
    assert ext and all(a.txn_ids for a in ext)
    for c in cases:
        mine = [a for a in alerts if a.customer_id == c.alert.customer_id]
        assert c.alert in mine
        if c.alert.rule_id == "EXT":
            assert len(mine) == 1


def test_the_model_never_sees_labels_summaries_or_decisive_evidence():
    cases, ds, alerts = world()
    raw = blind.load_raw()
    secrets = {c["summary"] for c in raw} | {c["typology"] for c in raw}
    secrets |= {c["case_key"] for c in raw} | {"decisive_evidence", "BLIND-0"}
    for c in cases:
        snap = build_case_snapshot(c.alert, ds, alerts)
        seen = triage.case_text(snap) + triage.ask(snap)
        graph_view = json.dumps({"alert": snap["alert"]})
        for s in secrets:
            assert s not in seen and s not in graph_view, s
    # nothing in the whole dataset carries them either (graph tools read from it)
    blob = json.dumps(ds.to_dict())
    for s in secrets:
        assert s not in blob, s
