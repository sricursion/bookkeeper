"""The gate: evaluate one proposed action and write the decision down.

`evaluate` is pure. `Gate` wraps it with a ledger and a clock. Dispositions:
`allow` executes, `hold` waits for a person, `deny` never executes under this
mandate. A denial anywhere beats a hold anywhere.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .actions import AuthorityClass, ProposedAction
from .clauses import CLAUSES, DENY, HOLD, PASS, ClauseResult, EvalContext, PriorDecision, target_of
from .ledger import Ledger
from .mandate import Mandate
from .money import format_amount
from .records import RecordStore

ALLOW = "allow"


@dataclass(frozen=True)
class Decision:
    action_id: str
    mandate_id: str
    fingerprint: str
    disposition: str  # allow | hold | deny
    clauses: tuple[ClauseResult, ...]
    reason: str
    degraded: bool
    duplicate_of: int | None
    evaluated_at: int
    amount_named: bool = False  # the principal named this exact amount; it lifts the cap and does not count toward the per-target total

    @property
    def failing(self) -> tuple[ClauseResult, ...]:
        return tuple(c for c in self.clauses if c.failed)

    @property
    def failing_ids(self) -> tuple[str, ...]:
        return tuple(c.clause_id for c in self.failing)

    def summary(self) -> str:
        """The disposition and the clauses behind it, with no amounts, caps, or
        budgets. This is what an agent is told; the full reason stays in the ledger."""
        label = {ALLOW: "ALLOWED", HOLD: "HELD", DENY: "DENIED"}[self.disposition]
        if self.duplicate_of is not None:
            return f"{label} as a duplicate of an earlier identical request"
        if not self.failing:
            return f"{label} under mandate {self.mandate_id}"
        return f"{label}: " + "; ".join(f"{c.clause_id} {c.name}" for c in self.failing)

    def to_payload(self, action: ProposedAction) -> dict[str, Any]:
        budget_amount = action.params.get("budget_amount")
        return {
            "action": action.to_dict(),
            "mandate_id": self.mandate_id,
            "authority_class": action.authority.value,
            "amount": action.amount,
            "budget_amount": int(budget_amount) if isinstance(budget_amount, int) and not isinstance(budget_amount, bool) else None,
            "target": target_of(action),
            "amount_named": self.amount_named,
            "currency": action.currency,
            "idempotency_key": action.idempotency_key,
            "fingerprint": self.fingerprint,
            "evaluated_at": self.evaluated_at,
            "disposition": self.disposition,
            "duplicate_of": self.duplicate_of,
            "degraded": self.degraded,
            "clauses": [c.to_dict() for c in self.clauses],
            "reason": self.reason,
        }


def _reason(action: ProposedAction, disposition: str, clauses: tuple[ClauseResult, ...], mandate: Mandate) -> str:
    label = {ALLOW: "ALLOWED", HOLD: "HELD", DENY: "DENIED"}[disposition]
    amount = f" {format_amount(action.amount, action.currency)}" if action.amount is not None else ""
    head = f"{label}: {action.kind.value}{amount}{action.target()}"
    if disposition == ALLOW:
        passed = sum(1 for c in clauses if c.outcome == PASS)
        return f"{head}; all {passed} applicable clauses passed under mandate {mandate.id}"
    failing = [c for c in clauses if c.failed]
    return head + "; " + "; ".join(f"{c.clause_id} {c.name}: {c.detail}" for c in failing)


def evaluate(
    action: ProposedAction,
    mandate: Mandate,
    records: RecordStore,
    now: int,
    spend_today: Mapping[AuthorityClass, int] | None = None,
    prior: PriorDecision | None = None,
    spent_today_on_target: int = 0,
) -> Decision:
    """Evaluate one action. Pure: no clock, no file, no network."""
    spend_today = spend_today or {}
    fingerprint = action.fingerprint()

    if prior is not None and prior.fingerprint == fingerprint:
        reason = (
            f"DUPLICATE of ledger seq {prior.seq}: same idempotency key and identical request; "
            f"the prior disposition ({prior.disposition}) stands and nothing is executed twice"
        )
        return Decision(
            action_id=action.id,
            mandate_id=mandate.id,
            fingerprint=fingerprint,
            disposition=prior.disposition,
            clauses=(),
            reason=reason,
            degraded=False,
            duplicate_of=prior.seq,
            evaluated_at=now,
        )

    ctx = EvalContext(action=action, mandate=mandate, records=records, now=now, spend_today=spend_today, prior=prior, spent_today_on_target=spent_today_on_target)
    results = tuple(clause(ctx) for clause in CLAUSES)
    outcomes = {r.outcome for r in results}
    if DENY in outcomes:
        disposition = DENY
    elif HOLD in outcomes:
        disposition = HOLD
    else:
        disposition = ALLOW
    degraded = any(r.clause_id == "G14" and r.outcome == HOLD for r in results)
    return Decision(
        action_id=action.id,
        mandate_id=mandate.id,
        fingerprint=fingerprint,
        disposition=disposition,
        clauses=results,
        reason=_reason(action, disposition, results, mandate),
        degraded=degraded,
        duplicate_of=None,
        evaluated_at=now,
        amount_named=action.amount is not None and action.amount in mandate.named_amounts,
    )


class Gate:
    """The stateful wrapper: reads the ledger for context, writes the decision back."""

    def __init__(self, mandate: Mandate, records: RecordStore, ledger: Ledger, clock: Callable[[], int] | None = None):
        self.mandate = mandate
        self.records = records
        self.ledger = ledger
        self.clock = clock or (lambda: int(time.time()))

    def refresh_records(self, records: RecordStore) -> None:
        self.records = records

    def evaluate(self, action: ProposedAction, now: int | None = None) -> Decision:
        now = self.clock() if now is None else int(now)
        prior = self.ledger.prior_decision(action.idempotency_key)
        spend = {authority: self.ledger.spend_today(authority, now) for authority in AuthorityClass}
        on_target = self.ledger.spend_today_on_target(target_of(action), now)
        decision = evaluate(action, self.mandate, self.records, now, spend, prior, on_target)
        self.ledger.append("decision", decision.to_payload(action), ts=now)
        return decision

    def record_execution(self, decision: Decision, result: Mapping[str, Any], *, now: int | None = None) -> None:
        """Write what actually happened after an allowed action was executed."""
        now = self.clock() if now is None else int(now)
        self.ledger.append(
            "execution",
            {"action_id": decision.action_id, "fingerprint": decision.fingerprint, "result": dict(result), "executed_at": now},
            ts=now,
        )
