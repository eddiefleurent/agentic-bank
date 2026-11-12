"""Proof: the customer can cut the agent off at once, and can reconnect afterwards.

    uv run python proofs/revoke_agent.py alex

Needs the deployed stack and credential files for alex and sam. Revokes alex's agent
connection (per customer and agent client), then signs alex in again, so the other proofs still
pass afterwards. Takes a few minutes: each sign-in waits for a fresh TOTP window.
"""

import sys
from typing import Any

from _common import Stack, bank_app, call_tool, check, mcp_post, rpc_error_code, run


def listing(stack: Stack, token: str) -> tuple[int, Any]:
    return mcp_post(stack.gateway_url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token)


def call_raw(stack: Stack, token: str) -> Any:
    message = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "bank___get_accounts", "arguments": {}},
    }
    return mcp_post(stack.gateway_url, message, token)[1]


def sees_accounts(stack: Stack, token: str, expected: list[str]) -> bool:
    _, out = call_tool(stack.gateway_url, token, "get_accounts", {})
    return sorted(a["account_id"] for a in out.get("accounts", [])) == expected


def main(user: str) -> None:
    stack = Stack()
    tokens = stack.agent_tokens(user)
    agent = tokens["access_token"]
    sam_agent = stack.token_with_scope("sam")
    app = stack.app_token(user)

    check(
        f"before revoke: {user}'s agent token reads accounts",
        sees_accounts(stack, agent, [f"acc_{user}_chk", f"acc_{user}_sav"]),
    )
    status, reply = listing(stack, agent)
    check("before revoke: tools/list works", status == 200 and "result" in (reply or {}))

    status, _ = bank_app.api("POST", "/agent/revoke", app, {})
    check("bank app revoke-agent: 200", status == 200)

    check(
        "after revoke: same token, tools/call refused with -32001",
        rpc_error_code(call_raw(stack, agent)) == -32001,
    )
    _, reply = listing(stack, agent)
    check(
        "after revoke: same token, tools/list refused with -32001", rpc_error_code(reply) == -32001
    )

    refreshed = stack.refresh_agent_token(tokens)
    check(
        "after revoke: a refreshed token is refused too (-32001)",
        rpc_error_code(call_raw(stack, refreshed)) == -32001,
    )
    check(
        "after revoke: sam's agent token still works",
        sees_accounts(stack, sam_agent, ["acc_sam_chk"]),
    )

    # Reconnecting the agent is a new sign-in (a later auth_time): that is the restore.
    again = stack.token_with_scope(user)
    check(
        f"new {user} sign-in after revoke: agent connection works again",
        sees_accounts(stack, again, [f"acc_{user}_chk", f"acc_{user}_sav"]),
    )
    check(
        "the old token stays dead after the reconnect",
        rpc_error_code(call_raw(stack, agent)) == -32001,
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: revoke_agent.py <user>   (alex)")
    run(lambda: main(sys.argv[1]))
