"""AgentCore Gateway REQUEST interceptor: the token, not the agent, decides who the customer is.

For `tools/call` it sets `arguments.customer` to the token's subject, overwriting whatever the agent
sent. The gateway's JWT authorizer already verified the token (signature, issuer, expiry, client),
so we only read its claims. Everything else passes through unchanged.
"""

import base64
import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

NO_SUBJECT = {
    "code": -32001,
    "message": "Request refused: caller identity could not be established.",
}


REVOKED = {
    "code": -32001,
    "message": "Request refused: the customer has revoked this agent's connection.",
}


def is_revoked(row: dict[str, Any] | None, auth_time: Any) -> bool:
    """Revoked if a row exists and the token was issued by a sign-in at or before it.

    Fails closed: with a row, an unreadable `auth_time` or `revoked_at` counts as revoked.
    """
    if row is None:
        return False
    try:
        return int(auth_time) <= int(row["revoked_at"])
    except TypeError, ValueError, KeyError:
        return True


def revocation_row(customer: str, client_id: str) -> dict[str, Any] | None:
    arn = os.environ["REVOCATIONS_TABLE_ARN"]
    table = boto3.resource("dynamodb").Table(arn.rpartition("/")[2])
    key = {"customer": customer, "client_id": client_id}
    return table.get_item(Key=key, ConsistentRead=True).get("Item")


def claims_of(headers: dict[str, Any]) -> dict[str, Any]:
    """Claims of the bearer token in the request headers, or {} if unreadable."""
    auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
    scheme, _, token = str(auth).partition(" ")
    if scheme.lower() != "bearer":
        return {}
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except IndexError, ValueError:
        return {}
    return claims if isinstance(claims, dict) else {}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request = (event.get("mcp") or {}).get("gatewayRequest") or {}
    body = request.get("body") or {}
    method = body.get("method")
    if method not in ("tools/call", "tools/list"):
        return _forward(body)

    claims = claims_of(request.get("headers") or {})
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        logger.error("no subject in token")
        return _refuse(body, NO_SUBJECT)
    client_id = claims.get("client_id")
    try:
        row = revocation_row(subject, str(client_id))
        revoked = is_revoked(row, claims.get("auth_time"))
    except Exception:  # fail closed: a lookup we cannot complete is a refusal
        logger.exception("revocation lookup failed")
        revoked = True
    if revoked:
        logger.info(json.dumps({"subject": subject, "client_id": client_id, "refused": "revoked"}))
        return _refuse(body, REVOKED)
    if method == "tools/list":
        return _forward(body)

    params = body.get("params") or {}
    arguments = {**(params.get("arguments") or {}), "customer": subject}
    # One audit line: who, through which client, which tool. Never the token or the arguments.
    logger.info(
        json.dumps({"subject": subject, "client_id": client_id, "tool": params.get("name")})
    )
    return _forward({**body, "params": {**params, "arguments": arguments}})


def _refuse(body: dict[str, Any], error: dict[str, Any]) -> dict[str, Any]:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "body": {"jsonrpc": "2.0", "id": body.get("id"), "error": error},
            }
        },
    }


def _forward(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": {"body": body}},
    }
