"""What the tool, the approvals API and the CLI must agree on: the transfer hash and its limits."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

MAX_CENTS = 100000
TRANSFER_ID = re.compile(r"^xfer_[0-9a-f]{16}$")


def now_iso(offset_seconds: int = 0) -> str:
    """UTC, whole seconds, `Z`: string order is time order. `2026-11-12T10:34:11Z`."""
    moment = datetime.now(UTC) + timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def transfer_hash(
    customer: str,
    transfer_id: str,
    from_account_id: str,
    payee_id: str,
    amount_cents: int | Decimal,
    memo: str,
    expires_at: str,
) -> str:
    """SHA-256 of the canonical JSON of everything the customer approves."""
    canonical = json.dumps(
        {
            "customer": customer,
            "transfer_id": transfer_id,
            "from_account_id": from_account_id,
            "payee_id": payee_id,
            "amount_cents": int(amount_cents),
            "memo": memo,
            "expires_at": expires_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
