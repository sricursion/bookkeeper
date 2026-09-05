"""What an agent may propose, and which authority each proposal needs.

The kinds mirror the money-moving tools on the Razorpay MCP server, plus one
class for account administration: credentials, contact details, and the
settlement destination, which is where business-email-compromise fraud lands.
Params follow Razorpay's field names and units: amounts are integer minor
units (paise), payments are `pay_...` ids, payout destinations are `fa_...`
fund accounts.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .money import is_valid_amount


class ActionKind(str, Enum):
    CREATE_ORDER = "create_order"
    CREATE_PAYMENT_LINK = "create_payment_link"
    CAPTURE_PAYMENT = "capture_payment"
    CREATE_REFUND = "create_refund"
    CREATE_PAYOUT = "create_payout"
    UPDATE_ACCOUNT = "update_account"  # credentials, contact details, settlement destination


class AuthorityClass(str, Enum):
    COLLECT = "collect"  # money in: orders, payment links, captures
    REFUND = "refund"  # money out, back to a payer
    PAYOUT = "payout"  # money out, to a counterparty
    ADMIN = "admin"  # change who the account is and where its money goes


AUTHORITY_OF: Mapping[ActionKind, AuthorityClass] = {
    ActionKind.CREATE_ORDER: AuthorityClass.COLLECT,
    ActionKind.CREATE_PAYMENT_LINK: AuthorityClass.COLLECT,
    ActionKind.CAPTURE_PAYMENT: AuthorityClass.COLLECT,
    ActionKind.CREATE_REFUND: AuthorityClass.REFUND,
    ActionKind.CREATE_PAYOUT: AuthorityClass.PAYOUT,
    ActionKind.UPDATE_ACCOUNT: AuthorityClass.ADMIN,
}

MONEY_OUT = frozenset({AuthorityClass.REFUND, AuthorityClass.PAYOUT})

HAS_AMOUNT = frozenset(
    {
        ActionKind.CREATE_ORDER,
        ActionKind.CREATE_PAYMENT_LINK,
        ActionKind.CAPTURE_PAYMENT,
        ActionKind.CREATE_REFUND,
        ActionKind.CREATE_PAYOUT,
    }
)

SENSITIVE_KEYS = ("password", "secret", "token", "otp", "pin", "api_key")


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, unicode kept as is."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def redact(value: Any) -> Any:
    """Copy a params structure with credential-like values replaced. Used for
    everything written to the ledger; the fingerprint still covers the real values."""
    if isinstance(value, Mapping):
        return {
            k: ("[redacted]" if any(s in str(k).lower() for s in SENSITIVE_KEYS) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


@dataclass(frozen=True)
class ProposedAction:
    """A money action an agent wants to take. Nothing here is trusted.

    `rationale` is whatever the agent said about why. It is written to the
    ledger for the human reviewer and is never evaluated by any clause.
    """

    kind: ActionKind
    params: Mapping[str, Any]
    idempotency_key: str | None = None
    evidence: tuple[str, ...] = ()
    proposed_by: str = "agent"
    rationale: str = ""
    id: str = field(default_factory=lambda: f"act_{uuid.uuid4().hex[:12]}")
    proposed_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def authority(self) -> AuthorityClass:
        return AUTHORITY_OF[self.kind]

    @property
    def has_amount(self) -> bool:
        return self.kind in HAS_AMOUNT

    @property
    def amount(self) -> int | None:
        """The amount in minor units if it is a positive integer, else None."""
        if not self.has_amount:
            return None
        value = self.params.get("amount")
        return value if is_valid_amount(value) else None

    @property
    def currency(self) -> str | None:
        value = self.params.get("currency")
        return value if isinstance(value, str) and value else None

    def canonical(self) -> dict[str, Any]:
        """The identity of the request: kind, params, and attached evidence."""
        return {
            "kind": self.kind.value,
            "params": json.loads(canonical_json(self.params)),
            "evidence": sorted(self.evidence),
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.canonical()).encode("utf-8")).hexdigest()

    def target(self) -> str:
        """A short description of what the action touches, for reasons and logs."""
        if self.kind is ActionKind.CREATE_REFUND:
            return f" on {self.params.get('payment_id')}"
        if self.kind is ActionKind.CAPTURE_PAYMENT:
            return f" of {self.params.get('payment_id')}"
        if self.kind is ActionKind.CREATE_PAYOUT:
            return f" to {self.params.get('fund_account_id')}"
        if self.kind is ActionKind.UPDATE_ACCOUNT:
            fields = ", ".join(sorted(str(k) for k in self.params.keys())) or "no fields"
            return f" ({fields})"
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "params": redact(self.params),
            "idempotency_key": self.idempotency_key,
            "evidence": list(self.evidence),
            "proposed_by": self.proposed_by,
            "rationale": self.rationale,
            "proposed_at": self.proposed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, default_id: str | None = None, proposed_at: int | None = None) -> "ProposedAction":
        kwargs: dict[str, Any] = {
            "kind": ActionKind(data["kind"]),
            "params": dict(data.get("params", {})),
            "idempotency_key": data.get("idempotency_key"),
            "evidence": tuple(data.get("evidence", ())),
            "proposed_by": data.get("proposed_by", "agent"),
            "rationale": data.get("rationale", ""),
        }
        if data.get("id") or default_id:
            kwargs["id"] = data.get("id") or default_id
        if data.get("proposed_at") is not None:
            kwargs["proposed_at"] = int(data["proposed_at"])
        elif proposed_at is not None:
            kwargs["proposed_at"] = proposed_at
        return cls(**kwargs)
