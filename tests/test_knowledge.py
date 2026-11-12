"""Policy assistant: the audience comes from identity, weak or ungrounded answers become "I don't
know", and citations name only passages the model was given. Retrieval is stubbed here; the real
embeddings run in scripts/rag_eval.py."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_bank.knowledge import policy, service
from agentic_bank.knowledge.corpus import load_corpus, split_sections
from agentic_bank.knowledge.policy import CUSTOMER, STAFF, Passage
from agentic_bank.knowledge.retrievers import (
    DIMENSIONS,
    InMemoryRetriever,
    KnowledgeBaseRetriever,
)
from agentic_bank.tools import handler as tools

APP = Path(__file__).resolve().parents[1] / "infra/app"


def passage(doc_id="fee-schedule", section="Wire transfer fees", audience="customer", score=0.6):
    return Passage(
        doc_id, doc_id.title(), section, "2026-01-01", audience, f"{section} text", score
    )


class StubRetriever:
    def __init__(self, hits):
        self.hits = hits
        self.audiences = None

    def retrieve(self, query, audiences, k):
        self.audiences = tuple(audiences)
        return self.hits


class StubModel:
    """A stub model that returns `reply` and records which passages it was shown."""

    def __init__(self, reply: dict | str):
        self.reply = reply
        self.calls = []

    def __call__(self, question, passages):
        self.calls.append(passages)
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def grounder(passed=True):
    return lambda source, question, answer: {"grounding": 0.9, "relevance": 0.9, "passed": passed}


# --- the audience is decided by identity, never by the request ---


def test_gateway_tool_searches_customer_documents_even_if_the_agent_asks_for_internal(
    monkeypatch,
):
    stub = StubRetriever([passage()])
    monkeypatch.setattr(service, "_retriever", lambda: stub)
    monkeypatch.setattr(service.boto3, "client", lambda *a, **k: None)
    monkeypatch.setattr(
        policy, "bedrock_answerer", lambda *a: StubModel({"answer": "x", "cite": [1]})
    )
    monkeypatch.setattr(policy, "guardrail_grounder", lambda *a: grounder())
    monkeypatch.setenv("POLICY_MODEL_ID", "m")
    monkeypatch.setenv("GUARDRAIL_ID", "g")
    monkeypatch.setenv("GUARDRAIL_VERSION", "1")
    context = SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "bank___ask_policy"})
    )
    event = {"customer": "alex", "question": "wire fee?", "audience": "internal"}
    out = tools.handler(event, context)
    assert stub.audiences == CUSTOMER
    assert out["answered"] is True


def test_internal_passage_is_dropped_even_if_the_retriever_returns_it():
    stub = StubRetriever([passage("fraud-escalation", "Thresholds", "internal", 0.9), passage()])
    hits = policy.relevant(stub, "q", CUSTOMER)
    assert [p.doc_id for p in hits] == ["fee-schedule"]


def test_lookup_policy_reads_customer_and_internal_documents():
    stub = StubRetriever([passage("dispute-procedures", "Automatic refunds", "internal", 0.7)])
    out = policy.lookup_policy("auto refund limit", stub)
    assert stub.audiences == STAFF
    assert out["found"] is True
    first = out["passages"][0]
    assert (first["doc_id"], first["section"], first["audience"]) == (
        "dispute-procedures",
        "Automatic refunds",
        "internal",
    )


def test_tool_is_unavailable_until_the_knowledge_base_is_configured(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_BASE_ID", raising=False)
    service._retriever.cache_clear()
    assert service.ask_policy_tool({"question": "x"}) == service.NOT_AVAILABLE
    assert service.lookup_policy("x") == service.NOT_AVAILABLE


# --- grounded answers or "I don't know" ---


def test_weak_retrieval_refuses_without_calling_the_model():
    ask = StubModel({"answer": "x", "cite": [1]})
    out = policy.ask_policy("mortgage rate?", StubRetriever([passage(score=0.1)]), ask, grounder())
    assert out["answered"] is False and out["answer"] == policy.IDK
    assert ask.calls == []


def test_model_null_answer_is_i_dont_know():
    ask = StubModel({"answer": None, "cite": []})
    out = policy.ask_policy("q", StubRetriever([passage()]), ask, grounder())
    assert (out["answered"], out["reason"]) == (False, "not in the policies")


@pytest.mark.parametrize(
    "reply",
    [
        {"answer": "x", "cite": [2]},  # cites a passage it was not given
        {"answer": "x", "cite": []},  # an answer without a citation
        {"answer": "x", "cite": [True]},  # bool is an int subclass
        {"answer": "x", "cite": [1], "extra": 1},
        "not json",
    ],
)
def test_invalid_model_reply_is_refused(reply):
    out = policy.ask_policy("q", StubRetriever([passage()]), StubModel(reply), grounder())
    assert (out["answered"], out["reason"]) == (False, "model reply not valid")


def test_ungrounded_answer_is_withheld():
    ask = StubModel({"answer": "Wires are free.", "cite": [1]})
    out = policy.ask_policy("q", StubRetriever([passage()]), ask, grounder(passed=False))
    assert out["answered"] is False
    assert "Wires are free." not in json.dumps(out)


def test_answer_cites_document_and_section_of_the_cited_passage_only():
    hits = [passage(score=0.7), passage("overdraft-policy", "Overdraft fee", score=0.5)]
    ask = StubModel({"answer": "An outgoing domestic wire costs $25.00.", "cite": [1]})
    out = policy.ask_policy("wire fee?", StubRetriever(hits), ask, grounder())
    assert out["answered"] is True
    assert [(c["doc_id"], c["section"]) for c in out["citations"]] == [
        ("fee-schedule", "Wire transfer fees")
    ]


@pytest.mark.parametrize("question", [None, "", "   ", "x" * 501, 7])
def test_invalid_question_is_an_input_error(question):
    out = policy.ask_policy(question, StubRetriever([]), StubModel({}), grounder())
    assert out["error"].startswith("invalid input: question")


def test_doubled_opening_brace_is_repaired_once():
    assert policy.parse_reply('{{"answer": "a", "cite": [1]}', 1) == ("a", [0])


# --- retrievers ---


def test_knowledge_base_retriever_sends_the_filter_and_reads_metadata():
    class Client:
        def retrieve(self, **kwargs):
            self.kwargs = kwargs
            meta = {"doc_id": "fee-schedule", "title": "Fees", "section": "ATM fees"}
            hit = {"content": {"text": "t"}, "score": 0.5, "metadata": meta}
            return {"retrievalResults": [hit]}

    client = Client()
    hits = KnowledgeBaseRetriever(client, "KB1").retrieve("atm", CUSTOMER, 3)
    config = client.kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert config == {
        "numberOfResults": 3,
        "filter": {"equals": {"key": "audience", "value": "customer"}},
    }
    # No audience in the metadata: `relevant` must drop it rather than trust it.
    assert hits[0].audience == "" and policy.relevant(StubRetriever(hits), "q", CUSTOMER) == []


def test_in_memory_retriever_filters_before_ranking():
    sections = load_corpus()
    words = ["fraud", "overdraft", "wire"]

    def embed(text):  # a bag of three words: enough to rank without Bedrock
        low = text.lower()
        return [float(low.count(w)) + 0.01 for w in words]

    index = InMemoryRetriever(sections, embed)
    hits = index.retrieve("fraud fraud fraud", CUSTOMER, 5)
    assert len(hits) == 5 and all(p.audience == "customer" for p in hits)
    assert index.retrieve("fraud fraud fraud", STAFF, 1)[0].doc_id == "fraud-escalation"


# --- the corpus and its Terraform twin ---


def test_corpus_labels_every_document_and_has_both_audiences():
    sections = load_corpus()
    assert {s.audience for s in sections} == {"customer", "internal"}
    assert len({s.doc_id for s in sections}) >= 8
    keys = [(s.doc_id, s.heading) for s in sections]
    assert len(keys) == len(set(keys)), "a citation must name one section"
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s.effective_date) for s in sections)


def test_text_before_the_first_section_is_refused():
    with pytest.raises(ValueError):
        split_sections("Intro\n## Heading\nbody")
    assert split_sections("## A\none\n## B\ntwo\nlines") == [("A", "one"), ("B", "two\nlines")]


def test_eval_questions_point_at_real_sections():
    sections = {(s.doc_id, s.heading): s.audience for s in load_corpus()}
    questions = json.loads((APP.parents[1] / "knowledge/eval/questions.json").read_text())
    for q in questions:
        if "doc_id" in q:
            assert (q["doc_id"], q["section"]) in sections, q["id"]
        if q.get("refuse") == "internal":
            assert sections[(q["doc_id"], q["section"])] == "internal"
    assert sum("refuse" in q for q in questions) >= 3


def test_terraform_matches_the_code():
    knowledge = (APP / "knowledge.tf").read_text()
    assert f"kb_dimensions  = {DIMENSIONS}" in knowledge
    assert 'chunking_strategy = "NONE"' in knowledge
    # The object text Terraform uploads is the text local mode embeds (corpus.section_text).
    assert 'format("%s (effective %s)\\n\\n## %s\\n\\n%s"' in knowledge
    gateway = (APP / "gateway.tf").read_text()
    schema = gateway[gateway.index('name        = "ask_policy"') :]
    assert '"audience"' not in schema.split("dynamic")[0], "the agent must not pick the audience"
    assert 'Action::"bank___ask_policy"' in (APP / "policy.tf").read_text()
