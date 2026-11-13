"""Read-only access to the WHOLE dataset for one alert: the agents explore the graph live.

Slice 2 gave agents a pre-built snapshot (2 hops, 30 days, at most 150 rows), so code had already
chosen the evidence. Here the agents start from the alert alone and can read any customer, any day
of the 90, at any hop depth. The controls move from "what is in the snapshot" to:

- read-only: every method builds new dicts; nothing in the dataset can change;
- paginated: at most PAGE_SIZE rows per call, so no single call dumps the graph;
- capped: at most MAX_ROWS_READ rows per alert, then reads are refused;
- audited: every call is logged with the alert id, the arguments and how many rows it returned,
  and `read_ids` holds every transaction id the agents were shown. The code checks accept only
  citations from that set: an agent can cite only what it actually read.

Slice 3 would put this behind a Gateway over a graph or transaction store; the agents would not
change. Card rows are left out (they are counted in `activity`), as in the snapshot.
"""

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from agentic_bank.aml.data import Dataset, Txn
from agentic_bank.aml.rules import Alert

PAGE_SIZE = 50
MAX_ROWS_READ = 2_000  # per alert, across all calls


def _row(t: Txn) -> dict[str, Any]:
    return {
        "txn_id": t.txn_id,
        "ts": t.ts,
        "customer_id": t.customer_id,
        "type": t.type,
        "direction": t.direction,
        "amount_cents": t.amount_cents,
        "counterparty_id": t.counterparty_id,
        "memo_untrusted": t.memo,
    }


class GraphSource:
    def __init__(self, ds: Dataset, alerts: list[Alert], alert_id: str) -> None:
        self._ds = ds
        self._alerts = alerts
        self.alert = next(a for a in alerts if a.alert_id == alert_id)
        self.log: list[dict[str, Any]] = []
        self.read_ids: set[str] = set(self.alert.txn_ids)
        self.rows_read = 0
        self._by_cp: dict[str, list[Txn]] | None = None

    # ---- bookkeeping ----------------------------------------------------------------------
    def _audit(self, tool: str, args: dict[str, Any], out: dict[str, Any], rows: list) -> dict:
        self.log.append(
            {"alert_id": self.alert.alert_id, "tool": tool, "args": args, "rows": len(rows)}
        )
        return out

    def _page(self, tool: str, args: dict[str, Any], txns: list[Txn], page: int) -> dict:
        if self.rows_read >= MAX_ROWS_READ:
            return self._audit(tool, args, {"error": "read cap for this alert reached"}, [])
        page = max(0, int(page))
        chunk = txns[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        self.rows_read += len(chunk)
        self.read_ids.update(t.txn_id for t in chunk)
        out = {
            "rows": [_row(t) for t in chunk],
            "page": page,
            "total_rows": len(txns),
            "next_page": page + 1 if (page + 1) * PAGE_SIZE < len(txns) else None,
        }
        return self._audit(tool, args, out, chunk)

    def _by_counterparty(self) -> dict[str, list[Txn]]:
        if self._by_cp is None:
            index: dict[str, list[Txn]] = defaultdict(list)
            for t in self._ds.transactions:
                if t.counterparty_id and t.type != "card":
                    index[t.counterparty_id].append(t)
            self._by_cp = dict(index)
        return self._by_cp

    def _party_txns(self, party_id: str, start: str | None, end: str | None) -> list[Txn]:
        if party_id in self._ds.customers:
            rows = [t for t in self._ds.by_customer[party_id] if t.type != "card"]
        else:
            rows = self._by_counterparty().get(party_id, [])
        lo, hi = start or "0000", (end or "9999") + "￿"  # dates compare as ISO strings
        return [t for t in rows if lo <= t.ts <= hi]

    # ---- reads ----------------------------------------------------------------------------
    def get_alert(self) -> dict[str, Any]:
        rows = [self._ds.by_id[i] for i in self.alert.txn_ids]
        out = {**self.alert.to_dict(), "flagged": [_row(t) for t in rows]}
        return self._audit("get_alert", {}, out, rows)

    def transactions(
        self, party_id: str, start: str | None = None, end: str | None = None, page: int = 0
    ) -> dict[str, Any]:
        args = {"party_id": party_id, "start": start, "end": end, "page": page}
        return self._page("transactions", args, self._party_txns(party_id, start, end), page)

    def transaction(self, txn_id: str) -> dict[str, Any]:
        t = self._ds.by_id.get(txn_id)
        if t is None or t.type == "card":
            return self._audit("transaction", {"txn_id": txn_id}, {"error": "not found"}, [])
        self.read_ids.add(t.txn_id)
        self.rows_read += 1
        return self._audit("transaction", {"txn_id": txn_id}, _row(t), [t])

    def follow(self, txn_id: str, direction: str = "forward", hours: int = 72) -> dict[str, Any]:
        """One hop along the money. forward: where the money in this transaction went next;
        backward: where it came from. Between two customers it crosses to the other account:
        forward from a p2p OUT to customer B lists B's moves out after it arrived; backward from
        a p2p IN from customer B lists B's moves in before it left. Otherwise it stays on the
        same account (forward from an IN: moves out after it; backward from an OUT: moves in
        before it). External counterparties have no account here, so the trail ends there."""
        args = {"txn_id": txn_id, "direction": direction, "hours": hours}
        t = self._ds.by_id.get(txn_id)
        if t is None or t.type == "card":
            return self._audit("follow", args, {"error": "not found"}, [])
        self.read_ids.add(t.txn_id)
        hours = max(1, min(int(hours), 24 * 14))
        at = datetime.fromisoformat(t.ts)
        forward = direction != "backward"
        crosses = t.counterparty_id in self._ds.customers and (
            (forward and t.direction == "out") or (not forward and t.direction == "in")
        )
        account = t.counterparty_id if crosses else t.customer_id
        assert account is not None
        lo, hi = (at, at + timedelta(hours=hours)) if forward else (at - timedelta(hours=hours), at)
        want = "out" if forward else "in"
        rows = [
            x
            for x in self._ds.by_customer[account]
            if x.txn_id != t.txn_id
            and not (x.counterparty_id == t.customer_id and x.ts == t.ts)  # the mirror row
            and x.type in ("wire", "p2p", "cash_deposit")
            and x.direction == want
            and lo <= datetime.fromisoformat(x.ts) <= hi
        ]
        out = self._page("follow", args, rows, 0)
        if "rows" in out:
            out["account"] = account
        return out

    def party(self, party_id: str) -> dict[str, Any]:
        """A customer's full KYC profile, or an external counterparty's name, kind and country."""
        if party_id in self._ds.customers:
            out = {"kind": "customer", **asdict(self._ds.customers[party_id])}
        elif party_id in self._ds.counterparties:
            out = {"kind": "external_counterparty", **asdict(self._ds.counterparties[party_id])}
        else:
            out = {"error": "not found"}
        return self._audit("party", {"party_id": party_id}, out, [])

    def neighbours(
        self, party_id: str, start: str | None = None, end: str | None = None, page: int = 0
    ) -> dict[str, Any]:
        """Who a party paid or was paid by, with totals, largest first (paginated)."""
        args = {"party_id": party_id, "start": start, "end": end, "page": page}
        total: dict[str, dict[str, int]] = defaultdict(
            lambda: {"in_cents": 0, "out_cents": 0, "n": 0}
        )
        for t in self._party_txns(party_id, start, end):
            other = t.counterparty_id if t.customer_id == party_id else t.customer_id
            if other is None:
                continue
            agg = total[other]
            # direction from party_id's side
            incoming = (t.direction == "in") == (t.customer_id == party_id)
            agg["in_cents" if incoming else "out_cents"] += t.amount_cents
            agg["n"] += 1
        ranked = sorted(total.items(), key=lambda kv: -(kv[1]["in_cents"] + kv[1]["out_cents"]))
        page = max(0, int(page))
        chunk = ranked[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        cps = self._ds.counterparties
        out = {
            "party_id": party_id,
            "neighbours": [
                {
                    "party_id": p,
                    **v,
                    "kind": "customer" if p in self._ds.customers else "external",
                    "country": cps[p].country if p in cps else None,
                }
                for p, v in chunk
            ],
            "next_page": page + 1 if (page + 1) * PAGE_SIZE < len(ranked) else None,
        }
        return self._audit("neighbours", args, out, chunk)

    def activity(self, customer_id: str) -> dict[str, Any]:
        mine = self._ds.by_customer.get(customer_id)
        if mine is None:
            return self._audit("activity", {"customer_id": customer_id}, {"error": "not found"}, [])
        sums: dict[str, int] = defaultdict(int)
        for t in mine:
            sums[f"{t.type}_{t.direction}_cents"] += t.amount_cents
        out = {
            "customer_id": customer_id,
            "transactions_90d": len(mine),
            "by_type": dict(sorted(Counter(t.type for t in mine).items())),
            "totals": dict(sorted(sums.items())),
        }
        return self._audit("activity", {"customer_id": customer_id}, out, [])

    def prior_alerts(self, customer_id: str) -> dict[str, Any]:
        prior = [
            a.to_dict()
            for a in self._alerts
            if a.customer_id == customer_id and a.created < self.alert.created
        ]
        return self._audit(
            "prior_alerts", {"customer_id": customer_id}, {"prior_alerts": prior}, []
        )
