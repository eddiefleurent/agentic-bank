"""Shared helpers for the proof scripts: discovery, customer sign-in, gateway calls, PASS/FAIL.

Everything is discovered at run time (SSM, Cognito, STS), nothing is hardcoded. Secrets (password,
TOTP seed, client secret, tokens) are read into memory and never printed.
Customer credentials: ~/.config/agentic-bank/<user>.json = {username, password, totp_secret},
written by scripts/setup_customer.py.
"""

import base64
import hashlib
import hmac
import html
import http.cookiejar
import json
import re
import secrets
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bank_app  # noqa: E402  the CLI that plays the phone app: sign-in and API calls live there

REGION = "us-east-1"
POOL_NAME = "agentic-bank-customers"
GATEWAY_URL_PARAM = "/agentic-bank/gateway-url"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"  # the `agent` client's only callback
_failures = 0


def check(claim: str, ok: bool) -> None:
    """Print one PASS/FAIL line; remember failures for exit_code()."""
    global _failures
    _failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {claim}")


def exit_code() -> int:
    return 1 if _failures else 0


def totp_now(secret: str) -> str:
    """RFC 6238: HMAC-SHA1, 30 s steps, 6 digits. Waits for a new window (Cognito rejects reuse)."""
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


class Stack:
    """Names discovered from the deployed stack."""

    def __init__(self) -> None:
        self.cognito = boto3.client("cognito-idp", region_name=REGION)
        ssm = boto3.client("ssm", region_name=REGION)
        self.gateway_url: str = ssm.get_parameter(Name=GATEWAY_URL_PARAM)["Parameter"]["Value"]
        pools = self.cognito.list_user_pools(MaxResults=60)["UserPools"]
        self.pool_id: str = next(p["Id"] for p in pools if p["Name"] == POOL_NAME)
        pool = self.cognito.describe_user_pool(UserPoolId=self.pool_id)["UserPool"]
        self.domain = f"https://{pool['Domain']}.auth.{REGION}.amazoncognito.com"
        clients = self.cognito.list_user_pool_clients(UserPoolId=self.pool_id, MaxResults=60)
        self.client_ids = {c["ClientName"]: c["ClientId"] for c in clients["UserPoolClients"]}

    def subject(self, user: str) -> str:
        """The Cognito `sub` of a customer (the partition key of their rows)."""
        attrs = self.cognito.admin_get_user(UserPoolId=self.pool_id, Username=user)
        return next(a["Value"] for a in attrs["UserAttributes"] if a["Name"] == "sub")

    def token_without_scope(self, user: str) -> str:
        """Access token from the `proof` client (password + TOTP, no OAuth): no read scope."""
        creds = credentials(user)
        client_id = self.client_ids["proof"]
        auth = self.cognito.admin_initiate_auth(
            UserPoolId=self.pool_id,
            ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": user, "PASSWORD": creds["password"]},
        )
        done = self.cognito.admin_respond_to_auth_challenge(
            UserPoolId=self.pool_id,
            ClientId=client_id,
            ChallengeName="SOFTWARE_TOKEN_MFA",
            Session=auth["Session"],
            ChallengeResponses={
                "USERNAME": user,
                "SOFTWARE_TOKEN_MFA_CODE": totp_now(creds["totp_secret"]),
            },
        )
        return done["AuthenticationResult"]["AccessToken"]

    @property
    def approvals_url(self) -> str:
        """The bank app API (HTTP API `agentic-bank-app`), found by name."""
        return bank_app.find_approvals_url()

    def app_token(self, user: str) -> str:
        """Fresh password + TOTP sign-in with the public `app` client (what the phone app does)."""
        return bank_app.sign_in(user)

    def token_with_scope(self, user: str, scopes: tuple[str, ...] = ("read", "propose")) -> str:
        """Access token from the `agent` client with the given scopes (default read, propose)."""
        return self.agent_tokens(user, scopes)["access_token"]

    def agent_tokens(
        self, user: str, scopes: tuple[str, ...] = ("read", "propose")
    ) -> dict[str, str]:
        """Token response of the `agent` client: authorization code + PKCE through the hosted
        login page (password, then TOTP), as an MCP client would, with resource=<gateway URL>.
        Holds access_token and refresh_token."""
        creds = credentials(user)
        client_id = self.client_ids["agent"]
        client = self.cognito.describe_user_pool_client(
            UserPoolId=self.pool_id, ClientId=client_id
        )["UserPoolClient"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CALLBACK,
                "scope": " ".join(["openid", *(f"{self.gateway_url}/{s}" for s in scopes)]),
                "resource": self.gateway_url,
                "state": secrets.token_urlsafe(8),
                "code_challenge": challenge.rstrip(b"=").decode(),
                "code_challenge_method": "S256",
            }
        )
        code = _hosted_login(f"{self.domain}/oauth2/authorize?{query}", creds)
        basic = base64.b64encode(f"{client_id}:{client['ClientSecret']}".encode()).decode()
        request = urllib.request.Request(
            f"{self.domain}/oauth2/token",
            data=urllib.parse.urlencode(
                {
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "redirect_uri": CALLBACK,
                    "code_verifier": verifier,
                }
            ).encode(),
            headers={"Authorization": f"Basic {basic}"},
        )
        with urllib.request.urlopen(request) as response:
            tokens: dict[str, str] = json.load(response)
        tokens["_client_id"] = client_id
        tokens["_client_secret"] = client["ClientSecret"]
        return tokens

    def refresh_agent_token(self, tokens: dict[str, str]) -> str:
        """New access token from the agent's refresh token (keeps the original auth_time)."""
        basic = base64.b64encode(
            f"{tokens['_client_id']}:{tokens['_client_secret']}".encode()
        ).decode()
        request = urllib.request.Request(
            f"{self.domain}/oauth2/token",
            data=urllib.parse.urlencode(
                {
                    "grant_type": "refresh_token",
                    "client_id": tokens["_client_id"],
                    "refresh_token": tokens["refresh_token"],
                }
            ).encode(),
            headers={"Authorization": f"Basic {basic}"},
        )
        with urllib.request.urlopen(request) as response:
            return json.load(response)["access_token"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _form(page: str) -> tuple[str, dict[str, str], list[str]]:
    """Hosted login form: action, hidden fields (CSRF etc.), all input names."""
    action = re.search(r'<form[^>]*action="([^"]*)"', page)
    hidden = dict(
        re.findall(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page)
    )
    names = re.findall(r'<input[^>]*name="([^"]+)"', page)
    return (html.unescape(action.group(1)) if action else ""), hidden, names


def _hosted_login(authorize_url: str, creds: dict[str, str]) -> str:
    """Walk the login page like a browser (password, then TOTP); return the authorization code."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _NoRedirect
    )

    def fetch(url: str, data: dict[str, str] | None = None) -> tuple[str, str, str]:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        try:
            with opener.open(urllib.request.Request(url, data=body)) as response:
                return response.url, response.read().decode(), ""
        except urllib.error.HTTPError as error:  # a redirect (we refuse to follow) or a failure
            return url, "", error.headers.get("location", "")

    url, page, location = fetch(authorize_url)
    while location and not location.startswith(CALLBACK):  # follow redirects inside Cognito
        url, page, location = fetch(urllib.parse.urljoin(url, location))
    action, fields, _ = _form(page)
    fields.update(username=creds["username"], password=creds["password"])
    url, page, location = fetch(urllib.parse.urljoin(url, action), fields)
    while location and not location.startswith(CALLBACK):
        url, page, location = fetch(urllib.parse.urljoin(url, location))
    action, fields, names = _form(page)
    field = next((n for n in names if "code" in n.lower() or "mfa" in n.lower()), None)
    if field is None:
        raise SystemExit("sign-in did not reach the TOTP step")
    fields[field] = totp_now(creds["totp_secret"])
    url, page, location = fetch(urllib.parse.urljoin(url, action), fields)
    code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code")
    if not code:
        raise SystemExit("sign-in finished without an authorization code")
    return code[0]


def mcp_post(gateway_url: str, body: dict[str, Any], token: str | None) -> tuple[int, Any]:
    """POST one JSON-RPC message to the gateway; return (HTTP status, parsed JSON or None)."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(gateway_url, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def call_tool(
    gateway_url: str, token: str | None, tool: str, arguments: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """tools/call bank___<tool>. Returns (HTTP status, tool output as a dict).

    A gateway or Cedar refusal has no tool output: the dict is then {"refused": <raw response>}.
    """
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": f"bank___{tool}", "arguments": arguments},
    }
    status, reply = mcp_post(gateway_url, message, token)
    try:
        result = reply["result"]
        if result.get("isError"):
            raise KeyError("isError")
        return status, json.loads(result["content"][0]["text"])
    except KeyError, IndexError, TypeError, ValueError:
        return status, {"refused": reply}


def rpc_error_code(reply: Any) -> int | None:
    """JSON-RPC error code of a gateway reply, or None when it is not an error."""
    try:
        return int(reply["error"]["code"])
    except KeyError, TypeError, ValueError:
        return None


def run(main: Any) -> None:
    main()
    sys.exit(exit_code())
