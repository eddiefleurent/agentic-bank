"""The decision: a pure function, first matching rule wins. The model never denies.

Possible outcomes are AUTO_REFUND, ASK and ANALYST. A "no" exists only as a person's decision.
"""

from dataclasses import dataclass

from agentic_bank.disputes.review import ReviewOutcome

AUTO_REFUND_MAX_CENTS = 5000
QUESTIONS = {"not_received": "q_delivery_checked", "not_recognised": "q_device_household"}


@dataclass(frozen=True)
class Decision:
    rule: int
    outcome: str  # AUTO_REFUND | ASK | ANALYST
    question_id: str | None = None


def decide(
    *,
    reason_code: str,
    amount_cents: int,
    has_order: bool,
    code_scores: dict[str, int],
    review: ReviewOutcome,
    round_no: int,
) -> Decision:
    if review.failure or review.review is None:  # 1: bad JSON, unknown field, weak grounding
        return Decision(1, "ANALYST")
    model = {c["id"]: c["score"] for c in review.review["criteria"]}
    if any(model.get(cid) != score for cid, score in code_scores.items()):  # 2: model vs code
        return Decision(2, "ANALYST")
    # Code's score wins for C1-C3 (equal by now); C4 is the model's.
    scores = {**model, **code_scores}.values()
    if any(s == -1 for s in scores):  # 3: a "no" is for a human
        return Decision(3, "ANALYST")
    if reason_code not in QUESTIONS or not has_order:  # 4
        return Decision(4, "ANALYST")
    if all(s == 1 for s in scores) and amount_cents <= AUTO_REFUND_MAX_CENTS:  # 5
        # 9: a model flag (customer concedes the charge, or the statement instructs the reviewer)
        # only ever takes away the automatic refund. It is a model judgment, so it may make the
        # outcome more cautious, never less: it is read here and nowhere else.
        if review.review.get("flags"):
            return Decision(9, "ANALYST")
        return Decision(5, "AUTO_REFUND")
    if round_no == 0 and any(s == 0 for s in scores):  # 6: no -1 here, so some 0
        return Decision(6, "ASK", QUESTIONS[reason_code])
    return Decision(7, "ANALYST")


TIMEOUT = Decision(8, "ANALYST")  # rule 8: no answer to our question in time
