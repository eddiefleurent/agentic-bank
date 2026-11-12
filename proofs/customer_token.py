"""Proof: the agent reaches data only through a customer's token, and cannot choose the customer.

    uv run python proofs/customer_token.py

Needs the deployed stack, AWS credentials able to read SSM and Cognito, and the credential files
for alex, sam and jordan (scripts/setup_customer.py). Takes about two minutes: each sign-in waits
for a fresh TOTP window.
"""

from _common import Stack, call_tool, check, mcp_post, run


def accounts(stack: Stack, token: str) -> list[str]:
    _, out = call_tool(stack.gateway_url, token, "get_accounts", {})
    return [a["account_id"] for a in out.get("accounts", [])]


def main() -> None:
    stack = Stack()
    url = stack.gateway_url

    status, _ = mcp_post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, None)
    check("no token: gateway answers 401", status == 401)
    status, out = call_tool(url, None, "get_accounts", {})
    check("no token: tools/call returns no data", status == 401 and "accounts" not in out)

    weak = stack.token_without_scope("alex")
    _, out = call_tool(url, weak, "get_accounts", {})
    check(
        "valid alex token without the read scope: refused (Cedar), no data", "accounts" not in out
    )

    alex = stack.token_with_scope("alex")
    check(
        "alex with the read scope sees exactly his 2 accounts",
        sorted(accounts(stack, alex)) == ["acc_alex_chk", "acc_alex_sav"],
    )

    sam_sub = stack.subject("sam")
    _, out = call_tool(url, alex, "get_accounts", {"customer": sam_sub})
    ids = [a["account_id"] for a in out.get("accounts", [])]
    check(
        "alex sends customer=<sam's subject>: interceptor overwrites it, still alex's accounts",
        sorted(ids) == ["acc_alex_chk", "acc_alex_sav"],
    )
    _, out = call_tool(
        url, alex, "list_transactions", {"account_id": "acc_sam_chk", "customer": sam_sub}
    )
    check(
        "alex asks for acc_sam_chk with customer=<sam>: 'no such account'",
        out == {"error": "no such account"},
    )
    _, out = call_tool(url, alex, "list_transactions", {"account_id": "acc_sam_chk"})
    check(
        "alex asks for acc_sam_chk: 'no such account', not 'forbidden'",
        out == {"error": "no such account"},
    )
    _, out = call_tool(url, alex, "list_transactions", {"account_id": "acc_alex_chk"})
    check(
        "alex's own checking account returns transactions",
        bool(out.get("transactions")) and "error" not in out,
    )

    check(
        "sam sees exactly his 1 account",
        accounts(stack, stack.token_with_scope("sam")) == ["acc_sam_chk"],
    )
    check(
        "jordan sees no accounts (empty, not someone else's)",
        accounts(stack, stack.token_with_scope("jordan")) == [],
    )


if __name__ == "__main__":
    run(main)
