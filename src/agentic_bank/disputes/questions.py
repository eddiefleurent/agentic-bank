"""Question templates. Code picks the id; the text is fixed here, so the model never writes to the
customer and a hostile merchant string cannot reach the customer's screen."""

QUESTION_TEXT = {
    "q_delivery_checked": (
        "The merchant's records show this order as delivered. Have you checked with neighbors, "
        "your building's mail room and the carrier? Please reply with what you found."
    ),
    "q_device_household": (
        "This purchase was made on a device that has bought from this merchant before. "
        "Could someone in your household, or an app on one of your own devices, have made it? "
        "Please reply with what you know."
    ),
}


def question_text(question_id: str) -> str:
    return QUESTION_TEXT[question_id]  # KeyError on an unknown id: a bug, not user input
