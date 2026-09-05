"""The mandate: what the principal authorised, in the principal's own words.

Everything the gate trusts comes from here or from the merchant's records.
Named amounts, recipients, and payment ids are the things the principal wrote
in their own instruction. Nothing that arrived through a tool result can add
to this object.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .actions import AuthorityClass
from .timeutil import parse_ts


@dataclass(frozen=True)
class Limits:
    per_action_cap: int  # paise; above this the action is held unless the amount was named
    daily_budget: int  # paise; allowed actions today, this class, may not exceed this
    evidence_above: int  # paise; above this at least one evidence reference is required
    human_approval_above: int  # paise; above this a person must approve, always

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Limits":
        return cls(
            per_action_cap=int(data["per_action_cap"]),
            daily_budget=int(data["daily_budget"]),
            evidence_above=int(data["evidence_above"]),
            human_approval_above=int(data["human_approval_above"]),
        )


@dataclass(frozen=True)
class Mandate:
    id: str
    principal: str
    issued_at: int
    valid_from: int
    valid_until: int
    scopes: frozenset[AuthorityClass]
    limits: Mapping[AuthorityClass, Limits]
    named_amounts: frozenset[int] = frozenset()
    named_recipients: frozenset[str] = frozenset()
    named_payment_ids: frozenset[str] = frozenset()
    named_fields: frozenset[str] = frozenset()  # account fields the principal asked to change
    named_values: Mapping[str, str] = field(default_factory=dict)  # values the principal gave for those fields
    named_recipient_amounts: Mapping[str, frozenset[int]] = field(default_factory=dict)  # amounts the principal named with a specific new recipient
    allowed_currencies: frozenset[str] = frozenset({"INR"})
    active_hours_ist: tuple[int, int] = (9, 21)  # money-out allowed when start <= hour < end
    max_records_age_seconds: int = 900

    def limits_for(self, authority: AuthorityClass) -> Limits | None:
        return self.limits.get(authority)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Mandate":
        limits = {AuthorityClass(name): Limits.from_dict(value) for name, value in data.get("limits", {}).items()}
        hours = data.get("active_hours_ist", [9, 21])
        return cls(
            id=str(data["id"]),
            principal=str(data.get("principal", "")),
            issued_at=parse_ts(data.get("issued_at", data["valid_from"])),
            valid_from=parse_ts(data["valid_from"]),
            valid_until=parse_ts(data["valid_until"]),
            scopes=frozenset(AuthorityClass(s) for s in data.get("scopes", [])),
            limits=limits,
            named_amounts=frozenset(int(a) for a in data.get("named_amounts", [])),
            named_recipients=frozenset(str(r) for r in data.get("named_recipients", [])),
            named_payment_ids=frozenset(str(p) for p in data.get("named_payment_ids", [])),
            named_fields=frozenset(str(f) for f in data.get("named_fields", [])),
            named_values={str(k): str(v) for k, v in data.get("named_values", {}).items()},
            named_recipient_amounts={str(k): frozenset(int(a) for a in v) for k, v in data.get("named_recipient_amounts", {}).items()},
            allowed_currencies=frozenset(str(c) for c in data.get("allowed_currencies", ["INR"])),
            active_hours_ist=(int(hours[0]), int(hours[1])),
            max_records_age_seconds=int(data.get("max_records_age_seconds", 900)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "principal": self.principal,
            "issued_at": self.issued_at,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "scopes": sorted(s.value for s in self.scopes),
            "limits": {k.value: vars(v) for k, v in self.limits.items()},
            "named_amounts": sorted(self.named_amounts),
            "named_recipients": sorted(self.named_recipients),
            "named_payment_ids": sorted(self.named_payment_ids),
            "named_fields": sorted(self.named_fields),
            "named_values": dict(self.named_values),
            "named_recipient_amounts": {k: sorted(v) for k, v in self.named_recipient_amounts.items()},
            "allowed_currencies": sorted(self.allowed_currencies),
            "active_hours_ist": list(self.active_hours_ist),
            "max_records_age_seconds": self.max_records_age_seconds,
        }


def load_mandates(path: str | Path) -> dict[str, Mandate]:
    """Load one mandate or a list of mandates from a JSON file, keyed by id."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else [data]
    mandates = [Mandate.from_dict(item) for item in items]
    return {m.id: m for m in mandates}
