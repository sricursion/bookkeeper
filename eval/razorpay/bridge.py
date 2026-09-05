"""From a Razorpay tool call to proposed money actions, and from the account to records.

The tool names and argument shapes are the official MCP server's. One tool
call can become more than one action: an order with Route transfers is an
order plus one payout per linked account, and every one of them has to pass.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from typing import Any

from munim import ActionKind, AuthorityClass, Counterparty, Mandate, Payment, ProposedAction, RecordStore
from munim.money import minor_from_major

CONSEQUENTIAL = frozenset(
    {"create_refund", "capture_payment", "create_payment_link", "payment_link_upi_create", "create_order", "create_instant_settlement", "revoke_token", "initiate_payment"}
)
SELF_SETTLEMENT = "self_settlement"

_PAYMENT_ID = re.compile(r"\bpay_[A-Za-z0-9]{6,}\b")
_ACCOUNT_ID = re.compile(r"\bacc_[A-Za-z0-9]{6,}\b")
_URL = re.compile(r"https?://\S+")
_RUPEES = re.compile(r"(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE)
_RUPEES_AFTER = re.compile(r"\b([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:rupees|rs)\b", re.IGNORECASE)


EVIDENCE_WORDS = ("ticket", "evidence", "reference", "approval", "invoice", "ref")


def _evidence(a: dict[str, Any]) -> tuple[str, ...]:
    """The real tools have no evidence field. A note whose key names a ticket,
    approval, invoice, or reference is the evidence. A receipt is not: the
    agent invents receipts, so it cannot vouch for anything."""
    notes = a.get("notes") or {}
    if not isinstance(notes, dict):
        return ()
    return tuple(str(v) for k, v in notes.items() if v and any(word in str(k).lower() for word in EVIDENCE_WORDS))


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")


def rupee_amounts_in(text: str) -> set[int]:
    amounts: set[int] = set()
    for pattern in (_RUPEES, _RUPEES_AFTER):
        for token in pattern.findall(text):
            try:
                amounts.add(minor_from_major(token.replace(",", "")))
            except Exception:
                continue
    return amounts


def mandate_for_task(base: Mandate, principal_text: str, now: int) -> Mandate:
    """The day's mandate, plus whatever the principal named in this task. A
    linked account named in a sentence is bound to the rupee amounts written
    in that same sentence, so an amount named elsewhere cannot be paid to it."""
    amounts = set(base.named_amounts) | rupee_amounts_in(principal_text)
    recipients = set(base.named_recipients) | set(_URL.findall(principal_text))
    bound: dict[str, set[int]] = {k: set(v) for k, v in base.named_recipient_amounts.items()}
    for sentence in _SENTENCE_SPLIT.split(principal_text):
        accounts = set(_ACCOUNT_ID.findall(sentence))
        if not accounts:
            continue
        amounts_here = rupee_amounts_in(sentence)
        for account in accounts:
            recipients.add(account)
            bound.setdefault(account, set()).update(amounts_here)
    payment_ids = set(base.named_payment_ids) | set(_PAYMENT_ID.findall(principal_text))
    return replace(
        base,
        named_amounts=frozenset(amounts),
        named_recipients=frozenset(recipients),
        named_recipient_amounts={k: frozenset(v) for k, v in bound.items()},
        named_payment_ids=frozenset(payment_ids),
        valid_from=min(base.valid_from, now - 60),
    )


def records_from_payments(payload: Any, now: int, source: str) -> RecordStore:
    """Build records from a fetch_all_payments response (real or simulated)."""
    items = payload.get("items", []) if isinstance(payload, dict) else []
    payments: dict[str, Payment] = {}
    for item in items:
        try:
            payments[item["id"]] = Payment(
                id=str(item["id"]),
                status=str(item.get("status", "created")),
                amount=int(item.get("amount", 0)),
                amount_refunded=int(item.get("amount_refunded") or 0),
                currency=str(item.get("currency", "INR")),
                customer_id=item.get("customer_id"),
                captured_at=int(item["created_at"]) if item.get("created_at") else None,
            )
        except (KeyError, ValueError, TypeError):
            continue
    counterparties = {SELF_SETTLEMENT: Counterparty(fund_account_id=SELF_SETTLEMENT, contact_id="self", name="merchant settlement account", first_seen=now - 86400 * 365, payouts_count=1)}
    return RecordStore(fetched_at=now, payments=payments, counterparties=counterparties, source=source)


def actions_from_call(name: str, args: dict[str, Any], idempotency_key: str) -> list[ProposedAction]:
    """Translate one consequential tool call into the money actions it implies."""
    a = dict(args)
    currency = str(a.get("currency") or "INR")

    if name == "create_refund":
        params = {"amount": _int(a.get("amount")), "currency": currency, "payment_id": a.get("payment_id"), "notes": a.get("notes") or {}, "receipt": a.get("receipt")}
        return [ProposedAction(kind=ActionKind.CREATE_REFUND, params=params, idempotency_key=idempotency_key, evidence=_evidence(a), rationale=f"tool call {name}")]

    if name == "capture_payment":
        params = {"amount": _int(a.get("amount")), "currency": currency, "payment_id": a.get("payment_id")}
        return [ProposedAction(kind=ActionKind.CAPTURE_PAYMENT, params=params, idempotency_key=idempotency_key, rationale=f"tool call {name}")]

    if name in ("create_payment_link", "payment_link_upi_create"):
        params = {
            "amount": _int(a.get("amount")), "currency": currency, "description": a.get("description") or "", "reference_id": a.get("reference_id"),
            "notes": a.get("notes") or {}, "callback_url": a.get("callback_url"),
            "customer": {k[len("customer_"):]: a[k] for k in ("customer_name", "customer_email", "customer_contact") if a.get(k)},
        }
        return [ProposedAction(kind=ActionKind.CREATE_PAYMENT_LINK, params=params, idempotency_key=idempotency_key, rationale=f"tool call {name}")]

    if name == "create_order":
        params = {"amount": _int(a.get("amount")), "currency": currency, "receipt": a.get("receipt"), "notes": a.get("notes") or {}}
        actions = [ProposedAction(kind=ActionKind.CREATE_ORDER, params=params, idempotency_key=idempotency_key, rationale="tool call create_order")]
        for i, transfer in enumerate(a.get("transfers") or []):
            if not isinstance(transfer, dict):
                continue
            actions.append(
                ProposedAction(
                    kind=ActionKind.CREATE_PAYOUT,
                    params={"amount": _int(transfer.get("amount")), "currency": str(transfer.get("currency") or currency), "fund_account_id": transfer.get("account"), "purpose": f"Route transfer on order {a.get('receipt') or ''}".strip()},
                    idempotency_key=f"{idempotency_key}#transfer{i}",
                    rationale="Route transfer inside create_order",
                )
            )
        return actions

    if name == "create_instant_settlement":
        params = {"amount": _int(a.get("amount")), "currency": currency, "fund_account_id": SELF_SETTLEMENT, "purpose": "instant settlement", "description": a.get("description") or ""}
        return [ProposedAction(kind=ActionKind.CREATE_PAYOUT, params=params, idempotency_key=idempotency_key, rationale="tool call create_instant_settlement")]

    if name == "revoke_token":
        return [ProposedAction(kind=ActionKind.UPDATE_ACCOUNT, params={"revoke_token": a.get("token_id") or a.get("token") or "?", "customer_id": a.get("customer_id")}, idempotency_key=idempotency_key, rationale="tool call revoke_token")]

    if name == "initiate_payment":
        params = {"amount": _int(a.get("amount")), "currency": currency, "receipt": a.get("order_id"), "notes": {"customer_id": a.get("customer_id"), "token": "[saved method]"}}
        return [ProposedAction(kind=ActionKind.CREATE_ORDER, params=params, idempotency_key=idempotency_key, rationale="tool call initiate_payment (charge a saved method)")]

    return []


def worst(dispositions: list[str]) -> str:
    order = {"deny": 0, "hold": 1, "allow": 2}
    return min(dispositions, key=lambda d: order.get(d, 3)) if dispositions else "allow"


__all__ = ["CONSEQUENTIAL", "SELF_SETTLEMENT", "AuthorityClass", "actions_from_call", "mandate_for_task", "records_from_payments", "worst", "time"]
