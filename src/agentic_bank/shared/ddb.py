"""Low-level DynamoDB helpers for settlement, the worker and the tools: typed values, reads."""

from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

_ser, _de = TypeSerializer(), TypeDeserializer()


def ser(value: Any) -> dict[str, Any]:
    """Python value -> DynamoDB typed value ({"S": ...})."""
    return _ser.serialize(value)


def deser(item: dict[str, Any]) -> dict[str, Any]:
    """DynamoDB typed item -> plain dict."""
    return {k: _de.deserialize(v) for k, v in item.items()}


def get(client: Any, table: str, customer: str, sk: str) -> dict[str, Any] | None:
    """One row, strongly consistent, or None."""
    item = client.get_item(
        TableName=table, Key={"customer": ser(customer), "sk": ser(sk)}, ConsistentRead=True
    ).get("Item")
    return deser(item) if item else None
