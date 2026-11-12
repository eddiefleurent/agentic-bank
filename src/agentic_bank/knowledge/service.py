"""Entry points wired to deployed resources: the ask_policy MCP tool and lookup_policy(query).

Configuration comes from the Lambda environment (infra/app/knowledge.tf). If the Knowledge Base is
not deployed, both say so instead of failing at import time.
"""

import functools
import os
from typing import Any

import boto3
from botocore.config import Config

from agentic_bank.knowledge import policy
from agentic_bank.knowledge.retrievers import KnowledgeBaseRetriever

NOT_AVAILABLE = {"error": "the policy assistant is not available"}
# Retrieve and the model call are reads: one retry is safe. Bounded so the tool fits its timeout.
CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=20, retries={"total_max_attempts": 2})


@functools.cache
def _retriever() -> KnowledgeBaseRetriever | None:
    kb_id = os.environ.get("KNOWLEDGE_BASE_ID")
    if not kb_id:
        return None
    client = boto3.client("bedrock-agent-runtime", config=CLIENT_CONFIG)
    return KnowledgeBaseRetriever(client, kb_id)


def ask_policy_tool(event: dict[str, Any]) -> dict[str, Any]:
    """The gateway tool. Every gateway caller is a customer token (the interceptor set `customer`
    from its subject), so the audience is fixed to "customer". Any `audience` argument the agent
    sends is ignored: identity, not the prompt, picks the filter."""
    retriever = _retriever()
    if retriever is None:
        return NOT_AVAILABLE
    bedrock = boto3.client("bedrock-runtime", config=CLIENT_CONFIG)
    return policy.ask_policy(
        event.get("question"),
        retriever,
        policy.bedrock_answerer(bedrock, os.environ["POLICY_MODEL_ID"]),
        policy.guardrail_grounder(
            bedrock, os.environ["GUARDRAIL_ID"], os.environ["GUARDRAIL_VERSION"]
        ),
    )


def lookup_policy(query: str) -> dict[str, Any]:
    """The dispute investigator's policy seam: customer AND internal documents, passages with
    citations, no generated answer. Only bank-side code calls this; it is not a gateway tool.
    See docs/rag.md for the contract."""
    retriever = _retriever()
    if retriever is None:
        return NOT_AVAILABLE
    return policy.lookup_policy(query, retriever)
