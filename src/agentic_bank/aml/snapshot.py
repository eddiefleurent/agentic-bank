"""Per-alert case snapshot: a bounded, JSON-serialisable copy of only this alert's slice of data.

Why a copy: agents work on the snapshot, never on the whole transaction store. A bug or a
prompt-injected agent can then only read this one case. Later the snapshot is stored in DynamoDB
and IAM `dynamodb:LeadingKeys` limits a case's role to its own partition key.

Key layout (pk is the partition key; one item per row, sk is the sort key):
  pk=CASE#<alert_id>  sk=ALERT                       the alert
                      sk=KYC#<customer_id>           alert customer, or a linked customer (summary)
                      sk=FLAGGED#<ts>#<txn_id>       the alert's own triggering transactions
                      sk=TXN#<ts>#<txn_id>           context transactions, with hop 0, 1 or 2
                      sk=COUNTERPARTY#<id>           external counterparties seen in the case
                      sk=PRIOR#<alert_id>            earlier alerts for the same customer
                      sk=SUMMARY                     activity summary of the alert customer
                      sk=META                        window, limits, truncated flag

Contents: no ground-truth labels (labels live in evals/aml/labels.json, a different file), no card
spend rows (card spend is only counted in the summary), and no customer further than 2 hops.
Memos are copied verbatim. They are untrusted text that may contain instructions.
"""

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from agentic_bank.aml.data import Dataset, Txn
from agentic_bank.aml.rules import Alert

WINDOW_BEFORE_DAYS = 30
WINDOW_AFTER_DAYS = 7
MAX_HOPS = 2
MAX_TXNS = 150  # context transactions, excluding the flagged ones
MAX_PER_LINKED = 15
MIN_LINKED_CENTS = 100_000  # at hops 1 and 2 only transactions of $1,000 or more are copied
SCHEMA_VERSION = 1


def _row(t: Txn, hop: int | None = None) -> dict[str, Any]:
    d = {
        "txn_id": t.txn_id,
        "ts": t.ts,
        "customer_id": t.customer_id,
        "type": t.type,
        "direction": t.direction,
        "amount_cents": t.amount_cents,
        "counterparty_id": t.counterparty_id,
        "memo": t.memo,
    }
    if hop is not None:
        d["hop"] = hop
    return d


def _linked_customers(txns: list[Txn], ds: Dataset) -> list[str]:
    out: list[str] = []
    for t in txns:
        if t.counterparty_id in ds.customers and t.counterparty_id not in out:
            out.append(t.counterparty_id)
    return out


def build_case_snapshot(
    alert: Alert,
    ds: Dataset,
    all_alerts: list[Alert] | None = None,
) -> dict[str, Any]:
    first = min(ds.by_id[i].ts for i in alert.txn_ids)
    last = max(ds.by_id[i].ts for i in alert.txn_ids)
    start = datetime.fromisoformat(first) - timedelta(days=WINDOW_BEFORE_DAYS)
    end = datetime.fromisoformat(last) + timedelta(days=WINDOW_AFTER_DAYS)

    def in_window(t: Txn) -> bool:
        return start <= datetime.fromisoformat(t.ts) <= end and t.type != "card"

    flagged = [ds.by_id[i] for i in alert.txn_ids if ds.by_id[i].customer_id == alert.customer_id]
    flagged_ids = {t.txn_id for t in flagged}

    context: list[dict[str, Any]] = []
    dist = {alert.customer_id: 0}
    frontier = [alert.customer_id]
    truncated = False
    kept_txns: list[Txn] = list(flagged)
    for hop in range(MAX_HOPS + 1):
        nxt: list[str] = []
        for cid in frontier:
            rows = [t for t in ds.by_customer[cid] if in_window(t) and t.txn_id not in flagged_ids]
            if hop > 0:
                rows = [t for t in rows if t.amount_cents >= MIN_LINKED_CENTS]
                if len(rows) > MAX_PER_LINKED:
                    rows, truncated = rows[:MAX_PER_LINKED], True
            for t in rows:
                if len(context) >= MAX_TXNS:
                    truncated = True
                    break
                context.append(_row(t, hop))
                kept_txns.append(t)
            if hop < MAX_HOPS:
                nxt += [
                    c
                    for c in _linked_customers([t for t in ds.by_customer[cid] if in_window(t)], ds)
                    if c not in dist and c not in nxt
                ]
        dist.update({c: hop + 1 for c in nxt})
        frontier = nxt

    mentioned = {t.customer_id for t in kept_txns} | {
        t.counterparty_id for t in kept_txns if t.counterparty_id in ds.customers
    }
    linked_ids = sorted(c for c in mentioned if c in dist and c != alert.customer_id)
    cp_ids = sorted(
        {t.counterparty_id for t in kept_txns if t.counterparty_id in ds.counterparties}
    )
    mine = ds.by_customer[alert.customer_id]
    summary = {
        "transactions_90d": len(mine),
        "by_type": dict(sorted(Counter(t.type for t in mine).items())),
        "cash_in_cents": sum(t.amount_cents for t in mine if t.type == "cash_deposit"),
        "wire_in_cents": sum(
            t.amount_cents for t in mine if t.type == "wire" and t.direction == "in"
        ),
        "wire_out_cents": sum(
            t.amount_cents for t in mine if t.type == "wire" and t.direction == "out"
        ),
        "card_spend_cents": sum(t.amount_cents for t in mine if t.type == "card"),
    }
    prior = [
        a
        for a in (all_alerts or [])
        if a.customer_id == alert.customer_id and a.created < alert.created
    ]
    cust = ds.customers[alert.customer_id]
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": f"CASE#{alert.alert_id}",
        "alert": alert.to_dict(),
        "window": {"start": start.date().isoformat(), "end": end.date().isoformat()},
        "customer": {"customer_id": cust.customer_id, "name": cust.name},
        "kyc": {k: v for k, v in asdict(cust).items() if k not in ("customer_id", "name")},
        "flagged_transactions": [_row(t) for t in flagged],
        "transactions": context,
        "linked_customers": [
            {
                "customer_id": c,
                "name": ds.customers[c].name,
                "occupation": ds.customers[c].occupation,
                "risk_rating": ds.customers[c].risk_rating,
                "hop": dist[c],
            }
            for c in linked_ids
        ],
        "counterparties": [asdict(ds.counterparties[c]) for c in cp_ids],
        "prior_alerts": [a.to_dict() for a in prior],
        "activity_summary": summary,
        "limits": {
            "max_hops": MAX_HOPS,
            "max_context_txns": MAX_TXNS,
            "window_days": [WINDOW_BEFORE_DAYS, WINDOW_AFTER_DAYS],
            "truncated": truncated,
        },
    }


def to_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """The DynamoDB-style item list for a snapshot (pk/sk plus the row's own fields)."""
    pk = snapshot["case_id"]
    items: list[dict[str, Any]] = [{"pk": pk, "sk": "ALERT", **snapshot["alert"]}]
    items.append(
        {
            "pk": pk,
            "sk": f"KYC#{snapshot['customer']['customer_id']}",
            **snapshot["customer"],
            **snapshot["kyc"],
        }
    )
    items += [
        {"pk": pk, "sk": f"KYC#{c['customer_id']}", **c} for c in snapshot["linked_customers"]
    ]
    items += [
        {"pk": pk, "sk": f"FLAGGED#{t['ts']}#{t['txn_id']}", **t}
        for t in snapshot["flagged_transactions"]
    ]
    items += [
        {"pk": pk, "sk": f"TXN#{t['ts']}#{t['txn_id']}", **t} for t in snapshot["transactions"]
    ]
    items += [
        {"pk": pk, "sk": f"COUNTERPARTY#{c['counterparty_id']}", **c}
        for c in snapshot["counterparties"]
    ]
    items += [{"pk": pk, "sk": f"PRIOR#{a['alert_id']}", **a} for a in snapshot["prior_alerts"]]
    items.append({"pk": pk, "sk": "SUMMARY", **snapshot["activity_summary"]})
    items.append({"pk": pk, "sk": "META", "window": snapshot["window"], **snapshot["limits"]})
    return items
