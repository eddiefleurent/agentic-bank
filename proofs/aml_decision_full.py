"""Full-set decision-model pass, and the offline Reflex/Drex cascade.

Running this file rebuilds both markdown reports from the stored jsonl. No API calls.
"""

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from agentic_bank.aml.decision import (  # noqa: E402
    BAND_AUTO,
    BAND_CLOSE,
    BAND_UNSURE,
    DREX_MODEL,
    MODEL,
    model_slug,
)
from evals.aml import harness  # noqa: E402

RESULTS = ROOT / "evals" / "aml" / "results"
BUDGET_USD = 1.0
WORKERS = 4
BASELINE = {
    "base": RESULTS / "baseline.cases.jsonl",
    "deep": RESULTS / "two-stage" / "deep-baseline-r3.cases.jsonl",
}
GRAPH = {"deep": RESULTS / "two-stage" / "deep-agents-graph-r3.cases.jsonl"}
REFLEX_COLUMNS = (
    ("Reflex", "self"),
    ("Baseline", "baseline"),
    ("Graph", "graph"),
    ("Cascade", "cascade"),
)
DREX_COLUMNS = (
    ("Drex", "self"),
    ("Reflex", "reflex"),
    ("Baseline", "baseline"),
    ("Graph", "graph"),
    ("Cascade", "cascade"),
)


def cascade(row: dict) -> str:
    """Auto-escalate, likely-close, or else the stored graph (then baseline) recommendation."""
    band = row.get("band")
    if band == BAND_AUTO:
        return "escalate"
    if band != BAND_UNSURE:
        return "close"
    for key in ("_graph", "_base"):
        rec = (row.get(key) or {}).get("recommendation")
        if rec in ("close", "escalate"):
            return rec
    return "close"


def score(rows: list[dict], source: str) -> dict:
    """Recall, false escalations, agent reach, and $ per case for one column."""
    shaped = []
    dollars = 0.0
    reached = 0
    for row in rows:
        rec, cost, agent = _cell(row, source)
        if rec not in ("close", "escalate"):
            continue
        shaped.append(
            {
                "verdict": row["verdict"],
                "depth": row["depth"],
                "recommendation": rec,
                "failure": None,
            }
        )
        dollars += cost
        if agent:
            reached += 1
    n = len(shaped)
    return {
        "core": harness.core(shaped),
        "depth": harness.by_depth(shaped),
        "n": n,
        "usd": dollars / n if n else 0.0,
        "reach": reached / n if n else 0.0,
    }


def _from_p(p: object) -> str:
    if not isinstance(p, (int, float)):
        return "close"
    return "escalate" if p >= 0.5 else "close"


def _cell(row: dict, source: str) -> tuple[str | None, float, bool]:
    if source == "self":
        return _from_p(row.get("p_escalate")), row.get("cost_usd") or 0.0, False
    if source == "reflex":
        hit = row.get("_reflex")
        if not hit:
            return None, 0.0, False
        return _from_p(hit.get("p_escalate")), hit.get("cost_usd") or 0.0, False
    if source in ("baseline", "graph"):
        hit = row.get("_base" if source == "baseline" else "_graph")
        rec = hit.get("recommendation") if hit else None
        if rec not in ("close", "escalate"):
            return None, 0.0, False
        return rec, hit.get("cost_usd") or 0.0, True
    cost = row.get("cost_usd") or 0.0
    agent = row.get("band") == BAND_UNSURE
    if agent:
        extra = row.get("_graph") or row.get("_base")
        if extra:
            cost += extra.get("cost_usd") or 0.0
    return cascade(row), cost, agent


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _by_id(path: Path) -> dict[str, dict]:
    return {row["id"]: row for row in _jsonl(path)}


def _by_variant(path: Path) -> dict[str, dict[str, dict]]:
    found: dict[str, dict[str, dict]] = {}
    for row in _jsonl(path):
        found.setdefault(row["variant"], {})[row["id"]] = row
    return found


def _same(hit: dict | None, verdict: str, alert_id: str | None) -> dict | None:
    if not hit or not alert_id:
        return None
    if hit.get("verdict") != verdict or hit.get("alert_id") != alert_id:
        return None
    return hit


def attach(rows: list[dict], reflex: dict | None = None) -> list[dict]:
    baseline = {variant: _by_id(path) for variant, path in BASELINE.items()}
    graph = {variant: _by_id(path) for variant, path in GRAPH.items()}
    attached = []
    for row in rows:
        alert_id = row.get("alert_id")
        variant, case_id, verdict = row["variant"], row["id"], row["verdict"]
        attached.append(
            {
                **row,
                "_base": _same(baseline.get(variant, {}).get(case_id), verdict, alert_id),
                "_graph": _same(graph.get(variant, {}).get(case_id), verdict, alert_id),
                "_reflex": None
                if reflex is None
                else _same(reflex.get(variant, {}).get(case_id), verdict, alert_id),
            }
        )
    return attached


def _ci(block: dict, kind: str) -> str:
    if kind == "recall":
        k, n, p = block["escalated"], block["suspicious"], block["recall"]
    else:
        k, n, p = block["false_escalations"], block["benign"], block["false_escalation_rate"]
    if not n:
        return "n/a"
    lo, hi = harness.wilson(k, n)
    return f"{p:.0%} ({lo:.0%} to {hi:.0%})"


def _usd(value: float) -> str:
    return f"${value:.4f}" if value >= 0.0001 else f"${value:.6f}"


def metrics_table(rows: list[dict], columns: tuple[tuple[str, str], ...]) -> str:
    cols = [score(rows, source) for _, source in columns]

    def line(label: str, cell) -> list:
        return [label, *(cell(col) for col in cols)]

    body = [
        line("n", lambda col: col["n"]),
        line("Recall on suspicious", lambda col: _ci(col["core"], "recall")),
        line("Recall, shallow", lambda col: _ci(col["depth"]["shallow"], "recall")),
        line("Recall, deep", lambda col: _ci(col["depth"]["deep"], "recall")),
        line("Recall, blind-deep", lambda col: _ci(col["depth"]["blind_deep"], "recall")),
        line("False-escalation on benign", lambda col: _ci(col["core"], "fp")),
        line("False-escalation, shallow", lambda col: _ci(col["depth"]["shallow"], "fp")),
        line("False-escalation, deep", lambda col: _ci(col["depth"]["deep"], "fp")),
        line("Share reaching the expensive agent", lambda col: f"{col['reach']:.0%}"),
        line("$ per case", lambda col: _usd(col["usd"])),
    ]
    return harness.table(["Metric", *[name for name, _ in columns]], body)


def band_table(rows: list[dict]) -> str:
    bands = (
        ("auto-escalate", BAND_AUTO),
        ("unsure", BAND_UNSURE),
        ("likely-close", BAND_CLOSE),
        ("no opinion", None),
    )
    body = []
    for label, band in bands:
        group = [row for row in rows if row.get("band") == band]
        suspicious = sum(row["verdict"] == "suspicious" for row in group)
        benign = sum(row["verdict"] == "benign" for row in group)
        share = f"{suspicious / len(group):.0%}" if group else "n/a"
        body.append([label, suspicious, benign, share])
    return harness.table(["Band", "Suspicious", "Benign", "Truly suspicious"], body)


def render(
    rows: list[dict],
    spent: float,
    aborted: bool,
    *,
    model: str = MODEL,
    latencies: list[float] | None = None,
) -> str:
    drex = model == DREX_MODEL
    columns = DREX_COLUMNS if drex else REFLEX_COLUMNS
    reflex = _by_variant(RESULTS / "reflex" / "reflex.cases.jsonl") if drex else None
    rows = attach(rows, reflex)
    deep = [row for row in rows if row["variant"] == "deep"]
    misses = [
        row for row in rows if row["verdict"] == "suspicious" and row.get("band") == BAND_CLOSE
    ]
    misses.sort(key=lambda row: (row["variant"], row["id"]))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["variant"]] = counts.get(row["variant"], 0) + 1
    who = "Drex" if drex else "Reflex"
    cascade_name = "Cascade-with-Drex" if drex else "Cascade"
    stop = " Run aborted: reported cost exceeded $1." if aborted else ""
    timed = list(latencies or [])
    latency = ""
    if drex and timed:
        mean = sum(timed) / len(timed)
        latency = (
            f" Latency mean {mean:.2f}s, p50 {harness.percentile(timed, 0.5):.2f}s"
            f" ({len(timed)} calls)."
        )
    deep_blurb = (
        "Drex, stored Reflex, baseline, and graph each have a row here, so the columns share a "
        "case set. Cascade is cascade-with-Drex."
        if drex
        else "Baseline, graph, and Reflex each have a row here, so the columns share a case set."
    )
    full_lead = (
        "Drex, stored Reflex, and the Drex cascade cover every case above. "
        if drex
        else "Reflex and the cascade cover every case above. "
    )
    miss_lines = [
        f"- {row['variant']}/{row['id']} {row['depth']} p={row['p_escalate']}" for row in misses
    ] or ["- none"]

    def used(kind: str, variant: str, path: Path, key: str) -> str:
        n = sum(1 for row in rows if row["variant"] == variant and row.get(key))
        status = f"used, {n} cases" if n else "not used, 0 alert ids match"
        return f"- {variant} {kind}: `{path.relative_to(ROOT)}` ({status})"

    stored = [used("baseline", variant, path, "_base") for variant, path in BASELINE.items()]
    stored += [used("graph", variant, path, "_graph") for variant, path in GRAPH.items()]
    if drex:
        n_reflex = sum(1 for row in rows if row.get("_reflex"))
        reflex_path = RESULTS / "reflex" / "reflex.cases.jsonl"
        stored.append(f"- reflex: `{reflex_path.relative_to(ROOT)}` (used, {n_reflex} cases)")
    base_joined = sum(1 for row in rows if row["variant"] == "base" and row.get("_base"))
    return "\n".join(
        [
            f"# {who} on the full AML case set",
            "",
            f"One OpenRouter pass of `{model}` on tokenized snapshots. Not an eval campaign. "
            f"No Bedrock calls.{stop}",
            "",
            f"{len(rows)} cases (base {counts.get('base', 0)}, deep {counts.get('deep', 0)}, "
            f"blind-deep {counts.get('deep2', 0)}). Reported cost ${spent:.6f}. "
            f"p≥0.5 counts as {who} escalate. {cascade_name}: auto-escalate → escalate; "
            "likely-close → close; unsure → stored graph recommendation, else stored baseline. "
            "A stored row is used only when its alert id and verdict match the current case."
            f"{latency}",
            "",
            "## Same 66 deep cases",
            "",
            deep_blurb,
            "",
            metrics_table(deep, columns),
            "",
            "## Full set",
            "",
            full_lead + f"Stored base baseline joined {base_joined} current base cases "
            "(label ids overlap the old file, alert ids do not). "
            "Graph joined the deep cases only. Blind-deep has no stored graph or baseline, "
            "so an unsure band there is not an escalation.",
            "",
            metrics_table(rows, columns),
            "",
            "## Bands",
            "",
            "Truly suspicious is the share of that band whose label is suspicious.",
            "",
            band_table(rows),
            "",
            "## Suspicious cases in the likely-close band",
            "",
            "The cascade sends these to the close queue. A person still reads them.",
            "",
            *miss_lines,
            "",
            "## Stored runs",
            "",
            *stored,
            "",
            "`evals/aml/results/2b/agents-graph-r*.cases.jsonl` is an older fingerprint of these "
            "66 deep cases and was not mixed in. Blind-deep has no stored graph or baseline; "
            "an unsure band there adds no agent cost and is not an escalation.",
            "",
        ]
    )


def render_stored(model: str = MODEL) -> str:
    slug = model_slug(model)
    rows = _jsonl(RESULTS / slug / f"{slug}.cases.jsonl")
    spent = sum(row.get("cost_usd") or 0.0 for row in rows)
    latencies = [row["latency_s"] for row in rows if "latency_s" in row]
    return render(rows, spent, False, model=model, latencies=latencies)


def write_summary(rows: list[dict], spent: float, aborted: bool, *, model: str, latencies) -> str:
    text = render(rows, spent, aborted, model=model, latencies=list(latencies))
    out = RESULTS / model_slug(model)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{model_slug(model)}.md").write_text(text)
    return text


def redact(text: str) -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        text = text.replace(key, "<key>")
    text = re.sub(r"(?i)bearer\s+\S+|sk-or-[A-Za-z0-9_-]+", "<key>", text)
    return re.sub(r"[0-9]{12}", "<acct>", text)


def run_all(model: str, load_cases, run_cases) -> int:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set; no decision call will be made.", file=sys.stderr)
        return 1
    loaded = load_cases()
    print(f"{model_slug(model)} cases: {len(loaded)} model {model}", file=sys.stderr)
    opinions, spent, aborted, latencies, fatal = run_cases(
        [snap for _, snap in loaded],
        model,
        workers=WORKERS,
        budget=BUDGET_USD,
        abort_on_4xx=True,
        progress=True,
    )
    if fatal is not None:
        print(f"HTTP {fatal.status}\n{redact(fatal.body)}", file=sys.stderr)
        return 1
    case_rows = []
    for (ref, _), opinion, latency in zip(loaded, opinions, latencies, strict=True):
        row = {
            "id": ref.case_id,
            "variant": ref.variant,
            "verdict": ref.verdict,
            "depth": ref.depth,
            "alert_id": ref.alert_id,
            "p_escalate": None if opinion is None else opinion.p_escalate,
            "p_suspicion": None if opinion is None else opinion.p_suspicion,
            "band": None if opinion is None else opinion.band,
            "cost_usd": None if opinion is None else opinion.cost_usd,
        }
        if isinstance(latency, (int, float)):
            row["latency_s"] = round(latency, 3)
        case_rows.append(row)
    slug = model_slug(model)
    out = RESULTS / slug
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{slug}.cases.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in case_rows) + "\n")
    timed = [item for item in latencies if isinstance(item, (int, float))]
    print(write_summary(case_rows, spent, aborted, model=model, latencies=timed))
    print(f"Wrote {path.relative_to(ROOT)}")
    return 0


def main() -> int:
    for model in (MODEL, DREX_MODEL):
        slug = model_slug(model)
        text = render_stored(model)
        path = RESULTS / slug / f"{slug}.md"
        path.write_text(text)
        print(f"Wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
