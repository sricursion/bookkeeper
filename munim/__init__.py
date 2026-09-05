"""Munim: a deterministic bookkeeper between an agent and a merchant's money.

A munim is the bookkeeper in an Indian merchant house: the person who checks
every outgoing rupee against the books before it leaves. This package plays
that role for AI agents that hold Razorpay tools.

Every proposed money action is checked against a mandate the merchant wrote
and against the merchant's own records, then written to an append-only,
hash-chained ledger with the full clause-by-clause evaluation. The gate never
reads the text the agent was exposed to. It constrains the action.
"""

from .actions import AUTHORITY_OF, MONEY_OUT, ActionKind, AuthorityClass, ProposedAction
from .clauses import CLAUSES, ClauseResult
from .gate import Decision, Gate, evaluate
from .ledger import Ledger, VerifyResult
from .mandate import Limits, Mandate, load_mandates
from .records import Counterparty, Payment, RecordStore

__version__ = "0.1.0"

__all__ = [
    "AUTHORITY_OF",
    "MONEY_OUT",
    "ActionKind",
    "AuthorityClass",
    "ProposedAction",
    "CLAUSES",
    "ClauseResult",
    "Decision",
    "Gate",
    "evaluate",
    "Ledger",
    "VerifyResult",
    "Limits",
    "Mandate",
    "load_mandates",
    "Counterparty",
    "Payment",
    "RecordStore",
]
