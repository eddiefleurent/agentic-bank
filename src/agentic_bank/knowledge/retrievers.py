"""Two retrievers behind one interface: the Bedrock Knowledge Base (deployed) and an in-memory
index over the same sections (local mode, for tests of the real embeddings and the eval).

Both apply the audience filter BEFORE ranking, as Retrieve does with a metadata filter: the top k
are the best k the caller may read, not the best k overall with some removed afterwards.
"""

import json
import math
from collections.abc import Callable, Sequence
from typing import Any

from agentic_bank.knowledge.corpus import Section
from agentic_bank.knowledge.policy import Passage, audience_filter

EMBED_MODEL = "amazon.titan-embed-text-v2:0"
DIMENSIONS = 1024  # must match the S3 Vectors index in infra/app/knowledge.tf

Embedder = Callable[[str], list[float]]


class KnowledgeBaseRetriever:
    """bedrock-agent-runtime Retrieve with the audience metadata filter set by our code."""

    def __init__(self, client: Any, knowledge_base_id: str):
        self.client = client
        self.knowledge_base_id = knowledge_base_id

    def retrieve(self, query: str, audiences: tuple[str, ...], k: int) -> list[Passage]:
        response = self.client.retrieve(
            knowledgeBaseId=self.knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": k,
                    "filter": audience_filter(audiences),
                }
            },
        )
        passages = []
        for hit in response.get("retrievalResults", []):
            meta = hit.get("metadata") or {}
            passages.append(
                Passage(
                    doc_id=str(meta.get("doc_id", "unknown")),
                    title=str(meta.get("title", "unknown")),
                    section=str(meta.get("section", "unknown")),
                    effective_date=str(meta.get("effective_date", "")),
                    audience=str(meta.get("audience", "")),  # missing => dropped by `relevant`
                    text=hit.get("content", {}).get("text", ""),
                    score=float(hit.get("score", 0.0)),
                )
            )
        return passages


def titan_embedder(bedrock: Any) -> Embedder:
    def embed(text: str) -> list[float]:
        body = {"inputText": text, "dimensions": DIMENSIONS, "normalize": True}
        response = bedrock.invoke_model(modelId=EMBED_MODEL, body=json.dumps(body))
        return json.loads(response["body"].read())["embedding"]

    return embed


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class InMemoryRetriever:
    """Local mode: embed every section once, rank by cosine similarity."""

    def __init__(self, sections: list[Section], embed: Embedder):
        self.embed = embed
        self.index = [(s, embed(s.text)) for s in sections]

    def retrieve(self, query: str, audiences: tuple[str, ...], k: int) -> list[Passage]:
        q = self.embed(query)
        scored = [(cosine(q, v), s) for s, v in self.index if s.audience in audiences]
        scored.sort(key=lambda pair: -pair[0])
        return [
            Passage(
                doc_id=s.doc_id,
                title=s.title,
                section=s.heading,
                effective_date=s.effective_date,
                audience=s.audience,
                text=s.text,
                score=score,
            )
            for score, s in scored[:k]
        ]
