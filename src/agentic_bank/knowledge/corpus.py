"""The policy corpus: one markdown file per document, `## ` sections, labels in documents.json.

A section is the unit of retrieval and of citation. infra/app/knowledge.tf splits the same files
the same way (one S3 object per section, chunking NONE), so local mode embeds exactly the text the
Knowledge Base would. Keep `Section.text` and the Terraform `content` template in step.
"""

import json
from dataclasses import dataclass
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parents[3] / "knowledge"
AUDIENCES = ("customer", "internal")


@dataclass(frozen=True)
class Section:
    doc_id: str
    title: str
    audience: str
    effective_date: str
    heading: str
    body: str

    @property
    def text(self) -> str:
        """The text embedded and returned: the document title rides along so a short section
        still says which policy it is from (a contextual header). Same template as the S3 object
        content in infra/app/knowledge.tf."""
        return f"{self.title} (effective {self.effective_date})\n\n## {self.heading}\n\n{self.body}"


def split_sections(markdown: str) -> list[tuple[str, str]]:
    """[(heading, body)] of a document made only of `## ` sections. Text before the first heading
    is refused: it would belong to no citable section."""
    text = markdown.strip()
    if not text.startswith("## "):
        raise ValueError("a policy document must start with a '## ' section heading")
    sections = []
    for part in ("\n" + text).split("\n## ")[1:]:
        heading, _, body = part.partition("\n")
        if not heading.strip() or not body.strip():
            raise ValueError(f"empty heading or section: {heading!r}")
        sections.append((heading.strip(), body.strip()))
    return sections


def load_corpus(directory: Path = CORPUS_DIR) -> list[Section]:
    manifest = json.loads((directory / "documents.json").read_text())
    sections = []
    for doc_id, meta in sorted(manifest.items()):
        if meta["audience"] not in AUDIENCES:
            raise ValueError(f"{doc_id}: audience must be one of {AUDIENCES}")
        markdown = (directory / "policies" / f"{doc_id}.md").read_text()
        for heading, body in split_sections(markdown):
            sections.append(
                Section(
                    doc_id, meta["title"], meta["audience"], meta["effective_date"], heading, body
                )
            )
    return sections
