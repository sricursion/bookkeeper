"""The merchant's own records: the second trusted source.

The gate fetches these itself from the merchant's Razorpay account (or loads
them from a file in tests). They are never taken from the agent. A refund is
grounded in a captured payment that exists here; a payout is grounded in a
counterparty that has a history here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .timeutil import parse_ts

PAYMENT_STATUSES = ("created", "authorized", "captured", "refunded", "failed")


@dataclass(frozen=True)
class Payment:
    id: str
    status: str  # Razorpay payment status
    amount: int  # paise
    amount_refunded: int = 0  # paise already refunded
    currency: str = "INR"
    customer_id: str | None = None
    captured_at: int | None = None

    @property
    def refundable(self) -> int:
        return max(self.amount - self.amount_refunded, 0)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Payment":
        status = str(data["status"])
        if status not in PAYMENT_STATUSES:
            raise ValueError(f"unknown payment status {status!r} for {data.get('id')}")
        captured_at = data.get("captured_at")
        return cls(
            id=str(data["id"]),
            status=status,
            amount=int(data["amount"]),
            amount_refunded=int(data.get("amount_refunded", 0)),
            currency=str(data.get("currency", "INR")),
            customer_id=data.get("customer_id"),
            captured_at=parse_ts(captured_at) if captured_at is not None else None,
        )


@dataclass(frozen=True)
class Counterparty:
    fund_account_id: str
    contact_id: str
    name: str
    first_seen: int
    payouts_count: int = 0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Counterparty":
        return cls(
            fund_account_id=str(data["fund_account_id"]),
            contact_id=str(data.get("contact_id", "")),
            name=str(data.get("name", "")),
            first_seen=parse_ts(data["first_seen"]),
            payouts_count=int(data.get("payouts_count", 0)),
        )


@dataclass(frozen=True)
class RecordStore:
    fetched_at: int
    payments: Mapping[str, Payment] = field(default_factory=dict)
    counterparties: Mapping[str, Counterparty] = field(default_factory=dict)
    source: str = "file"

    def age(self, now: int) -> int:
        return now - self.fetched_at

    def is_stale(self, now: int, max_age_seconds: int) -> bool:
        return self.age(now) > max_age_seconds

    def with_fetched_at(self, fetched_at: int) -> "RecordStore":
        return replace(self, fetched_at=fetched_at)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecordStore":
        payments = {p["id"]: Payment.from_dict(p) for p in data.get("payments", [])}
        counterparties = {c["fund_account_id"]: Counterparty.from_dict(c) for c in data.get("counterparties", [])}
        return cls(
            fetched_at=parse_ts(data["fetched_at"]),
            payments=payments,
            counterparties=counterparties,
            source=str(data.get("source", "file")),
        )

    @classmethod
    def load(cls, path: str | Path) -> "RecordStore":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
