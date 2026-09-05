"""The clauses. Each is a pure function of the proposed action, the mandate,
the records, the clock, and what the ledger already shows for today.

Outcomes: `pass`, `hold` (a person decides), `deny` (never under this
mandate), `skip` (not applicable to this action). A denial is for requests
that are impossible or malformed; a hold is for requests that are possible but
outside what the principal pre-authorised. None of these functions look at the
agent's rationale or at anything the agent read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .actions import MONEY_OUT, ActionKind, AuthorityClass, ProposedAction
from .mandate import Mandate
from .money import format_amount
from .records import RecordStore
from .timeutil import fmt, ist

PASS, HOLD, DENY, SKIP = "pass", "hold", "deny", "skip"


@dataclass(frozen=True)
class ClauseResult:
    clause_id: str
    name: str
    outcome: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.outcome in (HOLD, DENY)

    def to_dict(self) -> dict[str, str]:
        return {"id": self.clause_id, "name": self.name, "outcome": self.outcome, "detail": self.detail}


@dataclass(frozen=True)
class PriorDecision:
    """What the ledger already holds for this idempotency key."""

    seq: int
    fingerprint: str
    disposition: str


def target_of(action: ProposedAction) -> str | None:
    """What the money lands on: the payment for refunds and captures, the fund account for payouts."""
    if action.kind in (ActionKind.CREATE_REFUND, ActionKind.CAPTURE_PAYMENT):
        value = action.params.get("payment_id")
    elif action.kind is ActionKind.CREATE_PAYOUT:
        value = action.params.get("fund_account_id")
    else:
        return None
    return str(value) if value else None


@dataclass(frozen=True)
class EvalContext:
    action: ProposedAction
    mandate: Mandate
    records: RecordStore
    now: int
    spend_today: Mapping[AuthorityClass, int]
    prior: PriorDecision | None
    spent_today_on_target: int = 0  # allowed today against the same payment or recipient

    @property
    def amount(self) -> int | None:
        return self.action.amount

    @property
    def limits(self):
        return self.mandate.limits_for(self.action.authority)

    def money(self, minor: int) -> str:
        return format_amount(minor, self.action.currency)


Clause = Callable[[EvalContext], ClauseResult]


def _result(clause_id: str, name: str, outcome: str, detail: str) -> ClauseResult:
    return ClauseResult(clause_id, name, outcome, detail)


# G00 ------------------------------------------------------------------------

def g00_well_formed(ctx: EvalContext) -> ClauseResult:
    a = ctx.action
    problems: list[str] = []
    if a.has_amount:
        if a.amount is None:
            problems.append("amount must be a positive integer number of minor units (paise)")
        if a.currency is None:
            problems.append("currency is missing")
    if a.kind in (ActionKind.CREATE_REFUND, ActionKind.CAPTURE_PAYMENT) and not a.params.get("payment_id"):
        problems.append(f"{a.kind.value} needs a payment_id")
    if a.kind is ActionKind.CREATE_PAYOUT and not a.params.get("fund_account_id"):
        problems.append("create_payout needs a fund_account_id")
    if a.kind is ActionKind.UPDATE_ACCOUNT and not any(v not in (None, "") for v in a.params.values()):
        problems.append("update_account changes nothing")
    if problems:
        return _result("G00", "well formed", DENY, "; ".join(problems))
    return _result("G00", "well formed", PASS, "required fields present")


# G01 ------------------------------------------------------------------------

def g01_scope(ctx: EvalContext) -> ClauseResult:
    authority = ctx.action.authority
    if authority not in ctx.mandate.scopes:
        granted = ", ".join(sorted(s.value for s in ctx.mandate.scopes)) or "none"
        return _result(
            "G01", "authority in scope", DENY,
            f"{authority.value} authority was not granted by mandate {ctx.mandate.id} (granted: {granted})",
        )
    return _result("G01", "authority in scope", PASS, f"{authority.value} authority granted by mandate {ctx.mandate.id}")


# G02 ------------------------------------------------------------------------

def g02_mandate_window(ctx: EvalContext) -> ClauseResult:
    m = ctx.mandate
    if not (m.valid_from <= ctx.now < m.valid_until):
        return _result(
            "G02", "mandate in force", DENY,
            f"{fmt(ctx.now)} is outside the mandate window {fmt(m.valid_from)} to {fmt(m.valid_until)}",
        )
    return _result("G02", "mandate in force", PASS, f"mandate valid until {fmt(m.valid_until)}")


# G03 ------------------------------------------------------------------------

def g03_currency(ctx: EvalContext) -> ClauseResult:
    if not ctx.action.has_amount:
        return _result("G03", "currency allowed", SKIP, "no money moves")
    currency = ctx.action.currency
    if currency is None:
        return _result("G03", "currency allowed", SKIP, "no currency to check (see G00)")
    if currency not in ctx.mandate.allowed_currencies:
        allowed = ", ".join(sorted(ctx.mandate.allowed_currencies))
        return _result("G03", "currency allowed", DENY, f"{currency} is not an allowed currency (allowed: {allowed})")
    return _result("G03", "currency allowed", PASS, f"{currency} allowed")


# G04 ------------------------------------------------------------------------

def g04_idempotency(ctx: EvalContext) -> ClauseResult:
    key = ctx.action.idempotency_key
    if not key:
        return _result("G04", "idempotency key", DENY, "no idempotency key; every action must carry one")
    if ctx.prior is not None and ctx.prior.fingerprint != ctx.action.fingerprint():
        return _result(
            "G04", "idempotency key", DENY,
            f"key {key} was already used for a different request (ledger seq {ctx.prior.seq}); a changed request needs a new key",
        )
    return _result("G04", "idempotency key", PASS, f"key {key} not previously used")


# G05 ------------------------------------------------------------------------

def g05_refund_grounded(ctx: EvalContext) -> ClauseResult:
    name = "refund grounded in a captured payment"
    if ctx.action.kind is not ActionKind.CREATE_REFUND:
        return _result("G05", name, SKIP, "not a refund")
    payment_id = ctx.action.params.get("payment_id")
    if not payment_id or ctx.amount is None:
        return _result("G05", name, SKIP, "nothing to ground (see G00)")
    payment = ctx.records.payments.get(str(payment_id))
    if payment is None:
        return _result("G05", name, DENY, f"payment {payment_id} is not in the merchant's records")
    if payment.status != "captured":
        return _result("G05", name, DENY, f"payment {payment_id} is {payment.status}; only captured payments can be refunded")
    if ctx.amount > payment.refundable:
        return _result(
            "G05", name, DENY,
            f"refund {ctx.money(ctx.amount)} exceeds refundable {ctx.money(payment.refundable)} on {payment_id} "
            f"(captured {ctx.money(payment.amount)}, already refunded {ctx.money(payment.amount_refunded)})",
        )
    return _result("G05", name, PASS, f"refund {ctx.money(ctx.amount)} within refundable {ctx.money(payment.refundable)} on {payment_id}")


# G06 ------------------------------------------------------------------------

def g06_capture_grounded(ctx: EvalContext) -> ClauseResult:
    name = "capture grounded in an authorised payment"
    if ctx.action.kind is not ActionKind.CAPTURE_PAYMENT:
        return _result("G06", name, SKIP, "not a capture")
    payment_id = ctx.action.params.get("payment_id")
    if not payment_id or ctx.amount is None:
        return _result("G06", name, SKIP, "nothing to ground (see G00)")
    payment = ctx.records.payments.get(str(payment_id))
    if payment is None:
        return _result("G06", name, DENY, f"payment {payment_id} is not in the merchant's records")
    if payment.status != "authorized":
        return _result("G06", name, DENY, f"payment {payment_id} is {payment.status}; only authorised payments can be captured")
    if ctx.amount != payment.amount:
        return _result(
            "G06", name, DENY,
            f"capture {ctx.money(ctx.amount)} does not equal the authorised {ctx.money(payment.amount)} on {payment_id}; "
            "Razorpay captures the full authorised amount",
        )
    return _result("G06", name, PASS, f"capture of the full authorised {ctx.money(payment.amount)} on {payment_id}")


# G07 ------------------------------------------------------------------------

def g07_counterparty_established(ctx: EvalContext) -> ClauseResult:
    name = "payout recipient established"
    if ctx.action.kind is not ActionKind.CREATE_PAYOUT:
        return _result("G07", name, SKIP, "not a payout")
    fund_account_id = ctx.action.params.get("fund_account_id")
    if not fund_account_id:
        return _result("G07", name, SKIP, "no recipient to check (see G00)")
    fund_account_id = str(fund_account_id)
    known = ctx.records.counterparties.get(fund_account_id)
    if known is not None and known.payouts_count > 0:
        return _result("G07", name, PASS, f"{fund_account_id} ({known.name}) has {known.payouts_count} prior payouts since {fmt(known.first_seen)}")
    if fund_account_id in ctx.mandate.named_recipients:
        # A recipient with no history is paid only what the principal named for that recipient.
        # Amounts named alongside a specific recipient bind to it; otherwise any named amount will do.
        bound = ctx.mandate.named_recipient_amounts.get(fund_account_id)
        allowed = bound if bound is not None else ctx.mandate.named_amounts
        if ctx.amount is not None and ctx.amount not in allowed:
            return _result(
                "G07", name, HOLD,
                f"{fund_account_id} was named by the principal but has no payout history; the first payout to a new "
                f"counterparty must be for an amount the principal named with it, and {ctx.money(ctx.amount)} was not",
            )
        return _result("G07", name, PASS, f"{fund_account_id} was named by the principal, for an amount the principal named with it")
    return _result(
        "G07", name, HOLD,
        f"fund account {fund_account_id} has no payout history with this merchant and was not named by the principal",
    )


# G08 ------------------------------------------------------------------------

def g08_per_action_cap(ctx: EvalContext) -> ClauseResult:
    name = "within per-action cap"
    if ctx.amount is None or ctx.limits is None:
        return _result("G08", name, SKIP, "no amount or no limits for this authority")
    cap = ctx.limits.per_action_cap
    if ctx.amount in ctx.mandate.named_amounts:
        return _result("G08", name, PASS, f"{ctx.money(ctx.amount)} was named by the principal")
    if ctx.amount > cap:
        return _result("G08", name, HOLD, f"{ctx.money(ctx.amount)} is above the {ctx.action.authority.value} per-action cap of {ctx.money(cap)}")
    # The cap is a per-target cap for the day, so it cannot be dodged by splitting one large action into several.
    if ctx.spent_today_on_target + ctx.amount > cap:
        return _result(
            "G08", name, HOLD,
            f"{ctx.money(ctx.spent_today_on_target)} already allowed today against {target_of(ctx.action)} plus {ctx.money(ctx.amount)} "
            f"would exceed the per-action cap of {ctx.money(cap)}; a cap cannot be split around",
        )
    return _result("G08", name, PASS, f"{ctx.money(ctx.amount)} within the per-action cap of {ctx.money(cap)}, {ctx.money(ctx.spent_today_on_target)} already allowed today on the same target")


# G09 ------------------------------------------------------------------------

def g09_daily_budget(ctx: EvalContext) -> ClauseResult:
    name = "within daily budget"
    if ctx.amount is None or ctx.limits is None:
        return _result("G09", name, SKIP, "no amount or no limits for this authority")
    spent = ctx.spend_today.get(ctx.action.authority, 0)
    budget = ctx.limits.daily_budget
    if spent + ctx.amount > budget:
        return _result(
            "G09", name, HOLD,
            f"{ctx.action.authority.value} allowed today {ctx.money(spent)} plus {ctx.money(ctx.amount)} would exceed the daily budget of {ctx.money(budget)}",
        )
    return _result("G09", name, PASS, f"{ctx.money(spent)} allowed today plus {ctx.money(ctx.amount)} within the daily budget of {ctx.money(budget)}")


# G10 ------------------------------------------------------------------------

def g10_evidence_required(ctx: EvalContext) -> ClauseResult:
    name = "evidence attached above threshold"
    if ctx.amount is None or ctx.limits is None:
        return _result("G10", name, SKIP, "no amount or no limits for this authority")
    threshold = ctx.limits.evidence_above
    if ctx.amount > threshold and not ctx.action.evidence:
        return _result("G10", name, HOLD, f"{ctx.money(ctx.amount)} is above {ctx.money(threshold)} and no evidence reference is attached")
    if ctx.amount > threshold:
        return _result("G10", name, PASS, f"evidence attached: {', '.join(ctx.action.evidence)}")
    return _result("G10", name, PASS, f"{ctx.money(ctx.amount)} is at or below the evidence threshold of {ctx.money(threshold)}")


# G11 ------------------------------------------------------------------------

def g11_human_approval(ctx: EvalContext) -> ClauseResult:
    name = "below human-approval threshold"
    if ctx.amount is None or ctx.limits is None:
        return _result("G11", name, SKIP, "no amount or no limits for this authority")
    threshold = ctx.limits.human_approval_above
    if ctx.amount > threshold:
        return _result("G11", name, HOLD, f"{ctx.money(ctx.amount)} is above {ctx.money(threshold)}; a person must approve this one regardless of what was named")
    return _result("G11", name, PASS, f"{ctx.money(ctx.amount)} is at or below the human-approval threshold of {ctx.money(threshold)}")


# G12 ------------------------------------------------------------------------

def g12_active_hours(ctx: EvalContext) -> ClauseResult:
    name = "money-out within active hours"
    if ctx.action.authority not in MONEY_OUT:
        return _result("G12", name, SKIP, "not a money-out action")
    start, end = ctx.mandate.active_hours_ist
    hour = ist(ctx.now).hour
    if not (start <= hour < end):
        return _result(
            "G12", name, HOLD,
            f"{fmt(ctx.now)} is outside active hours {start:02d}:00 to {end:02d}:00 IST for money-out actions",
        )
    return _result("G12", name, PASS, f"{fmt(ctx.now)} is within active hours {start:02d}:00 to {end:02d}:00 IST")


# G13 ------------------------------------------------------------------------

FREE_TEXT_FIELDS = ("notes", "description", "narration", "purpose", "receipt", "reference_id", "subject", "callback_url")

# A UPI handle has a letters-only suffix with no dot (name@ybl, name@upi);
# an email address has a dotted domain (name@example.com) and is not matched.
_VPA = re.compile(r"(?<![\w.])[\w.\-]{2,}@[a-zA-Z]{2,}(?![\w.])")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_ACCOUNT = re.compile(r"(?<!\d)\d{9,18}(?!\d)")
_IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")


def _strings_in(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings_in(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings_in(item)


def payment_instructions_in(text: str) -> list[str]:
    """Tokens in free text that route money: UPI handles, URLs, account numbers, IFSC codes, IBANs."""
    found: list[str] = []
    for pattern in (_VPA, _URL, _ACCOUNT, _IFSC, _IBAN):
        found.extend(pattern.findall(text))
    return found


def g13_free_text_clean(ctx: EvalContext) -> ClauseResult:
    name = "free text carries no payment instructions"
    tokens: list[str] = []
    for field_name in FREE_TEXT_FIELDS:
        if field_name in ctx.action.params:
            for text in _strings_in(ctx.action.params[field_name]):
                tokens.extend(t for t in payment_instructions_in(text) if t not in ctx.mandate.named_recipients)
    if tokens:
        shown = ", ".join(dict.fromkeys(tokens))
        return _result("G13", name, HOLD, f"free text contains payment routing the principal did not name: {shown}")
    return _result("G13", name, PASS, "no UPI handles, links, account numbers, or IFSC codes in free text")


# G14 ------------------------------------------------------------------------

def g14_records_fresh(ctx: EvalContext) -> ClauseResult:
    name = "records fresh enough to ground money-out"
    if ctx.action.authority not in MONEY_OUT:
        return _result("G14", name, SKIP, "not a money-out action")
    max_age = ctx.mandate.max_records_age_seconds
    age = ctx.records.age(ctx.now)
    if ctx.records.is_stale(ctx.now, max_age):
        return _result(
            "G14", name, HOLD,
            f"DEGRADED: records were fetched {age // 60} minutes ago, older than the {max_age // 60} minute limit; money-out is held until records refresh",
        )
    return _result("G14", name, PASS, f"records fetched {max(age, 0) // 60} minutes ago, within the {max_age // 60} minute limit")


# G15 ------------------------------------------------------------------------

def g15_account_change_named(ctx: EvalContext) -> ClauseResult:
    name = "account change limited to what the principal named"
    if ctx.action.kind is not ActionKind.UPDATE_ACCOUNT:
        return _result("G15", name, SKIP, "not an account change")
    if AuthorityClass.ADMIN not in ctx.mandate.scopes:
        return _result("G15", name, SKIP, "no admin authority to refine (see G01)")
    changed = {str(k): v for k, v in ctx.action.params.items() if v not in (None, "")}
    if not changed:
        return _result("G15", name, SKIP, "nothing changes (see G00)")
    unnamed = sorted(k for k in changed if k not in ctx.mandate.named_fields)
    if unnamed:
        return _result("G15", name, HOLD, f"the principal did not ask to change: {', '.join(unnamed)}")
    wrong = sorted(k for k, v in changed.items() if k in ctx.mandate.named_values and str(v) != ctx.mandate.named_values[k])
    if wrong:
        return _result("G15", name, HOLD, f"the principal named a different value for: {', '.join(wrong)}")
    return _result("G15", name, PASS, f"changes only fields the principal named: {', '.join(sorted(changed))}")


# G16 ------------------------------------------------------------------------

def g16_recurring_named(ctx: EvalContext) -> ClauseResult:
    name = "recurring schedule asked for by the principal"
    if ctx.action.kind is not ActionKind.CREATE_PAYOUT or not ctx.action.params.get("recurring"):
        return _result("G16", name, SKIP, "not a recurring payout")
    if "recurring" in ctx.mandate.named_fields:
        return _result("G16", name, PASS, "the principal asked for a standing or recurring payment")
    return _result("G16", name, HOLD, "a recurring payout was not asked for by the principal; a standing instruction needs their word")


CLAUSES: tuple[Clause, ...] = (
    g00_well_formed,
    g01_scope,
    g02_mandate_window,
    g03_currency,
    g04_idempotency,
    g05_refund_grounded,
    g06_capture_grounded,
    g07_counterparty_established,
    g08_per_action_cap,
    g09_daily_budget,
    g10_evidence_required,
    g11_human_approval,
    g12_active_hours,
    g13_free_text_clean,
    g14_records_fresh,
    g15_account_change_named,
    g16_recurring_named,
)
