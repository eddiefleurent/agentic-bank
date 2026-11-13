"""The "blind" AML variant: 20 cases written by a different model, imported mechanically.

`evals/aml/blind/cases.json` was written from public typologies by a model that never saw this
repo, our rules, our prompts or our deep cases. This module only IMPORTS it. It does not edit the
file and it does not judge the content.

What the import does:
  * every case customer and counterparty gets a fresh id (C201+, X0501+) in a seeded shuffle over
    all 20 cases, so no id or id order says suspicious or benign;
  * `day` and `hour` become a timestamp in the existing 90-day window (day 0 = 2026-08-01);
  * the 20 cases go into the standard base dataset, so the rules and the graph tools run over a
    realistic store (200 other customers, about 49,000 other transactions, 114 other alerts);
  * transaction ids are re-assigned over the merged dataset with the same sort as the generator;
  * the existing rules run. A case whose alerted customer raises no alert gets one synthetic alert
    with `rule_id="EXT"` (an alert from another monitoring system) that flags that customer's
    non-card transactions.

What the model never sees: `label`, `typology`, `summary`, `decisive_evidence` and the case keys.
They are used only by the harness to score. Typologies are free text, so typology accuracy is n/a.

Limit: the case customers and their counterparties are new, so nobody in the 200-customer
background touches them. The graph agents have real noise in the store but the case's own
neighbourhood is exactly what the case file says.
"""

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agentic_bank.aml import data, rules
from agentic_bank.aml.data import Counterparty, Customer, Dataset, Txn
from agentic_bank.aml.rules import Alert

CASES = Path(__file__).resolve().parents[3] / "evals" / "aml" / "blind" / "cases.json"
SEED = 20261113
FIRST_CUSTOMER = 201
FIRST_COUNTERPARTY = 501


@dataclass(frozen=True)
class BlindLabel:
    """Ground truth for one imported case. Only the harness reads it."""

    label_id: str
    verdict: str  # suspicious | benign
    alerted_customer_id: str
    decisive_txn_ids: list[str]


def load_raw(path: Path = CASES) -> list[dict[str, Any]]:
    return json.loads(path.read_text())["cases"]


def build(
    path: Path = CASES, seed: int = SEED
) -> tuple[Dataset, list[Alert], list[BlindLabel], int]:
    """Return (dataset, alerts, labels, number of EXT alerts created)."""
    raw = load_raw(path)
    base, _ = data.generate()
    rng = random.Random(seed)
    order = list(range(len(raw)))
    rng.shuffle(order)  # label ids and entity ids follow this order, not the file's

    customers = dict(base.customers)
    cps = dict(base.counterparties)
    txns = list(base.transactions)
    next_c, next_x = FIRST_CUSTOMER, FIRST_COUNTERPARTY
    new_rows: list[tuple[int, str, Txn]] = []  # (case position, case txn key, row)
    alerted: dict[int, str] = {}

    for n, idx in enumerate(order):
        case = raw[idx]
        ids: dict[str, str] = {}
        for c in case["customers"]:
            cid = f"C{next_c:03d}"
            next_c += 1
            ids[c["key"]] = cid
            customers[cid] = Customer(
                cid,
                c["name"],
                c["occupation"],
                c["declared_monthly_income_cents"],
                c["expected_monthly_volume_cents"],
                c["risk_rating"],
                c["account_open_date"],
                list(c["expected_countries"]),
                c["notes"],
            )
        for p in case["counterparties"]:
            xid = f"X{next_x:04d}"
            next_x += 1
            ids[p["key"]] = xid
            cps[xid] = Counterparty(xid, p["name"], p["kind"], p["country"])
        for t in case["transactions"]:
            ts = data.START + timedelta(days=t["day"], hours=t["hour"])
            other = None if t["type"] == "cash_deposit" else ids[t["other_party"]]
            row = Txn(
                "",
                ts.isoformat(),
                ids[t["customer_key"]],
                t["type"],
                t["direction"],
                t["amount_cents"],
                other,
                t["memo"],
            )
            assert data.START <= ts < data.END, (case["case_key"], t["key"])
            new_rows.append((n, t["key"], row))
        alerted[n] = ids[case["alerted_customer_key"]]

    txns += [r for _, _, r in new_rows]
    txns.sort(
        key=lambda t: (
            t.ts,
            t.customer_id,
            t.type,
            t.direction,
            t.amount_cents,
            t.counterparty_id or "",
            t.memo,
        )
    )
    for i, t in enumerate(txns, 1):
        t.txn_id = f"T{i:06d}"
    ds = Dataset(customers, cps, txns)

    alerts = rules.run_rules(ds)
    by_customer: dict[str, list[Alert]] = {}
    for a in alerts:
        by_customer.setdefault(a.customer_id, []).append(a)
    ext = 0
    next_alert = len(alerts) + 1
    for n in range(len(order)):
        cid = alerted[n]
        if by_customer.get(cid):
            continue
        flagged = [t for t in ds.by_customer[cid] if t.type != "card"]
        alert = Alert(
            f"A-{next_alert:04d}",
            "EXT",
            cid,
            tuple(t.txn_id for t in flagged),
            max(datetime.fromisoformat(t.ts) for t in flagged).date().isoformat(),
        )
        next_alert += 1
        alerts.append(alert)
        by_customer[cid] = [alert]
        ext += 1

    labels = []
    for n, idx in enumerate(order):
        case = raw[idx]
        ids_by_key = {k: r.txn_id for p, k, r in new_rows if p == n}
        labels.append(
            BlindLabel(
                f"BLIND-{n + 1:03d}",
                case["label"],
                alerted[n],
                sorted(
                    ids_by_key[k] for k in case["decisive_evidence"] if k in ids_by_key
                ),  # KYC items skipped
            )
        )
    return ds, alerts, labels, ext


def pick_alert(alerts: list[Alert], customer_id: str) -> Alert:
    """The customer's first alert by id (no label is used to choose it)."""
    return min((a for a in alerts if a.customer_id == customer_id), key=lambda a: a.alert_id)


def manifest(ds: Dataset, alerts: list[Alert], ext: int) -> dict[str, Any]:
    return {
        "seed": SEED,
        "variant": "blind",
        "transactions": len(ds.transactions),
        "alerts": len(alerts),
        "ext_alerts": ext,
        "cases": len(load_raw()),
        "dataset_sha256": data.dataset_hash(ds),
    }


def main() -> None:
    """Write evals/aml/blind/manifest.json (the dataset itself is not committed)."""
    ds, alerts, _, ext = build()
    m = manifest(ds, alerts, ext)
    (CASES.parent / "manifest.json").write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")
    print(json.dumps(m, indent=1))


if __name__ == "__main__":
    main()
