"""Dispute rules for the policy checker, behind a one-method seam.

`PolicySource.lookup(query)` is the seam. `policy_source()` picks one from env INVESTIGATOR_POLICY:
  local           (default) `LocalPolicy` scores the sections of a synthetic markdown file by
                  shared words. Needs no AWS.
  knowledge_base  `KnowledgeBasePolicy` calls the RAG `lookup_policy` (agentic_bank.knowledge):
                  Bedrock Knowledge Base Retrieve over customer AND internal documents. Needs
                  KNOWLEDGE_BASE_ID and bedrock:Retrieve (infra/app/knowledge.tf).
The agent, its tool and the tests do not change. Whatever implements it must stay read-only and
return bank-owned text only: the passages reach the model as instructions-adjacent context.
"""

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

POLICY_FILE = Path(__file__).with_name("dispute_policy.md")
MAX_QUERY = 200
MAX_PASSAGES = 3
SECTION = re.compile(r"^## (DR-\d+) (.+)$", re.MULTILINE)
WORD = re.compile(r"[a-z0-9_]{3,}")


@dataclass(frozen=True)
class Passage:
    section_id: str  # "DR-2" or "dispute-procedures/denials": cited in policy_refs
    title: str
    text: str


class PolicySource(Protocol):
    def lookup(self, query: str, k: int = 2) -> list[Passage]: ...


def parse_sections(markdown: str) -> list[Passage]:
    heads = list(SECTION.finditer(markdown))
    return [
        Passage(
            h.group(1),
            h.group(2).strip(),
            markdown[h.end() : heads[i + 1].start() if i + 1 < len(heads) else None].strip(),
        )
        for i, h in enumerate(heads)
    ]


class LocalPolicy:
    """Keyword overlap over a local file. Good enough for nine sections; not retrieval quality."""

    def __init__(self, path: Path = POLICY_FILE) -> None:
        self.sections = parse_sections(path.read_text())

    def lookup(self, query: str, k: int = 2) -> list[Passage]:
        words = set(WORD.findall(query.lower()[:MAX_QUERY]))
        scored = [
            (len(words & set(WORD.findall(f"{p.title} {p.text}".lower()))), i, p)
            for i, p in enumerate(self.sections)
        ]
        hits = [p for score, _, p in sorted(scored, key=lambda t: (-t[0], t[1])) if score > 0]
        return hits[: max(1, min(k, MAX_PASSAGES))]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


class KnowledgeBasePolicy:
    """The RAG `lookup_policy` behind the same seam. Its citations (document, section) become
    `doc_id/section-slug` ids. An unavailable knowledge base raises: Strands hands the model a tool
    error, so the investigator cannot mistake "no knowledge base" for "no rule applies"."""

    def __init__(self, lookup: Callable[[str], dict[str, Any]] | None = None) -> None:
        if lookup is None:
            from agentic_bank.knowledge.service import lookup_policy  # boto3: only when chosen

            lookup = lookup_policy
        self._lookup = lookup

    def lookup(self, query: str, k: int = 2) -> list[Passage]:
        result = self._lookup(query[:MAX_QUERY])
        if "error" in result:
            raise RuntimeError(f"policy knowledge base: {result['error']}")
        return [
            Passage(
                f"{p['doc_id']}/{slug(p['section'])}", f"{p['document']}: {p['section']}", p["text"]
            )
            for p in result["passages"][: max(1, min(k, MAX_PASSAGES))]
        ]


SOURCES = ("local", "knowledge_base")


def policy_source(name: str | None = None) -> PolicySource:
    """The policy source from config (env INVESTIGATOR_POLICY), local unless asked otherwise."""
    name = name or os.environ.get("INVESTIGATOR_POLICY", "local")
    if name == "local":
        return LocalPolicy()
    if name == "knowledge_base":
        return KnowledgeBasePolicy()
    raise ValueError(f"unknown INVESTIGATOR_POLICY {name!r}; expected one of {SOURCES}")
