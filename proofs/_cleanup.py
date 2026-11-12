"""Reset alex's dispute fixtures so the dispute proofs can run again (admin credentials).

    uv run python proofs/_cleanup.py

Stops running workflows of the fixture cases, deletes their case rows, refund rows, REFUNDQ# slots
and vault rows, and takes each deleted refund back off the balance. AUDIT rows are kept. Each
refund row is deleted and subtracted in one transaction, so a rerun after a crash cannot subtract
the same refund twice.
"""

import boto3
from _common import REGION, Stack
from _dispute import ACCOUNT, MACHINE, TABLE, VAULT, case_id, ddb
from boto3.dynamodb.conditions import Attr, Key

FIXTURES = [f"t_alex_{n:03d}" for n in range(6, 12)]


def main() -> None:
    alex = Stack().subject("alex")
    table, cases = ddb().Table(TABLE), {case_id(t) for t in FIXTURES}
    sfn = boto3.client("stepfunctions", region_name=REGION)
    machine = next(
        m["stateMachineArn"]
        for m in sfn.list_state_machines()["stateMachines"]
        if m["name"] == MACHINE
    )
    stopped = 0
    for cid in cases:
        row = table.get_item(Key={"customer": alex, "sk": f"CASE#{cid}"}).get("Item")
        if not row:
            continue
        arn = machine.replace(":stateMachine:", ":execution:") + f":{row['execution']}"
        if sfn.describe_execution(executionArn=arn)["status"] == "RUNNING":
            sfn.stop_execution(executionArn=arn, cause="proof cleanup")
            stopped += 1
        table.delete_item(Key={"customer": alex, "sk": f"CASE#{cid}"})
    txns = table.query(
        KeyConditionExpression=Key("customer").eq(alex) & Key("sk").begins_with("TXN#"),
        ConsistentRead=True,
    )["Items"]
    refunds = [r for r in txns if "#r_" in r["sk"] and r["sk"].rsplit("#r_", 1)[-1] in cases]
    for r in refunds:
        cents = int(r["amount_cents"])  # the resource client serializes plain values
        table.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Delete": {
                        "TableName": TABLE,
                        "Key": {"customer": alex, "sk": r["sk"]},
                        "ConditionExpression": "attribute_exists(sk)",
                    }
                },
                {
                    "Update": {
                        "TableName": TABLE,
                        "Key": {"customer": alex, "sk": f"ACCT#{ACCOUNT}"},
                        "UpdateExpression": "SET balance_cents = balance_cents - :p",
                        "ExpressionAttributeValues": {":p": cents},
                    }
                },
            ]
        )
    removed, paid = len(refunds), sum(int(r["amount_cents"]) for r in refunds)
    for slot in ("REFUNDQ#1", "REFUNDQ#2"):
        table.delete_item(Key={"customer": alex, "sk": slot})
    vault, vaulted = ddb().Table(VAULT), 0
    for chunk in (list(cases)[i : i + 50] for i in range(0, len(cases), 50)):
        scan = {"FilterExpression": Attr("case_id").is_in(chunk), "ProjectionExpression": "#t"}
        scan["ExpressionAttributeNames"] = {"#t": "token"}
        while True:
            page = vault.scan(**scan)
            for item in page["Items"]:
                vault.delete_item(Key={"token": item["token"]})
                vaulted += 1
            if "LastEvaluatedKey" not in page:
                break
            scan["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    print(
        f"stopped {stopped} workflows; removed {removed} refunds (${paid / 100:.2f} taken "
        f"back), 2 slots, {vaulted} vault rows; cases reset: {len(cases)}"
    )


if __name__ == "__main__":
    main()
