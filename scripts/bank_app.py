"""The Agentic Bank app, as a CLI that plays the phone app.

    uv run python scripts/bank_app.py <user> list
    uv run python scripts/bank_app.py <user> approve <transfer_id> [--yes]
    uv run python scripts/bank_app.py <user> reject <transfer_id>
    uv run python scripts/bank_app.py <user> revoke-agent

Every run signs in fresh with the public `app` client (password, then TOTP; credentials from
~/.config/agentic-bank/<user>.json) and calls the approvals API. Before approving it recomputes the
transfer hash from the fields it prints and refuses to send if that differs from the listed hash:
what you see is what you sign. Secrets and tokens are never printed.
"""

import argparse
import base64
import hashlib
import hmac
import json
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentic_bank.shared.transfers import transfer_hash  # noqa: E402  same function as the API

REGION = "us-east-1"
POOL_NAME = "agentic-bank-customers"
APP_CLIENT_NAME = "app"
API_NAME = "agentic-bank-app"


def totp_now(secret: str) -> str:
    """RFC 6238 code. Waits for a fresh 30 s window (Cognito rejects reuse)."""
    time.sleep(30 - time.time() % 30 + 0.5)
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(time.time() // 30)), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**6:06d}"


def credentials(user: str) -> dict[str, str]:
    path = Path.home() / ".config" / "agentic-bank" / f"{user}.json"
    if not path.exists():
        raise SystemExit(f"missing {path}: run scripts/setup_customer.py {user}")
    return json.loads(path.read_text())


def find_approvals_url() -> str:
    """Endpoint of the HTTP API named `agentic-bank-app` (stage `$default`: no path prefix)."""
    client = boto3.client("apigatewayv2", region_name=REGION)
    token: str | None = None
    while True:
        page = client.get_apis(**({"NextToken": token} if token else {}))
        for api in page["Items"]:
            if api["Name"] == API_NAME:
                return api["ApiEndpoint"].rstrip("/")
        token = page.get("NextToken")
        if not token:
            raise SystemExit(f"no HTTP API named {API_NAME}: is the app stack deployed?")


def sign_in(user: str) -> str:
    """Password + TOTP with the public `app` client; returns the access token."""
    creds = credentials(user)
    cognito = boto3.client("cognito-idp", region_name=REGION)
    pools = cognito.list_user_pools(MaxResults=60)["UserPools"]
    pool_id = next(p["Id"] for p in pools if p["Name"] == POOL_NAME)
    clients = cognito.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)["UserPoolClients"]
    client_id = next(c["ClientId"] for c in clients if c["ClientName"] == APP_CLIENT_NAME)
    auth = cognito.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": user, "PASSWORD": creds["password"]},
    )
    if auth.get("ChallengeName") != "SOFTWARE_TOKEN_MFA":
        raise SystemExit(f"sign-in: expected a TOTP challenge, got {auth.get('ChallengeName')}")
    done = cognito.respond_to_auth_challenge(
        ClientId=client_id,
        ChallengeName="SOFTWARE_TOKEN_MFA",
        Session=auth["Session"],
        ChallengeResponses={
            "USERNAME": user,
            "SOFTWARE_TOKEN_MFA_CODE": totp_now(creds["totp_secret"]),
        },
    )
    return done["AuthenticationResult"]["AccessToken"]


def subject_of(token: str) -> str:
    """The `sub` claim. Read without verifying: the API verifies; we only need our own ID."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["sub"]


def api(
    method: str, path: str, token: str | None, body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    """Call the approvals API; return (HTTP status, parsed JSON or None)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        find_approvals_url() + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def transfers_of(reply: Any) -> list[dict[str, Any]]:
    """The list from GET /transfers (a bare list or {"transfers": [...]})."""
    return reply["transfers"] if isinstance(reply, dict) else reply


def recompute_hash(customer: str, item: dict[str, Any]) -> str:
    """Hash from the fields the app displays (memo is the listed `memo_untrusted`)."""
    return transfer_hash(
        customer=customer,
        transfer_id=item["transfer_id"],
        from_account_id=item["from_account_id"],
        payee_id=item["payee_id"],
        amount_cents=item["amount_cents"],
        memo=item["memo_untrusted"],
        expires_at=item["expires_at"],
    )


def show(customer: str, item: dict[str, Any]) -> None:
    ok = recompute_hash(customer, item) == item["request_hash"] and item.get("intact", False)
    state = "EXPIRED" if item.get("expired") and item["status"] == "PENDING" else item["status"]
    print(
        f"{item['transfer_id']}  {state}  ${item['amount']}  "
        f"{item['from_account_id']} ****{item['from_last4']} -> {item['payee_name']} "
        f"(****{item['payee_last4']})"
    )
    print(
        f"{'':23}memo from your agent (unverified): {item['memo_untrusted']!r}   "
        f"expires {item['expires_at']}   integrity {'ok' if ok else 'BROKEN'}"
    )


def find(customer: str, token: str, transfer_id: str) -> dict[str, Any]:
    status, reply = api("GET", f"/transfers/{quote(transfer_id, safe='')}", token)
    if status == 404:
        raise SystemExit("no such transfer")
    if status != 200:
        raise SystemExit(f"lookup failed: HTTP {status}")
    return reply["transfer"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("user")
    parser.add_argument("command", choices=["list", "approve", "reject", "revoke-agent"])
    parser.add_argument("transfer_id", nargs="?")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()
    if args.command in ("approve", "reject") and not args.transfer_id:
        parser.error(f"{args.command} needs a transfer_id")

    token = sign_in(args.user)
    customer = subject_of(token)

    if args.command == "list":
        status, reply = api("GET", "/transfers", token)
        if status != 200:
            raise SystemExit(f"list failed: HTTP {status}")
        for item in transfers_of(reply):
            show(customer, item)
    elif args.command == "approve":
        item = find(customer, token, args.transfer_id)
        show(customer, item)
        if not item.get("intact", False) or recompute_hash(customer, item) != item["request_hash"]:
            raise SystemExit("REFUSED: what is displayed does not match the listed hash")
        if not item["actionable"]:
            raise SystemExit("REFUSED: not actionable (decided, expired or missing bank records)")
        if not args.yes:
            question = (
                f"Approve ${item['amount']} to {item['payee_name']} ****{item['payee_last4']} "
                f"from ****{item['from_last4']}? [y/N] "
            )
            if input(question).strip().lower() != "y":
                raise SystemExit("not approved")
        status, reply = api(
            "POST",
            f"/transfers/{args.transfer_id}/approve",
            token,
            {"request_hash": recompute_hash(customer, item)},
        )
        if status == 200:
            balance = reply.get("balance") if isinstance(reply, dict) else None
            print("EXECUTED." + (f" Balance: ${balance}" if balance else ""))
        else:
            error = reply.get("error") if isinstance(reply, dict) else None
            raise SystemExit(f"HTTP {status}: {error}")
    elif args.command == "reject":
        status, reply = api("POST", f"/transfers/{args.transfer_id}/reject", token, {})
        if status != 200:
            error = reply.get("error") if isinstance(reply, dict) else None
            raise SystemExit(f"HTTP {status}: {error}")
        print("REJECTED.")
    else:
        status, reply = api("POST", "/agent/revoke", token, {})
        if status != 200:
            raise SystemExit(f"revoke failed: HTTP {status}")
        print(f"agent revoked at {time.strftime('%H:%M:%SZ', time.gmtime())}")


if __name__ == "__main__":
    main()
