"""Per-customer DynamoDB access. Money is integer cents in storage, USD strings on output."""

import json
import os
from decimal import Decimal
from typing import Any

import boto3


def format_cents(cents: int | Decimal) -> str:
    """12999 -> "129.99", -1299 -> "-12.99", 0 -> "0.00"."""
    return f"{Decimal(cents) / 100:.2f}"


READ = ["dynamodb:GetItem", "dynamodb:Query"]


def session_policy(
    table_arns: str | list[str],
    customer: str,
    actions: list[str] = READ,
    attributes: list[str] | None = None,
) -> str:
    """Inline policy for AssumeRole: only `actions`, only rows whose partition key is this customer
    (and, with `attributes`, only writes that name those attributes).

    AWS intersects it with the role's own policy, so even a bug in our code cannot read another
    customer's partition: DynamoDB answers AccessDeniedException.
    """
    only = {"dynamodb:LeadingKeys": [customer]}
    if attributes:
        only["dynamodb:Attributes"] = attributes
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": actions,
                    "Resource": table_arns,
                    "Condition": {
                        "ForAllValues:StringEquals": only,
                        "Null": {"dynamodb:LeadingKeys": "false"},
                    },
                }
            ],
        }
    )


def scoped_session(
    role_env: str,
    customer: str,
    table_arns: str | list[str],
    actions: list[str],
    attributes: list[str] | None = None,
    session_name: str | None = None,
) -> boto3.Session:
    """A session whose credentials can do `actions` on `customer`'s partition only.
    `session_name` is needed when the partition key holds characters STS refuses (such as #)."""
    creds = boto3.client("sts").assume_role(
        RoleArn=os.environ[role_env],
        RoleSessionName=session_name or f"c-{customer}",
        Policy=session_policy(table_arns, customer, actions, attributes),
        DurationSeconds=900,
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def scoped_dynamodb(
    role_env: str,
    customer: str,
    table_arns: str | list[str],
    actions: list[str],
    attributes: list[str] | None = None,
) -> Any:
    """A DynamoDB resource whose credentials can do `actions` on `customer`'s partition only."""
    return scoped_session(role_env, customer, table_arns, actions, attributes).resource("dynamodb")


def table_for(
    customer: str,
    role_env: str = "DATA_READER_ROLE_ARN",
    actions: list[str] = READ,
    attributes: list[str] | None = None,
):
    """The data table through credentials that can only see `customer`'s partition."""
    table_arn = os.environ["TABLE_ARN"]
    ddb = scoped_dynamodb(role_env, customer, table_arn, actions, attributes)
    return ddb.Table(table_arn.rpartition("/")[2])
