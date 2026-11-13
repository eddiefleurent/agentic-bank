"""AML slice 2b without AWS or a model: the deep variant is deterministic and needs exploration,
the graph tools are read-only, paginated, capped and audited, and the depth breakdown is right."""

import json
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path

import pytest

from agentic_bank.aml import agents, data, deep, graph_source, rules, snapshot, triage
from agentic_bank.aml.graph_source import GraphSource
from evals.aml import harness
from test_aml_triage import DELEGATE, FINDINGS, row, scripted

DEEP_MANIFEST = json.loads(
    (Path(__file__).parent.parent / "evals/aml/deep/manifest.json").read_text()
)


@cache
def world():
    return harness.load_cases(variant="deep")


def deep_cases():
    return [c for c in world()[0] if c.depth != "shallow"]


# ---------------------------------------------------------------- the deep variant


def test_deep_variant_is_deterministic_and_matches_its_manifest():
    ds, labels = deep.generate()
    again, labels2 = deep.generate()
    assert data.dataset_hash(ds) == data.dataset_hash(again) == DEEP_MANIFEST["dataset_sha256"]
    assert labels == labels2
    committed = json.loads(
        (Path(__file__).parent.parent / "evals/aml/deep/labels.json").read_text()
    )
    assert committed == json.loads(json.dumps(labels))
    assert data.dataset_hash(data.generate()[0]) != DEEP_MANIFEST["dataset_sha256"]  # base intact


def test_deep_cases_are_balanced_and_tagged():
    cases = deep_cases()
    assert len(cases) == deep.DEEP_CASES == 16
    assert [c.verdict for c in cases].count("suspicious") == 8
    assert {c.depth for c in cases} == {"hops", "window"}
    assert sum(c.depth == "shallow" for c in world()[0]) == 50
    assert all(c.decisive_txn_ids for c in cases)


def test_every_deep_case_alerts_on_its_primary_customer():
    _, labels = deep.generate()
    by_id = {c["label_id"]: c for c in labels["cases"]}
    for c in deep_cases():
        label = by_id[c.id]
        assert c.alert.customer_id == label["primary_customer_id"]
        assert c.alert.rule_id in label["expected_rules"]


def test_decisive_evidence_is_outside_the_evidence_pack():
    """Designed so: window cases always; hop cases unless background links happen to bring the
    row within 2 hops (1 of 9 in this seed, reported in docs/aml.md)."""
    _, ds, alerts = world()
    inside = []
    for c in deep_cases():
        snap = snapshot.build_case_snapshot(c.alert, ds, alerts)
        if set(c.decisive_txn_ids) <= triage.txn_ids(snap):
            inside.append(c.id)
        if c.depth == "window":
            first = min(datetime.fromisoformat(ds.by_id[i].ts) for i in c.alert.txn_ids)
            assert all(
                datetime.fromisoformat(ds.by_id[i].ts) < first - timedelta(days=30)
                for i in c.decisive_txn_ids
            )
    assert len(inside) <= 1


def test_deep_snapshots_never_contain_labels():
    _, labels = deep.generate()
    ds = deep.generate()[0]
    alerts = rules.run_rules(ds)
    text = " ".join(
        json.dumps(snapshot.build_case_snapshot(c.alert, ds, alerts)) for c in deep_cases()
    )
    for c in labels["cases"]:
        assert c["explanation"] not in text and c["scenario"] not in text
    assert "suspicious" not in text and "benign" not in text and "decisive" not in text


# ---------------------------------------------------------------- graph source


def graph(case_id: str = "") -> GraphSource:
    _, ds, alerts = world()
    case = next(c for c in deep_cases() if c.id == case_id) if case_id else deep_cases()[0]
    return GraphSource(ds, alerts, case.alert.alert_id)


def test_graph_reads_are_paginated_and_audited():
    g = graph()
    cid = g.alert.customer_id
    first = g.transactions(cid)
    assert len(first["rows"]) <= graph_source.PAGE_SIZE and first["total_rows"] > 0
    if first["next_page"]:
        second = g.transactions(cid, page=first["next_page"])
        assert {r["txn_id"] for r in second["rows"]}.isdisjoint(r["txn_id"] for r in first["rows"])
    assert all(r["type"] != "card" and "memo_untrusted" in r for r in first["rows"])
    g.party(cid)
    g.neighbours(cid)
    pages = 2 if first["next_page"] else 1
    assert [e["tool"] for e in g.log] == ["transactions"] * pages + ["party", "neighbours"]
    assert {e["alert_id"] for e in g.log} == {g.alert.alert_id}
    assert {r["txn_id"] for r in first["rows"]} <= g.read_ids


def test_graph_reads_are_capped_per_alert(monkeypatch):
    monkeypatch.setattr(graph_source, "MAX_ROWS_READ", 60)
    g = graph()
    pages = [g.transactions(c) for c in list(world()[1].customers)[:5]]
    assert any("error" in p for p in pages) and g.rows_read <= 60 + graph_source.PAGE_SIZE


def test_graph_tools_never_change_the_dataset():
    _, ds, _ = world()
    before = data.dataset_hash(ds)
    g = graph()
    for t in g.transactions(g.alert.customer_id)["rows"][:5]:
        g.follow(t["txn_id"], "forward")
        g.follow(t["txn_id"], "backward")
    g.activity(g.alert.customer_id)
    g.prior_alerts(g.alert.customer_id)
    assert data.dataset_hash(ds) == before
    assert "error" in g.transaction("T999999") and "error" in g.party("X9999")


def test_follow_crosses_to_the_other_customer_account():
    _, ds, _ = world()
    g = graph()
    p2p = next(
        t
        for t in ds.transactions
        if t.type == "p2p" and t.direction == "out" and t.counterparty_id in ds.customers
    )
    out = g.follow(p2p.txn_id, "forward", 336)
    assert out["account"] == p2p.counterparty_id
    assert all(r["customer_id"] == p2p.counterparty_id for r in out["rows"])
    assert all(r["direction"] == "out" and r["ts"] >= p2p.ts for r in out["rows"])


def test_graph_agents_start_from_the_alert_and_cite_only_what_they_read():
    _, ds, alerts = world()
    case = deep_cases()[0]
    snap = snapshot.build_case_snapshot(case.alert, ds, alerts)
    unread = next(t.txn_id for t in ds.transactions if t.txn_id not in case.alert.txn_ids)
    answer = {
        "recommendation": "escalate",
        "typology": "layering",
        "risk_factors": [{"statement": "x", "txn_ids": [unread]}],
        "mitigating_factors": [],
        "instruction_memo_txn_ids": [],
        "narrative": "x",
    }
    model_for = scripted(
        [DELEGATE, ("tools", [("Triage", answer)])],
        flow=[("tools", [("get_alert", {})]), FINDINGS],
        kyc=[("tools", [("kyc_profile", {"customer_id": case.alert.customer_id})]), FINDINGS],
    )
    run = agents.triage_graph(None, "m", {"alert": snap["alert"]}, ds, alerts, model_for=model_for)
    assert (run.outcome.failure or "").startswith("cited ids not in case")
    assert run.audit["calls"] >= 2 and "get_alert" in run.tool_calls

    answer["risk_factors"][0]["txn_ids"] = [case.alert.txn_ids[0]]  # read via get_alert
    model_for = scripted(
        [DELEGATE, ("tools", [("Triage", answer)])],
        flow=[("tools", [("get_alert", {})]), FINDINGS],
    )
    run = agents.triage_graph(None, "m", {"alert": snap["alert"]}, ds, alerts, model_for=model_for)
    assert run.outcome.failure is None and run.outcome.recommendation == "escalate"


def test_harness_hands_graph_reviewers_only_the_alert():
    cases, ds, alerts = world()
    seen = {}

    def spy(_bedrock, _model, given):
        seen.update(given)
        return triage.TriageRun(outcome=triage.failed("spy"))

    harness.evaluate_case(cases[0], spy, None, "m", ds, alerts, graph=True)
    assert set(seen) == {"alert"}


# ---------------------------------------------------------------- scoring


def test_depth_breakdown_and_repeat_aggregation():
    rows = [
        {**row("S1", "suspicious", "escalate"), "depth": "shallow"},
        {**row("S2", "suspicious", "close"), "depth": "hops"},
        {**row("S3", "suspicious", "escalate"), "depth": "window"},
        {**row("B1", "benign", "escalate"), "depth": "hops"},
        {**row("B2", "benign", "close"), "depth": "shallow"},
    ]
    m = harness.summarize(rows)
    assert m["by_depth"]["shallow"]["recall"] == 1.0
    assert m["by_depth"]["deep"]["recall"] == 0.5
    assert m["by_depth"]["deep"]["false_escalation_rate"] == 1.0
    assert m["by_depth"]["shallow"]["false_escalation_rate"] == 0.0
    other = harness.summarize([{**r, "recommendation": "escalate"} for r in rows])
    agg = harness.aggregate([m, other])
    assert agg["deep_recall"] == {"mean": 0.75, "min": 0.5, "max": 1.0}
    table = harness.compare_markdown({"x": [{"metrics": m}, {"metrics": other}]})
    assert "x (2 runs)" in table and "75% (50%-100%)" in table


@pytest.mark.parametrize("variant", ["base", "deep"])
def test_fingerprint_depends_on_variant(variant):
    other = "deep" if variant == "base" else "base"
    assert harness.fingerprint("a", "m", variant) != harness.fingerprint("a", "m", other)
