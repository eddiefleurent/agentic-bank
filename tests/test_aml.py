"""AML slice 1: deterministic data, rules that catch every planted case, safe bounded snapshots."""

import json
from functools import cache

from agentic_bank.aml import data, rules, snapshot

MANIFEST = json.loads((data.Path(__file__).parent.parent / "evals/aml/manifest.json").read_text())
LABEL_KEYS = {
    "verdict",
    "typology",
    "scenario",
    "expected_rules",
    "evidence_txn_ids",
    "injection_txn_ids",
    "explanation",
    "label_id",
    "noise_customer_ids",
}


@cache
def world():
    ds, labels = data.generate()
    return ds, labels, rules.run_rules(ds)


def test_same_seed_same_hash_and_matches_committed_manifest():
    ds, labels, alerts = world()
    again, labels2 = data.generate()
    assert data.dataset_hash(again) == data.dataset_hash(ds) == MANIFEST["dataset_sha256"]
    assert labels2 == labels
    assert data.dataset_hash(data.generate(seed=1)[0]) != MANIFEST["dataset_sha256"]
    assert len(alerts) == MANIFEST["alerts"]


def test_dataset_size_and_planted_counts():
    ds, labels, _ = world()
    assert len(ds.customers) == 200 and len(ds.counterparties) == 500
    assert 40_000 <= len(ds.transactions) <= 60_000
    verdicts = [c["verdict"] for c in labels["cases"]]
    assert verdicts.count("suspicious") == 25 and verdicts.count("benign") == 25
    injected = [i for c in labels["cases"] for i in c["injection_txn_ids"]]
    assert 3 <= len(injected) <= 10
    for i in injected:
        assert (
            "SYSTEM" in ds.by_id[i].memo
            or "AI" in ds.by_id[i].memo
            or "assistant" in ds.by_id[i].memo
        )
    for c in labels["cases"]:
        assert all(i in ds.by_id for i in c["evidence_txn_ids"])


def test_every_planted_case_raises_an_alert():
    ds, labels, alerts = world()
    for c in labels["cases"]:
        hit = [
            a
            for a in alerts
            if a.customer_id in c["customer_ids"]
            and a.rule_id in c["expected_rules"]
            and set(a.txn_ids) & set(c["evidence_txn_ids"])
        ]
        assert hit, (c["label_id"], c["scenario"])


def test_snapshot_never_contains_labels():
    ds, labels, alerts = world()
    scenarios = {c["scenario"] for c in labels["cases"]}
    explanations = [c["explanation"] for c in labels["cases"]]

    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)

    for a in alerts:
        snap = snapshot.build_case_snapshot(a, ds, alerts)
        text = json.dumps(snap)
        assert not LABEL_KEYS & set(keys(snap))
        assert not any(s in text for s in scenarios)
        assert not any(e in text for e in explanations)
        assert "suspicious" not in text and "benign" not in text


def test_snapshot_is_bounded():
    ds, _, alerts = world()
    for a in alerts:
        snap = snapshot.build_case_snapshot(a, ds, alerts)
        assert len(snap["transactions"]) <= snapshot.MAX_TXNS
        assert all(t["type"] != "card" for t in snap["transactions"] + snap["flagged_transactions"])
        assert all(t["hop"] <= snapshot.MAX_HOPS for t in snap["transactions"])
        assert len(json.dumps(snap)) < 120_000
        assert len(snapshot.to_items(snap)) < 400


def test_snapshot_only_contains_data_reachable_from_the_customer():
    ds, _, alerts = world()
    for a in alerts:
        reach = {a.customer_id}
        frontier = [a.customer_id]
        for _ in range(snapshot.MAX_HOPS):
            nxt = [
                t.counterparty_id
                for c in frontier
                for t in ds.by_customer[c]
                if t.counterparty_id in ds.customers and t.counterparty_id not in reach
            ]
            reach.update(nxt)
            frontier = nxt
        snap = snapshot.build_case_snapshot(a, ds, alerts)
        rows = snap["transactions"] + snap["flagged_transactions"]
        assert {t["customer_id"] for t in rows} <= reach
        assert {c["customer_id"] for c in snap["linked_customers"]} <= reach
        assert {i["pk"] for i in snapshot.to_items(snap)} == {f"CASE#{a.alert_id}"}
        for t in rows:
            assert (
                t["counterparty_id"] is None
                or t["counterparty_id"] in ds.customers | ds.counterparties
            )


def test_flagged_transactions_and_prior_alerts():
    ds, _, alerts = world()
    a = next(
        x
        for x in alerts
        if any(y.customer_id == x.customer_id and y.created < x.created for y in alerts)
    )
    snap = snapshot.build_case_snapshot(a, ds, alerts)
    assert {t["txn_id"] for t in snap["flagged_transactions"]} == set(a.txn_ids)
    assert snap["prior_alerts"] and all(p["created"] < a.created for p in snap["prior_alerts"])
