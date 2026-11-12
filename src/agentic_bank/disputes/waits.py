"""Resume a paused Step Functions task from a stored task token, at most once.

The token sits on the case row while the workflow waits. Resuming first claims (consumes) it with a
conditional UpdateItem: REMOVE task_token, only if the status is the expected one and a token
exists. A second caller finds no token and gets AlreadyResumed. Then it calls SendTaskSuccess.
If that call fails, delivery is unknown (the workflow may have advanced), so the token stays
consumed and the caller is told it is unconfirmed. Never resubmit automatically. If the send
really failed, the wait's 1 h task timeout moves the case on; nothing else reconciles it.
"""

import json
from typing import Any

from botocore.exceptions import ClientError

from agentic_bank.shared.ddb import deser, ser


class AlreadyResumed(Exception):
    """No waiting task for this case in this state (answered, decided, timed out)."""


class CallbackUnconfirmed(Exception):
    """The send failed in a way that may or may not have delivered it; the token stays consumed."""


def resume(
    ddb: Any,
    sfn: Any,
    table: str,
    customer: str,
    case_id: str,
    *,
    expected_status: str,
    sets: dict[str, Any],
    output: dict[str, Any],
) -> None:
    payload = json.dumps(output)  # fail before consuming the token, not after
    names = {"#s": "status", **{f"#k{i}": k for i, k in enumerate(sets)}}
    values = {":exp": ser(expected_status)}
    values |= {f":v{i}": ser(v) for i, v in enumerate(sets.values())}
    assignments = ", ".join(f"#k{i} = :v{i}" for i in range(len(sets)))
    try:
        old = ddb.update_item(
            TableName=table,
            Key={"customer": ser(customer), "sk": ser(f"CASE#{case_id}")},
            UpdateExpression=(f"SET {assignments} " if assignments else "")
            + "REMOVE task_token, analyst_queue",
            ConditionExpression="#s = :exp AND attribute_exists(task_token)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="UPDATED_OLD",  # only what we touched (the token included)
        )["Attributes"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise AlreadyResumed(case_id) from None
        raise
    try:
        sfn.send_task_success(taskToken=deser(old)["task_token"], output=payload)
    except Exception as exc:
        raise CallbackUnconfirmed(case_id) from exc
