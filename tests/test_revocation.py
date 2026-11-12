import base64
import json

import pytest

from agentic_bank.tools import interceptor


@pytest.mark.parametrize(
    ("row", "auth_time", "revoked"),
    [
        ({"revoked_at": 1000}, 999, True),
        ({"revoked_at": 1000}, 1000, True),
        ({"revoked_at": 1000}, 1001, False),
        (None, 5, False),
        ({"revoked_at": 1000}, None, True),  # no auth_time: fail closed
        ({"customer": "alex"}, 1001, True),  # unreadable row: fail closed
    ],
)
def test_revocation_decision(row, auth_time, revoked):
    assert interceptor.is_revoked(row, auth_time) is revoked


def request(method: str, claims: dict) -> dict:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"name": "bank___x"}}
    headers = {"Authorization": f"Bearer h.{payload}.s"}
    return {"mcp": {"gatewayRequest": {"headers": headers, "body": body}}}


CLAIMS = {"sub": "alex", "client_id": "agent", "auth_time": 900}


def refused(out: dict) -> bool:
    resp = out["mcp"].get("transformedGatewayResponse")
    return bool(resp) and resp["body"]["error"]["code"] == -32001


@pytest.mark.parametrize("method", ["tools/call", "tools/list"])
def test_revoked_agent_refused_on_call_and_list(monkeypatch, method):
    monkeypatch.setattr(interceptor, "revocation_row", lambda c, k: {"revoked_at": 1000})
    assert refused(interceptor.handler(request(method, CLAIMS), None))


@pytest.mark.parametrize("method", ["tools/call", "tools/list"])
def test_lookup_error_fails_closed(monkeypatch, method):
    def boom(customer, client_id):
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(interceptor, "revocation_row", boom)
    assert refused(interceptor.handler(request(method, CLAIMS), None))


def test_new_sign_in_after_revocation_passes(monkeypatch):
    monkeypatch.setattr(interceptor, "revocation_row", lambda c, k: {"revoked_at": 1000})
    out = interceptor.handler(request("tools/list", {**CLAIMS, "auth_time": 1001}), None)
    assert "transformedGatewayRequest" in out["mcp"]
