"""The "deep" AML variant: the standard 50 cases plus 16 whose decisive evidence sits outside the
case snapshot, more than 2 hops away or more than 30 days before the alert.

These cases were DESIGNED to need exploration. They test whether an agent that can walk the full
transaction graph finds what a fixed evidence pack leaves out. They say nothing about how often
such cases occur in a real bank. The standard data (`data.generate()` with no extra) is unchanged,
so slice 2 results stay reproducible.

Every label in this variant carries `depth`: `shallow` (a standard case), `hops` (the decisive
evidence is 3 or more hops from the alert customer) or `window` (it is more than 30 days before
the alert's first transaction).

  suspicious  layering_chain_4hop (3, hops)     round chain through 4 accounts; the exit to a
                                                high-risk country is at hop 3
              funnel_source_hop3 (3, hops)      8 depositors fed by one person, who was fed by a
                                                low-income account that received a high-risk wire
              repeated_one_off (2, window)      a "one-off house sale" pass-through, done twice
                                                before, outside the window
  benign      explained_by_history (3, window)  a large in-and-out whose references are explained
                                                by an estate letter and a builder's contract 60+
                                                days earlier
              family_chain_4hop (3, hops)       a round chain to a title company; the grandparent's
                                                documented gift note is at hop 3
              tie_in_history (2, window)        wires to a high-risk country notary, explained by
                                                an inheritance transfer 70 days earlier
"""

from datetime import timedelta
from typing import Any

from agentic_bank.aml import data
from agentic_bank.aml.data import HIGH_RISK_COUNTRIES, SAFE_FOREIGN, Builder, Txn, usd

SEED = data.SEED
DEEP_CASES = 16


def fresh_cp(b: Builder, kind: str, country: str, name: str) -> str:
    """An unused, non-high-risk counterparty, reserved for this case and renamed. High-risk
    counterparties are shared across cases, so renaming one would change other cases."""
    ok = [
        c.counterparty_id
        for c in b.cps.values()
        if c.kind == kind
        and c.counterparty_id not in b.reserved
        and c.country not in HIGH_RISK_COUNTRIES
    ]
    cid = b.rng.choice(ok)
    b.reserved.add(cid)
    b.cps[cid].country, b.cps[cid].name = country, name
    return cid


def _tag(b: Builder, depth: str, decisive: list[Txn]) -> None:
    """Mark the case just planted: its depth and the rows that decide it (outside the snapshot)."""
    b.cases[-1]["depth"] = depth
    b.cases[-1]["decisive"] = decisive


# ---- suspicious -------------------------------------------------------------------------------


def s_layering_4hop(b: Builder, k: int) -> None:
    members = [b.take() for _ in range(4)]
    agency = ["Selbrook", "Ravenholm", "Dunmere"][k]
    b.profile(
        members[0],
        "self_employed",
        f"Owner, {agency} Import Agency",
        6000,
        volume=50_000,
        opened=data._opened(b, 60),
        notes="Import agency; pays freight partners for clients' shipments.",
    )
    for m in members[1:3]:
        b.profile(
            m,
            "self_employed",
            "Freight forwarding agent",
            4000,
            volume=20_000,
            opened=data._opened(b, 18),
        )
    b.profile(members[3], "student", "Student", 1200, opened=data._opened(b, 1))
    x = [30_000, 40_000, 25_000][k]
    src = fresh_cp(b, "business", b.rng.choice(SAFE_FOREIGN), f"{agency} Clients Ltd")
    dst = b.pick("business", country=b.rng.choice(HIGH_RISK_COUNTRIES))
    memos = [
        "client remittance",
        "freight partner payment",
        "freight settlement",
        "settlement",
        "goods",
    ]
    rows = b.chain(members, b.rng.randint(20, 70), x, src, dst, memos)
    b.case(
        "suspicious",
        "layering",
        "layering_chain_4hop",
        members,
        rows,
        ["R5"],
        f"${x:,} passes through 4 accounts in round amounts within days and leaves from a student "
        "account opened a month ago to a high-risk jurisdiction. The exit is 3 hops from the "
        "alerted import agency.",
    )
    _tag(b, "hops", rows[-1:])


def s_funnel_source(b: Builder, k: int) -> None:
    source, feeder, target = b.take(), b.take(), b.take()
    deps = [b.take() for _ in range(8)]
    b.profile(source, "employee", "Warehouse picker", 2400, opened=data._opened(b, 3))
    b.profile(feeder, "employee", "Cashier", 2600, opened=data._opened(b, 10))
    club = ["Norvale Rowing Club", "Penmere Chess Society", "Halbrook Cycling Club"][k]
    b.profile(
        target,
        "employee",
        "Bus driver",
        3800,
        opened=data._opened(b, 40),
        notes=f"Says he is treasurer of {club}.",
    )
    for d in deps:
        b.profile(
            d,
            "employee",
            b.rng.choice(["Cleaner", "Barista", "Retail clerk"]),
            2200,
            opened=data._opened(b, 20),
        )
    day = b.rng.randint(15, 60)
    hr = b.pick("business", country=b.rng.choice(HIGH_RISK_COUNTRIES))
    each = b.rng.randint(2600, 3000)
    total = each * 8 + 900
    t0 = b.when(day, 9)
    rows = [b.add(source, t0, "wire", "in", usd(total + 400), hr, "consulting")]
    rows.append(b.add(source, t0 + timedelta(hours=20), "p2p", "out", usd(total), feeder, "loan"))
    t = t0 + timedelta(hours=30)
    for d in deps:
        t += timedelta(hours=b.rng.randint(2, 8))
        rows.append(b.add(feeder, t, "p2p", "out", usd(each + 100), d, "gift"))
    for d in deps:
        t += timedelta(hours=b.rng.randint(3, 10))
        rows.append(
            b.add(d, t, "p2p", "out", usd(each), target, b.rng.choice(["dues", "club dues"]))
        )
    out_cp = fresh_cp(b, "business", b.rng.choice(SAFE_FOREIGN), f"{club.split()[0]} Marine Supply")
    rows.append(
        b.add(
            target,
            t + timedelta(hours=20),
            "wire",
            "out",
            usd(int(each * 8 * 0.93)),
            out_cp,
            "equipment purchase",
        )
    )
    b.case(
        "suspicious",
        "funnel_account",
        "funnel_source_hop3",
        [target, *deps, feeder, source],
        rows,
        ["R3"],
        "Eight 'club dues' payers were each paid the same amount the day before by one cashier, "
        "who was paid by a new low-income account that had just received a wire from a high-risk "
        "jurisdiction. The source is 3 hops from the alerted treasurer.",
    )
    _tag(b, "hops", rows[:1])


def s_repeated_one_off(b: Builder, k: int) -> None:
    cid = b.take()
    b.profile(
        cid, "employee", ["Retail clerk", "Delivery driver"][k], 3000, opened=data._opened(b, 30)
    )
    src = fresh_cp(
        b, "business", b.rng.choice(SAFE_FOREIGN), ["Aldmere Property Sales", "Briscott Realty"][k]
    )
    rows: list[Txn] = []
    for n, day in enumerate((6, 22, 80)):
        amt = b.rng.randint(30_000, 40_000) + b.rng.randint(1, 99) / 100
        share = 0.75 if n < 2 else 0.92  # the earlier ones stay under the R2 threshold
        outs = [
            (
                "wire",
                b.pick("person", country=b.rng.choice(SAFE_FOREIGN)),
                usd(amt * share * 0.6),
                "family support",
                b.rng.randint(6, 20),
            ),
            (
                "wire",
                b.pick("person", country=b.rng.choice(SAFE_FOREIGN)),
                usd(amt * share * 0.4),
                "family support",
                b.rng.randint(21, 30),
            ),
        ]
        rows += b.pass_through(cid, day, usd(amt), src, "proceeds sale of family house", outs)
    b.case(
        "suspicious",
        "rapid_movement",
        "repeated_one_off",
        [cid],
        rows,
        ["R2"],
        "A 'sale of the family house' passes straight abroad. The same one-off sale, from the "
        "same payer, happened twice before, 58 and 74 days earlier: outside the 30-day window.",
    )
    _tag(b, "window", rows[:6])


# ---- benign -----------------------------------------------------------------------------------


def b_explained_by_history(b: Builder, k: int) -> None:
    cid = b.take()
    b.profile(
        cid, "employee", ["Nurse", "Electrician", "Accountant"][k], 5200, opened=data._opened(b, 80)
    )
    surname = b._surname()
    law = fresh_cp(b, "business", "US", f"{b._surname()} & Tor Solicitors")
    builder = fresh_cp(b, "business", "US", f"{b._surname()} Builders")
    ref, contract = b.rng.randint(7000, 7999), b.rng.randint(5000, 5999)
    final = b.rng.randint(40_000, 55_000)
    balance = int(final * 0.9)
    early = b.rng.randint(3, 12)
    rows = [
        b.add(
            cid,
            b.when(early),
            "wire",
            "in",
            usd(1_200),
            law,
            f"Estate of R. {surname}: interim distribution, ref {ref}. Final distribution about "
            f"${final:,} on probate close.",
        ),
        b.add(
            cid,
            b.when(early + 2),
            "wire",
            "out",
            usd(5_000),
            builder,
            f"deposit renovation contract {contract}; balance ${balance:,} due on completion",
        ),
    ]
    late = b.rng.randint(76, 84)
    t0 = b.when(late, 9)
    rows.append(b.add(cid, t0, "wire", "in", usd(final), law, f"ref {ref}"))
    rows.append(
        b.add(
            cid,
            t0 + timedelta(hours=b.rng.randint(8, 30)),
            "wire",
            "out",
            usd(balance),
            builder,
            f"payment {contract}",
        )
    )
    b.case(
        "benign",
        "rapid_movement",
        "explained_by_history",
        [cid],
        rows,
        ["R2"],
        "An estate distribution goes to a builder. Both references are explained by the "
        "solicitor's interim payment and the builder's contract deposit 60+ days earlier.",
    )
    _tag(b, "window", rows[:2])


def b_family_chain(b: Builder, k: int) -> None:
    gp, parent, aunt, student = (b.take() for _ in range(4))
    street = ["14 Elm Row", "3 Quarry Lane", "27 Mill Court"][k]
    b.profile(
        gp,
        "retiree",
        "Retired",
        4000,
        opened=data._opened(b, 100),
        notes=f"Gifting a house deposit to grandchild (account {student}) through family "
        f"accounts; purchase of {street}. Gift letter on file at the branch.",
    )
    b.profile(parent, "employee", "Pharmacist", 6500, opened=data._opened(b, 90))
    b.profile(aunt, "employee", "School teacher", 4800, opened=data._opened(b, 70))
    b.profile(student, "student", "Student", 1300, opened=data._opened(b, 12))
    x = [30_000, 25_000, 40_000][k]
    rows = b.chain(
        [gp, parent, aunt, student],
        b.rng.randint(20, 70),
        x,
        fresh_cp(b, "business", "US", f"{b._surname()} Savings Bank"),
        fresh_cp(b, "business", "US", f"{b._surname()} Title and Escrow"),
        ["CD maturity", "family", "family transfer", "for the house", "transfer"],
    )
    b.case(
        "benign",
        "layering",
        "family_chain_4hop",
        [student, aunt, parent, gp],
        rows,
        ["R5"],
        "A grandparent's documented house-deposit gift passes through family accounts to a title "
        "company. The gift note is on the grandparent's profile, 3 hops from the alerted student.",
    )
    _tag(b, "hops", rows[:1])


def b_tie_in_history(b: Builder, k: int) -> None:
    cid = b.take()
    b.profile(cid, "employee", ["Accountant", "Pharmacist"][k], 6000, opened=data._opened(b, 50))
    country = b.rng.choice(HIGH_RISK_COUNTRIES)
    notary = fresh_cp(b, "business", country, f"{country} Notary Office {b.rng.randint(10, 99)}")
    ref = b.rng.randint(2000, 2999)
    rows = [
        b.add(
            cid,
            b.when(b.rng.randint(3, 8)),
            "wire",
            "in",
            usd(2_800),
            notary,
            f"inheritance transfer 1 of 3, estate of father, ref {ref}; estate tax and notary "
            "fees payable on settlement",
        ),
    ]
    late = b.rng.randint(74, 80)
    rows.append(b.add(cid, b.when(late), "wire", "out", usd(4_200 + k * 300), notary, f"ref {ref}"))
    rows.append(b.add(cid, b.when(late + 6), "wire", "out", usd(3_600), notary, f"ref {ref}"))
    b.case(
        "benign",
        "high_risk_jurisdiction",
        "tie_in_history",
        [cid],
        rows,
        ["R4"],
        f"Wires to a notary in {country} pay estate tax and fees, as the inheritance transfer from "
        "the same notary 70 days earlier said they would.",
    )
    _tag(b, "window", rows[:1])


PLAN = [
    (s_layering_4hop, 3),
    (s_funnel_source, 3),
    (s_repeated_one_off, 2),
    (b_explained_by_history, 3),
    (b_family_chain, 3),
    (b_tie_in_history, 2),
]


def plant(b: Builder) -> None:
    for c in b.cases:
        c["depth"] = "shallow"
    for fn, n in PLAN:
        for k in range(n):
            fn(b, k)


def generate(seed: int = SEED) -> tuple[data.Dataset, dict[str, Any]]:
    return data.generate(seed, extra=plant)


def main() -> None:
    """Write evals/aml/deep/labels.json and manifest.json (the dataset itself is not committed)."""
    import json
    from pathlib import Path

    from agentic_bank.aml.rules import run_rules

    ds, labels = generate()
    alerts = run_rules(ds)
    manifest = {
        "seed": SEED,
        "variant": "deep",
        "transactions": len(ds.transactions),
        "alerts": len(alerts),
        "cases": len(labels["cases"]),
        "cases_by_depth": {
            d: sum(c["depth"] == d for c in labels["cases"]) for d in ("shallow", "hops", "window")
        },
        "dataset_sha256": data.dataset_hash(ds),
    }
    out = Path("evals/aml/deep")
    out.mkdir(parents=True, exist_ok=True)
    (out / "labels.json").write_text(json.dumps(labels, indent=1, sort_keys=True) + "\n")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
