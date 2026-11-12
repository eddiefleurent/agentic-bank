"""The investigator's final answer, as a typed model. Strands validates the model's output against
it (structured output); anything that does not fit is a failure, which decision rule 1 sends to an
analyst. There is no amount field and no outcome field: a recommendation is advice only.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Path = Annotated[str, StringConstraints(pattern=r"^[a-z_]+(\.[a-z0-9_]+)*$", max_length=60)]
# "DR-2" (local rules file) or "dispute-procedures/denials" (knowledge base: doc_id/section slug)
PolicyRef = Annotated[
    str, StringConstraints(pattern=r"^(DR-\d{1,2}|[a-z0-9-]{1,40}/[a-z0-9-]{1,60})$")
]
Flag = Literal["concedes_charge", "instructs_reviewer"]  # == disputes.review.FLAGS (tested)


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: Literal["C1", "C2", "C3", "C4"]
    score: Literal[-1, 0, 1]
    rationale: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    fields: list[Path] = Field(max_length=8)


class Investigation(BaseModel):
    """Score the named criteria, cite case-file paths and dispute rules, and recommend."""

    model_config = ConfigDict(extra="forbid", strict=True)

    criteria: list[Criterion] = Field(min_length=1, max_length=4)
    flags: list[Flag] = Field(
        max_length=2, description="Statement flags; [] if none. Any flag blocks auto-refund."
    )  # required, as for the classifier: an omitted flag must not read as "none"
    summary: Annotated[str, StringConstraints(max_length=600)]
    recommendation: Literal["refund_eligible", "ask_customer", "escalate"] = Field(
        description="Advice for the bank's rules and its analyst. It never pays or denies."
    )
    policy_refs: list[PolicyRef] = Field(default_factory=list, max_length=5)
    history_note: Annotated[str, StringConstraints(max_length=300)] = ""
