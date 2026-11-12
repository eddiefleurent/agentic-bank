"""Limits written twice, in Python and in Terraform, must agree. A mismatch otherwise fails only
on AWS. proofs/money_wall.py proves the IAM side on AWS; these catch drift before a deploy."""

import re
from pathlib import Path

from agentic_bank.shared.transfers import MAX_CENTS
from agentic_bank.tools.disputes import CASE_ATTRIBUTES
from agentic_bank.tools.handler import propose_transfer

APP = Path(__file__).resolve().parents[1] / "infra/app"
# analyst_reason is not money, but the worker pays a staff refund only if it is on the case.
MONEY = {
    "balance_cents",
    "description",
    "channel",
    "refund_cents",
    "request_hash",
    "analyst_reason",
}


def tf_list(file: str, pattern: str) -> set[str]:
    block = re.search(pattern + r" = \[(.*?)\]", (APP / file).read_text(), re.S)
    assert block is not None
    return set(re.findall(r'"([a-z_]+)"', block[1]))


def test_case_writer_allowlist_matches_the_tools_and_excludes_money():
    in_terraform = tf_list("dispute.tf", "case_attributes")
    assert in_terraform == set(CASE_ATTRIBUTES)
    assert not MONEY & in_terraform


def test_proposer_allowlist_matches_what_propose_transfer_writes():
    class Table:
        def get_item(self, Key):  # noqa: N803
            return {"Item": {"name": "Jordan"}}

        def put_item(self, Item, **_):  # noqa: N803
            self.written = set(Item)

    proposer = Table()
    proposal = {"from_account_id": "a", "payee_id": "p", "amount_cents": 100, "memo": ""}
    propose_transfer(Table(), proposer, "alex", proposal)
    assert proposer.written == tf_list("data.tf", '"dynamodb:Attributes"')


def test_transfer_cap_is_the_same_in_code_cedar_and_the_tool_schema():
    assert f"context.input.amount_cents <= {MAX_CENTS}" in (APP / "policy.tf").read_text()
    assert f"1 to {MAX_CENTS} " in (APP / "gateway.tf").read_text()
