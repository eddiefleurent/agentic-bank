from agentic_bank.disputes.decide import decide
from agentic_bank.disputes.review import ReviewOutcome


def review(**scores: int) -> ReviewOutcome:
    criteria = [{"id": k, "score": v, "rationale": "r", "fields": []} for k, v in scores.items()]
    return ReviewOutcome(review={"criteria": criteria, "summary": "s"}, grounding={"passed": True})


def run(model, code=None, reason="not_received", amount=3400, order=True, round_no=0):
    code = {"C1": 1} if code is None else code
    return decide(
        reason_code=reason,
        amount_cents=amount,
        has_order=order,
        code_scores=code,
        review=model,
        round_no=round_no,
    )


def test_rule1_invalid_or_ungrounded_goes_to_analyst():
    out = run(ReviewOutcome(failure="grounding below threshold"))
    assert (out.rule, out.outcome) == (1, "ANALYST")


def test_rule2_model_disagrees_with_code():
    assert run(review(C1=0, C4=1)).rule == 2


def test_rule3_adverse_goes_to_analyst_never_denied():
    out = run(review(C1=-1, C4=1), {"C1": -1})
    assert (out.rule, out.outcome) == (3, "ANALYST")
    out = run(review(C1=1, C4=-1))  # model's own -1 on C4 also goes to a person
    assert (out.rule, out.outcome) == (3, "ANALYST")


def test_rule4_other_reason_or_no_order():
    assert run(review(C4=1), {}, reason="other").rule == 4
    assert run(review(C1=1, C4=1), {}, order=False).rule == 4


def test_rule5_auto_refund_at_the_limit_only():
    assert run(review(C1=1, C4=1), amount=5000).outcome == "AUTO_REFUND"
    assert run(review(C1=1, C4=1), amount=5001).rule == 7


def test_rule6_asks_once_with_a_template():
    out = run(review(C1=0, C4=1), {"C1": 0})
    assert (out.rule, out.outcome, out.question_id) == (6, "ASK", "q_delivery_checked")
    out = run(review(C2=0, C3=1, C4=1), {"C2": 0, "C3": 1}, reason="not_recognised")
    assert out.question_id == "q_device_household"


def test_rule7_still_unclear_after_the_answer():
    out = run(review(C1=0, C4=1), {"C1": 0}, round_no=1)
    assert (out.rule, out.outcome) == (7, "ANALYST")


def test_model_can_never_produce_a_denial():
    for c1 in (-1, 0, 1):
        for c4 in (-1, 0, 1):
            for amount in (100, 9000):
                out = run(review(C1=c1, C4=c4), {"C1": c1}, amount=amount)
                assert out.outcome in {"AUTO_REFUND", "ASK", "ANALYST"}
