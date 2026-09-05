"""The decision corpus: proposed actions with the disposition each must get.

Cases run in file order against one shared ledger, so budgets accumulate and
idempotency keys carry over, the way they would in a real day.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .actions import ProposedAction
from .gate import Decision, Gate
from .ledger import Ledger
from .mandate import Mandate, load_mandates
from .records import RecordStore
from .timeutil import parse_ts

CORPUS_DIR = Path(__file__).resolve().parent.parent / "corpus"


@dataclass(frozen=True)
class Case:
    name: str
    mandate_id: str
    now: int
    records_fetched_at: int | None
    action: dict[str, Any]
    expect_disposition: str
    expect_clauses: tuple[str, ...]
    expect_duplicate: bool
    expect_degraded: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any], defaults: dict[str, Any]) -> "Case":
        expect = data["expect"]
        fetched = data.get("records_fetched_at", defaults.get("records_fetched_at"))
        return cls(
            name=str(data["case"]),
            mandate_id=str(data.get("mandate", defaults["mandate"])),
            now=parse_ts(data.get("now", defaults["now"])),
            records_fetched_at=parse_ts(fetched) if fetched is not None else None,
            action=dict(data["action"]),
            expect_disposition=str(expect["disposition"]),
            expect_clauses=tuple(sorted(expect.get("clauses", []))),
            expect_duplicate=bool(expect.get("duplicate", False)),
            expect_degraded=bool(expect.get("degraded", False)),
        )


@dataclass(frozen=True)
class Outcome:
    case: Case
    decision: Decision

    @property
    def got_clauses(self) -> tuple[str, ...]:
        return tuple(sorted(self.decision.failing_ids))

    @property
    def ok(self) -> bool:
        return (
            self.decision.disposition == self.case.expect_disposition
            and self.got_clauses == self.case.expect_clauses
            and (self.decision.duplicate_of is not None) == self.case.expect_duplicate
            and self.decision.degraded == self.case.expect_degraded
        )

    def mismatch(self) -> str:
        parts = []
        if self.decision.disposition != self.case.expect_disposition:
            parts.append(f"disposition expected {self.case.expect_disposition}, got {self.decision.disposition}")
        if self.got_clauses != self.case.expect_clauses:
            parts.append(f"clauses expected {list(self.case.expect_clauses)}, got {list(self.got_clauses)}")
        if (self.decision.duplicate_of is not None) != self.case.expect_duplicate:
            parts.append(f"duplicate expected {self.case.expect_duplicate}, got {self.decision.duplicate_of is not None}")
        if self.decision.degraded != self.case.expect_degraded:
            parts.append(f"degraded expected {self.case.expect_degraded}, got {self.decision.degraded}")
        return "; ".join(parts)


def load_cases(path: Path) -> list[Case]:
    defaults: dict[str, Any] = {}
    cases: list[Case] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            data = json.loads(line)
            if "defaults" in data:
                defaults = dict(data["defaults"])
                continue
            cases.append(Case.from_dict(data, defaults))
    return cases


def run_corpus(corpus_dir: Path, ledger_path: Path) -> Iterator[Outcome]:
    mandates: dict[str, Mandate] = load_mandates(corpus_dir / "mandates.json")
    base_records = RecordStore.load(corpus_dir / "records.json")
    ledger = Ledger(ledger_path)
    for case in load_cases(corpus_dir / "actions.jsonl"):
        records = base_records if case.records_fetched_at is None else base_records.with_fetched_at(case.records_fetched_at)
        gate = Gate(mandates[case.mandate_id], records, ledger, clock=lambda now=case.now: now)
        action = ProposedAction.from_dict(case.action, default_id=case.name, proposed_at=case.now)
        yield Outcome(case=case, decision=gate.evaluate(action, now=case.now))
