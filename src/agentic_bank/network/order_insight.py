"""Mock card-network "Order Insight": what the merchant's order says about a card charge.

`OrderInsight` is the seam. A real scheme service is an HTTPS client (mTLS, egress control,
timeouts) behind the same `lookup`; callers do not change. Here it reads `orders.json`.
Merchant and item text is typed by outsiders: it only ever travels under `*_untrusted` keys.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DELIVERY = {"not_shipped", "in_transit", "delivered", "delivered_signed"}
THREE_DS = {"authenticated", "not_performed"}
CHANNEL = {"digital", "shipped"}
TEXT_LIMIT = 200


@dataclass(frozen=True)
class Order:
    merchant_untrusted: str
    items_untrusted: list[str]
    channel: str
    delivery: str
    device_type: str
    device_id: str
    device_prior_orders: int
    three_ds: str
    delivered_on: str | None = None

    def to_facts(self) -> dict:
        """Safe-to-show form: text cut, coded fields forced into fixed enums, device masked."""
        return {
            "merchant_untrusted": self.merchant_untrusted[:TEXT_LIMIT],
            "items_untrusted": [i[:TEXT_LIMIT] for i in self.items_untrusted[:10]],
            "channel": self.channel if self.channel in CHANNEL else "unknown",
            "delivery": self.delivery if self.delivery in DELIVERY else "unknown",
            "delivered_on": self.delivered_on,
            "device_type": self.device_type[:20],
            "device_last4": self.device_id[-4:],
            "device_prior_orders": self.device_prior_orders,
            "three_ds": self.three_ds if self.three_ds in THREE_DS else "unknown",
        }


class OrderInsight(Protocol):
    def lookup(self, network_ref: str) -> Order | None: ...


ORDERS = json.loads(Path(__file__).with_name("orders.json").read_text())


class FixtureOrderInsight:
    def lookup(self, network_ref: str) -> Order | None:
        raw = ORDERS.get(network_ref)
        return Order(**raw) if raw else None
