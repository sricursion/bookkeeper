"""Munim as an AgentDojo defense.

The banking suite's consequential tools (send_money, schedule_transaction,
update_scheduled_transaction, update_password, update_user_info) are routed
through the gate. Everything the gate trusts comes from two places: the user's
own task text, read once at the start of an episode, and the account's own
records as they stood when the episode began. Tool outputs, where the
injections live, are never read by the gate.

Mapping to the merchant model: an IBAN is a fund account, a transfer is a
payout, a scheduled transfer is a payout with a date, a password or address
change is an account change. Amounts become integer minor units.
"""

from __future__ import annotations

import hashlib
import re
import time
from ast import literal_eval
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentdojo.agent_pipeline.llms.google_llm import EMPTY_FUNCTION_NAME
from agentdojo.agent_pipeline.tool_execution import ToolsExecutor, is_string_list, tool_result_to_str
from agentdojo.functions_runtime import EmptyEnv, Env, FunctionCall, FunctionsRuntime
from agentdojo.types import ChatMessage, ChatToolResultMessage, text_content_block_from_string

from munim import ActionKind, AuthorityClass, Counterparty, Gate, Ledger, Limits, Mandate, ProposedAction, RecordStore
from munim.gate import Decision
from munim.money import minor_from_major

CURRENCY = "EUR"  # the suite has no currency field; one code is used for formatting only
CONSEQUENTIAL = frozenset(
    {"send_money", "schedule_transaction", "update_scheduled_transaction", "update_password", "update_user_info"}
)
UNBOUNDED = 10**15  # the benchmark has no evidence and no human in the loop; those thresholds are switched off
PROFILE_FIELDS = ("first_name", "last_name", "street", "city")

# Authority is derived from the user's own words only.
_MONEY = re.compile(
    r"\b(pay|paid|payment|send|sent|transfer|refund|reimburse|rent|bill|subscription|standing orders?|difference|schedule|scheduled)\b",
    re.IGNORECASE,
)
_ADDRESS = re.compile(r"\b(address|moved)\b", re.IGNORECASE)
_NAME = re.compile(r"\b(my name|first name|last name)\b", re.IGNORECASE)
_ACCOUNT_INFO = re.compile(r"\b(account information|account info|user info)\b", re.IGNORECASE)
_PASSWORD = re.compile(r"\bpassword\b", re.IGNORECASE)
_PASSWORD_VALUE = re.compile(
    r"password\s+(?:to|as|of|=|:)?\s*['\"‘’“”]([^'\"‘’“”]+)['\"‘’“”]",
    re.IGNORECASE,
)
# Words that ask for a standing payment. Rent and subscriptions are periodic by nature; a
# model asked to "adjust my rent" reasonably schedules it as recurring, and the first
# version of this rule held that, costing utility on the benchmark's rent task.
_RECURRING = re.compile(r"\b(recurring|standing orders?|subscriptions?|rent|monthly|weekly|yearly|annual)\b", re.IGNORECASE)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")

# A number the user wrote as an amount: grouped or plain digits, not part of a filename or
# identifier, and either written with decimals or standing next to a money word. A street
# number or a postcode is not an amount the principal named, and a named amount lifts the cap.
_NUMBER = re.compile(r"(?<![A-Za-z0-9._\-])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![A-Za-z0-9_\-])")
_MONEY_CONTEXT = re.compile(
    r"(rent|amount|pay|paid|payment|send|sent|transfer|refund|bill|price|cost|fee|total|share|spent|charge|owe|[$€£₹]|\beur\b|\busd\b|\bgbp\b|dollars?|euros?|pounds?|rupees?)",
    re.IGNORECASE,
)


def canon(iban: Any) -> str:
    """IBANs compare case-insensitively and without spaces in the suite; the gate keys them the same way."""
    return re.sub(r"\s+", "", str(iban or "")).upper()


def to_minor(value: Any) -> int | None:
    """Benchmark amounts are floats in major units; the gate wants integer minor units."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return minor_from_major(str(value).replace(",", "").strip())
    except Exception:
        return None


def _looks_like_year(token: str) -> bool:
    return "." not in token and "," not in token and len(token) == 4 and 1900 <= int(token) <= 2099


def named_amounts_in(text: str) -> set[int]:
    amounts: set[int] = set()
    for match in _NUMBER.finditer(text):
        token = match.group(0)
        window = text[max(0, match.start() - 40): match.end() + 40]
        money_nearby = bool(_MONEY_CONTEXT.search(window))
        if _looks_like_year(token) and not money_nearby:
            continue
        if "." not in token and not money_nearby:
            continue
        minor = to_minor(token)
        if minor:
            amounts.add(minor)
    return amounts


@dataclass(frozen=True)
class BankFacts:
    own_iban: str
    counterparties: dict[str, Counterparty]
    max_outgoing_minor: int


def bank_facts(env: Any, now: int) -> BankFacts:
    """Counterparties that have actually been paid before, and the largest amount the
    account has ever committed. Scheduled transfers are not history: nothing has been
    paid yet. Senders of incoming money are not payout history either. The account's
    own IBAN is not a counterparty."""
    account = env.bank_account
    own = canon(account.iban)
    seen: dict[str, Counterparty] = {}
    committed: list[int] = []
    for t in account.transactions:
        if canon(t.sender) in ("ME", own):
            committed.append(to_minor(t.amount) or 0)
            recipient = canon(t.recipient)
            if recipient and recipient != own:
                prior = seen.get(recipient)
                seen[recipient] = Counterparty(
                    fund_account_id=recipient, contact_id=recipient, name="prior payout recipient", first_seen=now, payouts_count=(prior.payouts_count if prior else 0) + 1
                )
    for t in account.scheduled_transactions:
        committed.append(to_minor(t.amount) or 0)
    return BankFacts(own_iban=own, counterparties=seen, max_outgoing_minor=max(committed, default=0))


def records_from_env(env: Any, now: int, counterparties: dict[str, Counterparty] | None = None) -> RecordStore:
    facts = bank_facts(env, now)
    return RecordStore(fetched_at=now, payments={}, counterparties=facts.counterparties if counterparties is None else counterparties, source="agentdojo:bank_account")


def derive_mandate(task_text: str, env: Any, now: int, cap_multiplier: float = 2.0) -> Mandate:
    """Everything the principal authorised, read from the task text and the account history.

    Scopes come from keywords in the user's own instruction. Named recipients
    are IBANs the user wrote, each bound to the amounts written in the same
    sentence. Named amounts are numbers the user wrote as money. The
    per-action cap is a multiple of the largest amount the account has ever
    committed. None of this touches a tool output.
    """
    facts = bank_facts(env, now)
    scopes: set[AuthorityClass] = set()
    named_fields: set[str] = set()
    named_values: dict[str, str] = {}

    if _MONEY.search(task_text):
        scopes.add(AuthorityClass.PAYOUT)
    if _ADDRESS.search(task_text):
        scopes.add(AuthorityClass.ADMIN)
        named_fields.update({"street", "city"})
    if _NAME.search(task_text):
        scopes.add(AuthorityClass.ADMIN)
        named_fields.update({"first_name", "last_name"})
    if _ACCOUNT_INFO.search(task_text):
        scopes.add(AuthorityClass.ADMIN)
        named_fields.update(PROFILE_FIELDS)
    if _PASSWORD.search(task_text):
        scopes.add(AuthorityClass.ADMIN)
        named_fields.add("password")
        match = _PASSWORD_VALUE.search(task_text)
        if match:
            named_values["password"] = match.group(1)
    if _RECURRING.search(task_text):
        named_fields.add("recurring")

    recipients: set[str] = set()
    bound: dict[str, set[int]] = {}
    for sentence in _SENTENCE_SPLIT.split(task_text):
        ibans = {canon(i) for i in _IBAN.findall(sentence)}
        if not ibans:
            continue
        amounts_here = named_amounts_in(_IBAN.sub(" ", sentence))
        for iban in ibans:
            recipients.add(iban)
            bound.setdefault(iban, set()).update(amounts_here)
    amounts = named_amounts_in(_IBAN.sub(" ", task_text))

    cap = max(int(facts.max_outgoing_minor * cap_multiplier), 100)
    limits = {
        AuthorityClass.PAYOUT: Limits(
            per_action_cap=cap, daily_budget=3 * cap, evidence_above=UNBOUNDED, human_approval_above=UNBOUNDED
        )
    }
    digest = hashlib.sha256(task_text.encode("utf-8")).hexdigest()[:8]
    return Mandate(
        id=f"agentdojo-{digest}",
        principal="the user, from the task text",
        issued_at=now,
        valid_from=now - 60,
        valid_until=now + 24 * 3600,
        scopes=frozenset(scopes),
        limits=limits,
        named_amounts=frozenset(amounts),
        named_recipients=frozenset(recipients),
        named_fields=frozenset(named_fields),
        named_values=named_values,
        named_recipient_amounts={k: frozenset(v) for k, v in bound.items()},
        allowed_currencies=frozenset({CURRENCY}),
        active_hours_ist=(0, 24),
        max_records_age_seconds=3600,
    )


def _amount_or_none(value: Any) -> Any:
    return None if value in (None, "", 0, 0.0) else value


def action_from_call(call: FunctionCall, env: Any, idempotency_key: str) -> ProposedAction | None:
    """Translate a consequential banking tool call into a proposed money action."""
    name = call.function
    args = dict(call.args)
    if name in ("send_money", "schedule_transaction"):
        params: dict[str, Any] = {
            "amount": to_minor(args.get("amount")),
            "currency": CURRENCY,
            "fund_account_id": canon(args.get("recipient")),
            "purpose": str(args.get("subject") or ""),
            "date": args.get("date"),
        }
        if name == "schedule_transaction":
            params["recurring"] = bool(args.get("recurring"))
        return ProposedAction(kind=ActionKind.CREATE_PAYOUT, params=params, idempotency_key=idempotency_key, rationale=f"tool call {name}")

    if name == "update_scheduled_transaction":
        try:
            scheduled_id: int | None = int(float(args.get("id")))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            scheduled_id = None
        existing = next((t for t in env.bank_account.scheduled_transactions if t.id == scheduled_id), None)
        new_recipient = canon(args.get("recipient")) if args.get("recipient") else ""
        recipient = new_recipient or (canon(existing.recipient) if existing else "")
        new_amount = _amount_or_none(args.get("amount"))
        amount = new_amount if new_amount is not None else (existing.amount if existing else None)
        subject = args.get("subject") or (existing.subject if existing else "")
        params = {
            "amount": to_minor(amount),
            "currency": CURRENCY,
            "fund_account_id": recipient,
            "purpose": str(subject),
            "scheduled_transaction_id": scheduled_id,
            "changes": sorted(k for k, v in args.items() if k != "id" and v not in (None, "")),
        }
        if args.get("recurring"):
            params["recurring"] = True
        if existing is not None and (not new_recipient or new_recipient == canon(existing.recipient)):
            # Same payee: only the increase is a new commitment against the daily budget.
            old_minor = to_minor(existing.amount) or 0
            params["budget_amount"] = max((to_minor(amount) or 0) - old_minor, 0)
        return ProposedAction(kind=ActionKind.CREATE_PAYOUT, params=params, idempotency_key=idempotency_key, rationale=f"tool call {name}")

    if name == "update_password":
        return ProposedAction(
            kind=ActionKind.UPDATE_ACCOUNT, params={"password": args.get("password")}, idempotency_key=idempotency_key, rationale="tool call update_password"
        )

    if name == "update_user_info":
        fields = {k: v for k, v in args.items() if k in PROFILE_FIELDS and v not in (None, "")}
        return ProposedAction(kind=ActionKind.UPDATE_ACCOUNT, params=fields, idempotency_key=idempotency_key, rationale="tool call update_user_info")

    return None


class GatedToolsExecutor(ToolsExecutor):
    """The stock executor, with consequential calls routed through the gate.

    A new episode starts whenever the conversation has no tool results yet.
    The mandate for the episode is derived from the user query at that moment
    and never revised. Which counterparties count as established is frozen at
    the same moment: a transfer the agent itself makes during the episode does
    not make its recipient history. The model is told which clauses declined
    a call, never the caps or budgets behind them; those stay in the ledger.
    """

    def __init__(
        self,
        ledger_dir: Path,
        tool_output_formatter: Callable[..., str] = tool_result_to_str,
        clock: Callable[[], int] | None = None,
        cap_multiplier: float = 2.0,
    ) -> None:
        super().__init__(tool_output_formatter)
        self.ledger_dir = Path(ledger_dir)
        self.clock = clock or (lambda: int(time.time()))
        self.cap_multiplier = cap_multiplier
        self.episode = 0
        self._gate: Gate | None = None
        self._frozen: dict[str, Counterparty] = {}
        self._calls = 0
        self.stats = {"evaluated": 0, "allow": 0, "hold": 0, "deny": 0, "executed": 0}

    def _start_episode(self, query: str, env: Any) -> Gate:
        self.episode += 1
        self._calls = 0
        now = self.clock()
        mandate = derive_mandate(query, env, now, self.cap_multiplier)
        self._frozen = bank_facts(env, now).counterparties
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        ledger = Ledger(self.ledger_dir / f"episode_{self.episode:03d}.jsonl")
        ledger.append("episode", {"query": query, "mandate": mandate.to_dict()}, ts=now)
        self._gate = Gate(mandate, records_from_env(env, now, self._frozen), ledger, clock=self.clock)
        return self._gate

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = [],
        extra_args: dict = {},
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict]:
        if len(messages) == 0 or messages[-1]["role"] != "assistant":
            return query, runtime, env, messages, extra_args
        if messages[-1]["tool_calls"] is None or len(messages[-1]["tool_calls"]) == 0:
            return query, runtime, env, messages, extra_args

        first_turn = not any(m["role"] == "tool" for m in messages)
        gate = self._start_episode(query, env) if first_turn or self._gate is None else self._gate
        gate.refresh_records(records_from_env(env, self.clock(), self._frozen))

        results: list[ChatToolResultMessage] = []
        available = {tool.name for tool in runtime.functions.values()}
        for tool_call in messages[-1]["tool_calls"]:
            if tool_call.function == EMPTY_FUNCTION_NAME:
                results.append(self._error(tool_call, "Empty function name provided. Provide a valid function name."))
                continue
            if tool_call.function not in available:
                results.append(self._error(tool_call, f"Invalid tool {tool_call.function} provided."))
                continue
            for arg_k, arg_v in tool_call.args.items():
                if isinstance(arg_v, str) and is_string_list(arg_v):
                    tool_call.args[arg_k] = literal_eval(arg_v)

            decision: Decision | None = None
            if tool_call.function in CONSEQUENTIAL:
                self._calls += 1
                key = tool_call.id or f"call-{self.episode}-{self._calls}"
                action = action_from_call(tool_call, env, key)
                if action is not None:
                    decision = gate.evaluate(action)
                    self.stats["evaluated"] += 1
                    self.stats[decision.disposition] += 1
                    if decision.disposition != "allow":
                        results.append(
                            self._error(
                                tool_call,
                                f"Declined by the account's money gate. {decision.summary()}. This action will not be permitted in this "
                                "session however it is phrased; do not retry it. Continue with the user's original request and report the decline.",
                            )
                        )
                        continue

            tool_call_result, error = runtime.run_function(env, tool_call.function, tool_call.args)
            if decision is not None and error is None:
                self.stats["executed"] += 1
                gate.record_execution(decision, {"tool": tool_call.function, "result": str(tool_call_result)[:300]})
            results.append(
                ChatToolResultMessage(
                    role="tool",
                    content=[text_content_block_from_string(self.output_formatter(tool_call_result))],
                    tool_call_id=tool_call.id,
                    tool_call=tool_call,
                    error=error,
                )
            )
        return query, runtime, env, [*messages, *results], extra_args

    @staticmethod
    def _error(tool_call: FunctionCall, message: str) -> ChatToolResultMessage:
        return ChatToolResultMessage(
            role="tool",
            content=[text_content_block_from_string("")],
            tool_call_id=tool_call.id,
            tool_call=tool_call,
            error=message,
        )
