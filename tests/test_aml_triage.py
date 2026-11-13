"""AML slice 2 without AWS or a model: tools read one snapshot, bounds hold, code checks catch bad
answers, every failure escalates, and the eval maths is right."""

import copy
import inspect
from functools import cache
from types import SimpleNamespace
from typing import Any

import pytest

from agentic_bank.aml import agents, case_tools, triage
from agentic_bank.aml.agents import Bounds, ToolBudget
from agentic_bank.aml.case_tools import SnapshotSource
from agentic_bank.aml.snapshot import build_case_snapshot
from evals.aml import harness
from test_investigator import Scripted


@cache
def world():
    cases, ds, alerts = harness.load_cases()
    return cases, ds, alerts


def snap(label: str = "LABEL-007") -> dict[str, Any]:
    cases, ds, alerts = world()
    case = next(c for c in cases if c.id == label)
    return build_case_snapshot(case.alert, ds, alerts)


def answer(s: dict[str, Any], recommendation: str = "escalate") -> dict[str, Any]:
    flagged = [t["txn_id"] for t in s["flagged_transactions"]]
    return {
        "recommendation": recommendation,
        "typology": "structuring",
        "risk_factors": [{"statement": "Cash just under 10,000.", "txn_ids": flagged[:2]}],
        "mitigating_factors": [
            {"statement": "Occupation noted.", "txn_ids": [], "kyc_fields": ["occupation"]}
        ],
        "instruction_memo_txn_ids": [],
        "narrative": "Repeated cash deposits under the threshold.",
    }


# ---------------------------------------------------------------- tool layer


def test_tools_read_only_this_snapshot_and_mark_memos_untrusted():
    s = snap()
    src = SnapshotSource(s)
    ids = case_tools.txn_ids(s)
    flagged = case_tools.list_flagged(src)["flagged"]
    assert flagged and all("memo" not in t and "memo_untrusted" in t for t in flagged)
    party = s["alert"]["customer_id"]
    rows = case_tools.transactions_for_party(src, party, limit=999)["rows"]
    assert len(rows) <= case_tools.MAX_ROWS and {t["txn_id"] for t in rows} <= ids
    other = next(t for t in world()[1].transactions if t.txn_id not in ids)
    assert "error" in case_tools.get_transaction(src, other.txn_id)
    assert "error" in case_tools.get_party(src, "X999")
    with pytest.raises(KeyError):
        src.section("everything")
    assert set(src.reads) <= set(case_tools.SECTIONS)


def test_agents_reach_data_only_through_the_case_source():
    names = {t.tool_name for t in agents.flow_tools(SnapshotSource(snap()))} | {
        t.tool_name for t in agents.kyc_tools(SnapshotSource(snap()))
    }
    assert names == agents.FLOW_TOOLS | agents.KYC_TOOLS
    write_words = ("close", "file", "send", "contact", "update", "write", "put", "delete")
    assert not [n for n in agents.ALLOWED_TOOLS if set(n.split("_")) & set(write_words)]


# ---------------------------------------------------------------- bounds


def event(name: str) -> Any:
    return SimpleNamespace(tool_use={"name": name}, cancel_tool=False)


def test_budget_refuses_over_cap_and_off_allowlist():
    budget = ToolBudget(cap=1)
    first, second, rogue, final = (
        event("list_flagged"),
        event("neighbours"),
        event("close_alert"),
        event("Triage"),
    )
    for e in (first, second, rogue, final):
        budget.before_tool(e)
    assert first.cancel_tool is False and final.cancel_tool is False
    assert "budget" in second.cancel_tool and "not allowed" in rogue.cancel_tool
    assert budget.calls == ["list_flagged"] and budget.refused == ["neighbours", "close_alert"]


def scripted(supervisor, flow=None, kyc=None, **kw):
    models = {
        "supervisor": Scripted(supervisor, **kw),
        "flow_tracer": Scripted(flow or [("tools", [("list_flagged", {})]), ("text", "facts")]),
        "kyc_profiler": Scripted(kyc or [("tools", [("kyc_profile", {})]), ("text", "profile")]),
    }
    return models.__getitem__


DELEGATE = ("tools", [("flow_tracer", {"input": "trace"}), ("kyc_profiler", {"input": "kyc"})])


def run(supervisor, bounds=None, **kw):
    s = snap()
    return triage_run(s, scripted(supervisor, **kw), bounds)


def triage_run(s, model_for, bounds=None):
    return agents.triage(None, "m", s, bounds=bounds or Bounds(), model_for=model_for)


def test_agents_happy_path_is_checked_by_code():
    s = snap()
    result = triage_run(s, scripted([DELEGATE, ("tools", [("Triage", answer(s, "close"))])]))
    assert result.outcome.recommendation == "close" and result.outcome.failure is None
    assert {"flow_tracer", "list_flagged", "kyc_profiler", "kyc_profile"} <= set(result.tool_calls)
    assert result.usage["model_calls"] >= 4  # supervisor + both specialists share one budget


def test_agents_fabricated_id_escalates():
    s = snap()
    bad = answer(s, "close")
    bad["mitigating_factors"][0]["txn_ids"] = ["T999999"]
    result = triage_run(s, scripted([DELEGATE, ("tools", [("Triage", bad)])]))
    assert result.outcome.recommendation == "escalate"
    assert (result.outcome.failure or "").startswith("cited ids not in case")


@pytest.mark.parametrize(
    ("bounds", "kw"),
    [
        (Bounds(max_turns=4, max_tool_calls=50), {}),
        (Bounds(max_total_tokens=500), {"usage": 200}),
        (Bounds(wall_clock_seconds=0.2), {"delay": 0.3}),
    ],
    ids=["turns", "tokens", "wall_clock"],
)
def test_any_bound_hit_escalates(bounds, kw):
    result = run([DELEGATE] * 10, bounds=bounds, **kw)
    assert result.outcome.recommendation == "escalate" and result.outcome.failure


def test_tool_cap_refuses_extra_calls_then_still_answers():
    s = snap()
    many = ("tools", [("flow_tracer", {"input": str(i)}) for i in range(4)])
    result = triage_run(
        s, scripted([many, ("tools", [("Triage", answer(s))])]), Bounds(max_tool_calls=2)
    )
    assert result.refused_tools and len(result.tool_calls) == 2


def test_text_only_supervisor_never_closes():
    result = run([("text", "Looks fine, close it.")] * 5)
    assert result.outcome.recommendation == "escalate" and result.outcome.failure


# ---------------------------------------------------------------- code checks


def test_check_accepts_a_valid_answer():
    s = snap()
    assert triage.check(answer(s), s).failure is None
    assert triage.check(answer(s, "close"), s).recommendation == "close"


@pytest.mark.parametrize(
    ("mutate", "failure"),
    [
        (lambda a: a["risk_factors"][0]["txn_ids"].append("T999999"), "cited ids not in case"),
        (lambda a: a["instruction_memo_txn_ids"].append("T000000"), "cited ids not in case"),
        (lambda a: a.update(typology="terrorism"), "invalid output"),
        (lambda a: a.update(amount_cents=5), "invalid output"),
        (lambda a: a["risk_factors"][0].update(txn_ids=[]), "escalate without a cited transaction"),
    ],
)
def test_check_rejects_bad_escalations(mutate, failure):
    s = snap()
    a = answer(s)
    mutate(a)
    out = triage.check(a, s)
    assert out.recommendation == "escalate" and (out.failure or "").startswith(failure)


def test_close_without_a_cited_reason_escalates():
    s = snap()
    a = answer(s, "close")
    a["mitigating_factors"] = [{"statement": "Seems fine.", "txn_ids": [], "kyc_fields": []}]
    out = triage.check(a, s)
    assert out.recommendation == "escalate" and out.failure == "close without a cited reason"
    assert (
        out.advice and out.advice["recommendation"] == "close"
    )  # the model's advice is kept for the analyst


# ---------------------------------------------------------------- baseline failure path


class Bedrock:
    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error
        self.kwargs: dict[str, Any] = {}

    def converse(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.reply


def test_baseline_valid_answer_and_forced_tool():
    s = snap()
    content = [{"toolUse": {"name": triage.TOOL_NAME, "input": answer(s, "close")}}]
    usage = {"inputTokens": 100, "outputTokens": 20}
    bedrock = Bedrock({"output": {"message": {"content": content}}, "usage": usage})
    run_ = triage.run_baseline(bedrock, "m", s)
    assert run_.outcome.recommendation == "close" and run_.usage["model_calls"] == 1
    assert bedrock.kwargs["toolConfig"]["toolChoice"] == {"tool": {"name": triage.TOOL_NAME}}
    text = bedrock.kwargs["messages"][0]["content"][0]["text"]
    assert '"memo_untrusted"' in text and '"memo"' not in text


@pytest.mark.parametrize(
    "bedrock",
    [
        Bedrock(error=TimeoutError()),
        Bedrock({"output": {"message": {"content": [{"text": "close it"}]}}}),
        Bedrock({"output": {"message": {"content": [{"toolUse": {"name": "x", "input": {}}}]}}}),
    ],
    ids=["error", "text", "wrong_tool"],
)
def test_baseline_failures_escalate(bedrock):
    out = triage.run_baseline(bedrock, "m", snap()).outcome
    assert out.recommendation == "escalate" and out.failure


def test_reviewers_share_one_signature():
    a, b = inspect.signature(triage.run_baseline), inspect.signature(agents.run_agents)
    assert list(a.parameters) == list(b.parameters) == ["bedrock", "model_id", "snapshot"]
    assert triage.select("baseline") is triage.run_baseline
    assert triage.select("agents") is agents.run_agents


# ---------------------------------------------------------------- harness


def test_one_matching_alert_per_labelled_case():
    cases, _, _ = world()
    assert [c.verdict for c in cases].count("suspicious") == 25
    assert [c.verdict for c in cases].count("benign") == 25
    assert all(c.alert.rule_id == harness.RULE_OF[c.typology] for c in cases)
    assert sum(bool(c.injection_txn_ids) for c in cases) == 5


def row(id_, verdict, recommendation, failure=None, typology="structuring", predicted=None):
    advice = None if predicted is None else {"typology": predicted}
    return {
        "id": id_,
        "verdict": verdict,
        "typology": typology,
        "recommendation": recommendation,
        "failure": failure,
        "predicted_typology": predicted,
        "flagged_instruction_memos": [],
        "injection_txn_ids": [],
        "advice": advice,
        "latency_s": 1.0,
        "input_tokens": 100,
        "output_tokens": 10,
        "model_calls": 1,
        "tool_calls": 0,
        "cost_usd": 0.001,
    }


def test_scoring_maths():
    rows = [
        row("S1", "suspicious", "escalate", predicted="structuring"),
        row("S2", "suspicious", "escalate", failure="baseline error: X"),
        row("S3", "suspicious", "close", predicted="layering"),
        row("S4", "suspicious", "escalate", predicted="layering"),
        row("B1", "benign", "close", predicted="none"),
        row("B2", "benign", "escalate", failure="cited ids not in case: T1"),
    ]
    m = harness.summarize(rows)
    assert m["recall"] == 0.75 and m["recall_model_decided"] == 0.5
    assert m["missed_suspicious"] == ["S3"]
    assert m["false_escalation_rate"] == 0.5 and m["false_escalations"] == ["B2"]
    assert m["typology_accuracy"] == pytest.approx(1 / 3) and m["typology_n"] == 3
    assert m["answers"] == 5 and m["citation_failures"] == ["B2"]
    assert m["failures"] == {"baseline error": 1, "cited ids not in case": 1}
    assert m["by_typology"]["structuring"] == {
        "suspicious_escalated": "3/4",
        "benign_closed": "1/2",
    }


def test_fingerprint_is_stable_and_moves_with_what_matters(monkeypatch):
    a = harness.fingerprint("agents", "m")
    assert a == harness.fingerprint("agents", "m")
    assert a != harness.fingerprint("baseline", "m") != harness.fingerprint("agents", "m2")
    monkeypatch.setattr(triage, "BASELINE_PROMPT", triage.BASELINE_PROMPT + " ")
    assert harness.fingerprint("agents", "m") != a


def test_deepcopy_of_snapshot_is_untouched_by_tools():
    s = snap()
    before = copy.deepcopy(s)
    src = SnapshotSource(s)
    case_tools.transactions_for_party(src, s["alert"]["customer_id"])
    case_tools.neighbours(src, s["alert"]["customer_id"])
    triage.case_text(s)
    assert s == before
