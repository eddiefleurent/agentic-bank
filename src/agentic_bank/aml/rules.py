"""Deterministic AML rules: plain code, no model. Transactions in, alerts out.

An alert is a reason to look, not a verdict. Real banks tune rules to catch everything that might
matter, so most alerts turn out to be innocent. These rules work the same way: they match shapes
(amount just under a threshold, money in and out fast, many payers, a risky country, round chains)
and know nothing about why. The "why" lives in the KYC profile and memos, which a reviewer reads.

Rules (amounts in USD):
  R1 structuring     3+ cash deposits of 9,000-9,999.99 within 7 days
  R2 rapid movement  wire/p2p in of 5,000+, 80%+ sent out by wire/p2p within 48 hours
  R3 funnel          8+ different senders paying in within 7 days (10,000+ total), 70%+ out in 72h
  R4 high-risk wire  wire of 3,000+ to or from a high-risk jurisdiction (grouped over 30 days)
  R5 round chain     round-dollar (multiple of 500) in of 5,000+, 80%+ out, also round, in 72 hours
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from agentic_bank.aml.data import HIGH_RISK_COUNTRIES, Dataset, Txn

COOLDOWN = timedelta(days=30)  # one alert per customer and rule per 30 days
MOVE = ("wire", "p2p")


@dataclass(frozen=True)
class Alert:
    alert_id: str
    rule_id: str
    customer_id: str
    txn_ids: tuple[str, ...]
    created: str  # date of the last triggering transaction

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["txn_ids"] = list(self.txn_ids)
        return d


Hit = list[Txn]


def _t(t: Txn) -> datetime:
    return datetime.fromisoformat(t.ts)


def _out_after(txns: list[Txn], i: int, hours: int, *, round_only: bool = False) -> Hit:
    t0 = _t(txns[i])
    end = t0 + timedelta(hours=hours)
    out = []
    for t in txns[i + 1 :]:
        if _t(t) > end:
            break
        if t.direction == "out" and t.type in MOVE:
            if not round_only or (t.amount_cents >= 500_000 and t.amount_cents % 50_000 == 0):
                out.append(t)
    return out


def r1_structuring(txns: list[Txn]) -> list[Hit]:
    dep = [t for t in txns if t.type == "cash_deposit" and 900_000 <= t.amount_cents < 1_000_000]
    hits = []
    for i, d in enumerate(dep):
        group = [t for t in dep[i:] if _t(t) <= _t(d) + timedelta(days=7)]
        if len(group) >= 3:
            hits.append(group)
    return hits


def r2_rapid(txns: list[Txn]) -> list[Hit]:
    hits = []
    for i, t in enumerate(txns):
        if t.direction == "in" and t.type in MOVE and t.amount_cents >= 500_000:
            out = _out_after(txns, i, 48)
            if sum(o.amount_cents for o in out) >= 0.8 * t.amount_cents:
                hits.append([t, *out])
    return hits


def r3_funnel(txns: list[Txn]) -> list[Hit]:
    inn = [t for t in txns if t.direction == "in" and t.type in MOVE]
    hits = []
    for t in inn:
        win = [x for x in inn if _t(t) - timedelta(days=7) <= _t(x) <= _t(t)]
        senders = {x.counterparty_id for x in win}
        total = sum(x.amount_cents for x in win)
        if len(senders) >= 8 and total >= 1_000_000:
            i = txns.index(t)
            out = _out_after(txns, i, 72)
            if sum(o.amount_cents for o in out) >= 0.7 * total:
                hits.append([*win, *out])
    return hits


def r4_high_risk(txns: list[Txn], ds: Dataset) -> list[Hit]:
    risky = [
        t
        for t in txns
        if t.type == "wire"
        and t.amount_cents >= 300_000
        and t.counterparty_id in ds.counterparties
        and ds.counterparties[t.counterparty_id].country in HIGH_RISK_COUNTRIES
    ]
    return [
        [x for x in risky[i:] if _t(x) <= _t(t) + timedelta(days=30)] for i, t in enumerate(risky)
    ]


def r5_round_chain(txns: list[Txn]) -> list[Hit]:
    hits = []
    for i, t in enumerate(txns):
        if (
            t.direction == "in"
            and t.type in MOVE
            and t.amount_cents >= 500_000
            and t.amount_cents % 50_000 == 0
        ):
            out = _out_after(txns, i, 72, round_only=True)
            if sum(o.amount_cents for o in out) >= 0.8 * t.amount_cents:
                hits.append([t, *out])
    return hits


def run_rules(ds: Dataset) -> list[Alert]:
    """Scan every customer. Deterministic: same dataset, same alerts, same ids."""
    raw: list[tuple[str, str, Hit]] = []
    per_rule: list[tuple[str, Callable[[list[Txn]], list[Hit]]]] = [
        ("R1", r1_structuring),
        ("R2", r2_rapid),
        ("R3", r3_funnel),
        ("R5", r5_round_chain),
    ]
    for cid, txns in ds.by_customer.items():
        for rule_id, fn in per_rule:
            raw += [(rule_id, cid, h) for h in fn(txns)]
        raw += [("R4", cid, h) for h in r4_high_risk(txns, ds)]
    raw.sort(key=lambda r: (r[2][0].ts, r[1], r[0]))
    seen_txn: set[tuple[str, str, str]] = set()
    last: dict[tuple[str, str], datetime] = {}
    kept = []
    for rule_id, cid, hit in raw:
        ids = sorted({t.txn_id for t in hit})
        created = max(_t(t) for t in hit)
        if last.get((cid, rule_id)) and created - last[(cid, rule_id)] < COOLDOWN:
            continue
        if any((cid, rule_id, i) in seen_txn for i in ids):
            continue
        seen_txn.update((cid, rule_id, i) for i in ids)
        last[(cid, rule_id)] = created
        kept.append((created, cid, rule_id, tuple(ids)))
    kept.sort()
    return [
        Alert(f"A-{n:04d}", rule_id, cid, ids, created.date().isoformat())
        for n, (created, cid, rule_id, ids) in enumerate(kept, 1)
    ]
