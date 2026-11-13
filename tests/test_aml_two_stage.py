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
            "stage1": {
                "recommendation": "close",
                "failure": None,
                "input_tokens": 1000,
                "output_tokens": 0,
                "seconds": 2.0,
            },
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


# ---------------------------------------------------------------- round 2: overturn rules


def raw(rec="escalate", failure=None, risk=(), read=(), owners=None, pack=("T1", "T2")):
    return {
        "recommendation": rec,
        "failure": failure,
        "cited_risk_ids": list(risk),
        "cited_ids": list(risk),
        "read_ids": list(read),
        "owners": owners or {},
        "pack_ids": list(pack),
        "alerted_customer_id": "C1",
    }


def verdicts(r) -> tuple[bool, bool, bool]:
    return two_stage.rule_a(r), two_stage.rule_b(r), two_stage.rule_c(r)


def test_rules_on_a_new_read_txn_on_another_account():
    r = raw(risk=["T9"], read=["T9"], owners={"T9": "C2"})
    assert verdicts(r) == (True, True, True)


def test_rules_on_pack_txn_on_another_account_only_b_and_c_overturn():
    r = raw(risk=["T1"], read=["T1"], owners={"T1": "C2"})
    assert verdicts(r) == (False, True, True)


def test_rules_on_new_txn_on_the_alerted_account_only_a_and_c_overturn():
    r = raw(risk=["T9"], read=["T9"], owners={"T9": "C1"})
    assert verdicts(r) == (True, False, True)


def test_rules_on_only_pack_ids_on_the_alerted_account_only_c_overturns():
    r = raw(risk=["T1"], read=["T1"], owners={"T1": "C1"})
    assert verdicts(r) == (False, False, True)


def test_rules_ignore_cited_ids_the_agents_did_not_read():
    r = raw(risk=["T9"], read=["T1"], owners={"T9": "C2"})
    assert verdicts(r) == (False, False, True)


def test_no_rule_overturns_on_agent_close_or_failure():
    for r in (
        raw(rec="close", risk=["T9"], read=["T9"], owners={"T9": "C2"}),
        raw(failure="turn limit", risk=["T9"], read=["T9"], owners={"T9": "C2"}),
    ):
        assert verdicts(r) == (False, False, False)


def test_rule_ordering_is_a_and_b_inside_c():
    import itertools

    for risk, owner, pack in itertools.product(
        (["T1"], ["T9"], ["T1", "T9"]), ("C1", "C2"), (("T1",), ("T9",))
    ):
        r = raw(risk=risk, read=risk, owners={i: owner for i in risk}, pack=pack)
        a, b, c = verdicts(r)
        assert (not a or c) and (not b or c)


def test_live_two_stage_stores_the_raw_stage_two_result():
    snap = {**snapshot(), "alert": {"alert_id": "A-1", "customer_id": "C1"}}
    second = stage("escalate", cited=["T000900"], read=["T000900"])
    out = two_stage.two_stage(stage("close"), second, lambda i: "C2")(None, "m", snap)
    stored = out.audit["two_stage"]["stage2_raw"]
    assert stored["owners"] == {"T000900": "C2"} and stored["alerted_customer_id"] == "C1"
    assert stored["pack_ids"] == ["T000001", "T000002"] and stored["read_ids"] == ["T000900"]
    assert two_stage.rule_b(stored) and two_stage.rule_a(stored)


# ---------------------------------------------------------------- round 2: combined set


@cache
def world2():
    return harness.load_cases(variant="deep2")


def test_deep2_has_the_deep_variant_plus_20_blind_deep_cases():
    cases, ds, alerts = world2()
    assert len(cases) == 86
    bd = [c for c in cases if c.depth == "blind_deep"]
    assert len(bd) == 20 and sum(c.verdict == "suspicious" for c in bd) == 10
    deep = [c for c in cases if c.depth in ("hops", "window", "blind_deep")]
    assert len(deep) == 36 and sum(c.verdict == "suspicious" for c in deep) == 18
    assert all(i in ds.by_id for c in cases for i in c.decisive_txn_ids)
    assert all("D0" not in c.id and "S" not in c.id for c in bd)


def test_deep2_labels_are_never_shown_to_the_model():
    cases, ds, alerts = world2()
    raw_cases = blind.load_raw(blind.DEEP_CASES)
    secrets = {c["summary"] for c in raw_cases} | {c["typology"] for c in raw_cases}
    secrets |= {c["evidence_location"] for c in raw_cases} | {c["case_key"] for c in raw_cases}
    secrets |= {"decisive_evidence", "evidence_location"}
    blob = json.dumps(ds.to_dict())
    for s in secrets:
        assert s not in blob, s
    for c in cases[-20:]:
        snap = build_case_snapshot(c.alert, ds, alerts)
        seen = triage.case_text(snap) + triage.ask(snap)
        assert not any(s in seen for s in secrets)


def test_deep2_matches_its_manifest():
    ds, alerts, _, ext, _ = blind.build_merged(
        blind.DEEP_CASES,
        base=__import__("agentic_bank.aml.deep", fromlist=["x"]).generate()[0],
        prefix="BDEEP",
    )
    m = json.loads((blind.BLIND_DIR / "deep2_manifest.json").read_text())
    assert m["dataset_sha256"] == __import__("agentic_bank.aml.data", fromlist=["x"]).dataset_hash(
        ds
    )
    assert m["ext_alerts"] == ext


def test_summary_groups_blind_and_blind_deep_apart_from_deep():
    rows = [
        {"depth": d, "verdict": "suspicious", "recommendation": "escalate", "failure": None}
        for d in ("shallow", "hops", "window", "blind", "blind_deep")
    ]
    by = harness.by_depth(rows)
    assert (by["shallow"]["suspicious"], by["deep"]["suspicious"]) == (1, 2)
    assert by["blind"]["suspicious"] == 1 and by["blind_deep"]["suspicious"] == 1


def test_round2_scoring_rescoring_noise_band_and_flip_rate():
    from evals.aml import round2

    def row(cid, rec, r2=None, failure=None, depth="hops"):
        return {
            "id": cid, "depth": depth, "verdict": "suspicious",
            "stage1_recommendation": rec, "stage1_failure": failure, "stage2_raw": r2,
        }  # fmt: skip

    pack_only_other = raw(risk=["T1"], read=["T1"], owners={"T1": "C2"})
    closed = row("a", "close", pack_only_other)
    assert round2.single_call(closed) == "close"
    assert [round2.with_rule(closed, r) for r in "ABC"] == ["close", "escalate", "escalate"]
    assert round2.single_call(row("b", "close", failure="schema")) == "escalate"
    assert round2.with_rule(row("b", "escalate"), "A") == "escalate"  # stage 2 never ran
    assert round2.noise_band([0.5, 0.75, 0.625]) == 0.25
    empty = round2.measures([row("a", "close", depth="blind_deep")], round2.single_call)
    assert empty["shallow_benign_fe"] is None and empty["blind_deep_recall"] == 0.0
    runs = [[row("a", "close"), row("b", "escalate")], [row("a", "escalate"), row("b", "escalate")]]
    assert round2.flip_rate(runs)["all"] == 0.5
