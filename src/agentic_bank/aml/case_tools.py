"""Read-only tools over ONE case, the only data the triage agents can reach.

The seam: every tool calls a `CaseSource`. Today `SnapshotSource` reads the in-memory snapshot dict
from `snapshot.build_case_snapshot`. Slice 3 adds a source that queries the DynamoDB partition
`CASE#<alert_id>` (behind a Gateway, with `dynamodb:LeadingKeys` on the role), and the tools and
agents do not change.

Plain Python, no model and no framework: each function takes a source and returns a JSON-able
dict, so tests can call them directly. Memos come back as `memo_untrusted`: text typed by
customers and outsiders, which may contain instructions and must be read as data only.
"""

from collections import defaultdict
from typing import Any, Protocol

MAX_ROWS = 40  # rows any one tool call returns


class CaseSource(Protocol):
    """One case's sections. Each method returns a copy-safe JSON value for this case only."""

    def section(self, name: str) -> Any: ...


SECTIONS = (
    "alert",
    "window",
    "customer",
    "kyc",
    "flagged_transactions",
    "transactions",
    "linked_customers",
    "counterparties",
    "prior_alerts",
    "activity_summary",
    "limits",
)


class SnapshotSource:
    """The local stand-in for the DynamoDB case partition: a read-only view of one snapshot."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snap = snapshot
        self.reads: list[str] = []  # which sections were read, for tests and audit

    def section(self, name: str) -> Any:
        if name not in SECTIONS:
            raise KeyError(name)
        self.reads.append(name)
        return self._snap[name]


def untrusted(row: dict[str, Any]) -> dict[str, Any]:
    """A transaction row with its memo renamed, so every reader sees the memo is untrusted."""
    out = {k: v for k, v in row.items() if k != "memo"}
    if "memo" in row:
        out["memo_untrusted"] = row["memo"]
    return out


def _all_txns(src: CaseSource) -> list[dict[str, Any]]:
    flagged = [{**t, "flagged": True} for t in src.section("flagged_transactions")]
    return flagged + [{**t, "flagged": False} for t in src.section("transactions")]


def txn_ids(snapshot: dict[str, Any]) -> set[str]:
    """Every transaction id in a snapshot: the only ids an answer may cite."""
    return {t["txn_id"] for t in snapshot["flagged_transactions"] + snapshot["transactions"]}


# ---------------------------------------------------------------- flow tracer


def list_flagged(src: CaseSource) -> dict[str, Any]:
    alert = src.section("alert")
    return {
        "alert_id": alert["alert_id"],
        "rule_id": alert["rule_id"],
        "customer_id": alert["customer_id"],
        "flagged": [untrusted(t) for t in src.section("flagged_transactions")],
    }


def get_transaction(src: CaseSource, txn_id: str) -> dict[str, Any]:
    for t in _all_txns(src):
        if t["txn_id"] == txn_id:
            return untrusted(t)
    return {"error": f"no transaction {txn_id!r} in this case"}


def transactions_for_party(src: CaseSource, party_id: str, limit: int = 20) -> dict[str, Any]:
    """Rows on a customer's own account, or rows where a party is the counterparty."""
    limit = max(1, min(int(limit), MAX_ROWS))
    rows = [t for t in _all_txns(src) if party_id in (t["customer_id"], t["counterparty_id"])]
    rows.sort(key=lambda t: t["ts"])
    return {
        "party_id": party_id,
        "total_rows": len(rows),
        "rows": [untrusted(t) for t in rows[:limit]],
        "truncated": len(rows) > limit,
    }


def get_party(src: CaseSource, party_id: str) -> dict[str, Any]:
    """An external counterparty (X...) or a linked customer (C...) seen in this case."""
    for c in src.section("counterparties"):
        if c["counterparty_id"] == party_id:
            return {"kind": "external_counterparty", **c}
    for c in src.section("linked_customers"):
        if c["customer_id"] == party_id:
            return {"kind": "linked_customer", **c}
    if party_id == src.section("customer")["customer_id"]:
        return {"kind": "alert_customer", **src.section("customer")}
    return {"error": f"no party {party_id!r} in this case"}


def neighbours(src: CaseSource, customer_id: str) -> dict[str, Any]:
    """Who this customer's account sent money to or received it from, with totals and countries."""
    total: dict[str, dict[str, int]] = defaultdict(lambda: {"in_cents": 0, "out_cents": 0, "n": 0})
    for t in _all_txns(src):
        if t["customer_id"] != customer_id or t["counterparty_id"] is None:
            continue
        agg = total[t["counterparty_id"]]
        agg["in_cents" if t["direction"] == "in" else "out_cents"] += t["amount_cents"]
        agg["n"] += 1
    country = {c["counterparty_id"]: c["country"] for c in src.section("counterparties")}
    hop = {c["customer_id"]: c["hop"] for c in src.section("linked_customers")}
    out = [
        {"party_id": p, **v, "country": country.get(p), "hop": hop.get(p)}
        for p, v in sorted(total.items(), key=lambda kv: -(kv[1]["in_cents"] + kv[1]["out_cents"]))
    ]
    return {"customer_id": customer_id, "neighbours": out[:MAX_ROWS]}


# ---------------------------------------------------------------- KYC profiler


def kyc_profile(src: CaseSource) -> dict[str, Any]:
    return {"customer": src.section("customer"), "kyc": src.section("kyc")}


def activity_summary(src: CaseSource) -> dict[str, Any]:
    return {"window": src.section("window"), "activity_summary": src.section("activity_summary")}


def prior_alerts(src: CaseSource) -> dict[str, Any]:
    return {"prior_alerts": src.section("prior_alerts")}
