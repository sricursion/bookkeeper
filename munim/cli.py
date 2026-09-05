"""Command line: evaluate one action, verify a ledger, or run the corpus."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from .actions import ProposedAction
from .corpus import CORPUS_DIR, run_corpus
from .gate import Gate
from .ledger import Ledger
from .mandate import load_mandates
from .records import RecordStore
from .timeutil import parse_ts


def _utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def cmd_evaluate(args: argparse.Namespace) -> int:
    mandates = load_mandates(args.mandate)
    mandate = mandates[args.mandate_id] if args.mandate_id else next(iter(mandates.values()))
    records = RecordStore.load(args.records)
    ledger = Ledger(args.ledger)
    action = ProposedAction.from_dict(json.loads(Path(args.action).read_text(encoding="utf-8")))
    now = parse_ts(args.now) if args.now else None
    decision = Gate(mandate, records, ledger).evaluate(action, now=now)
    print(decision.reason)
    if args.verbose:
        for clause in decision.clauses:
            print(f"  {clause.clause_id} {clause.outcome:<5} {clause.name}: {clause.detail}")
    print(f"ledger: seq {ledger.head[0]} at {ledger.path}")
    return 0 if decision.disposition == "allow" else 1


def cmd_verify(args: argparse.Namespace) -> int:
    result = Ledger(args.ledger).verify()
    print(("intact: " if result.ok else "BROKEN: ") + result.detail)
    return 0 if result.ok else 1


def cmd_corpus(args: argparse.Namespace) -> int:
    corpus_dir = Path(args.corpus)
    ledger_path = Path(args.ledger) if args.ledger else Path(tempfile.mkdtemp()) / "corpus.ledger.jsonl"
    if ledger_path.exists():
        ledger_path.unlink()
    outcomes = list(run_corpus(corpus_dir, ledger_path))
    width = max(len(o.case.name) for o in outcomes)
    counts = {"allow": 0, "hold": 0, "deny": 0}
    mismatches = 0
    for o in outcomes:
        d = o.decision
        counts[d.disposition] += 1
        mark = "ok      " if o.ok else "MISMATCH"
        clauses = ",".join(d.failing_ids) or "-"
        dup = " dup" if d.duplicate_of is not None else ""
        print(f"{mark} {o.case.name:<{width}} {d.disposition:<5} {clauses}{dup}")
        if not o.ok:
            mismatches += 1
            print(f"         {o.mismatch()}")
    verify = Ledger(ledger_path).verify()
    print(
        f"\n{len(outcomes)} cases: {counts['allow']} allow, {counts['hold']} hold, {counts['deny']} deny; "
        f"{mismatches} mismatches; ledger {'intact' if verify.ok else 'BROKEN'} ({verify.entries} entries) at {ledger_path}"
    )
    return 0 if mismatches == 0 and verify.ok else 1


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = argparse.ArgumentParser(prog="munim", description="A deterministic bookkeeper between an agent and a merchant's money.")
    sub = parser.add_subparsers(dest="command", required=True)

    ev = sub.add_parser("evaluate", help="evaluate one proposed action and append the decision to a ledger")
    ev.add_argument("action", help="JSON file with the proposed action")
    ev.add_argument("--mandate", required=True, help="mandate JSON (one or a list)")
    ev.add_argument("--mandate-id", help="which mandate to use when the file holds several")
    ev.add_argument("--records", required=True, help="records JSON")
    ev.add_argument("--ledger", default="ledger.jsonl")
    ev.add_argument("--now", help="evaluate as of this ISO time (default: wall clock)")
    ev.add_argument("-v", "--verbose", action="store_true", help="print every clause")
    ev.set_defaults(func=cmd_evaluate)

    vf = sub.add_parser("verify", help="check a ledger's hash chain")
    vf.add_argument("ledger")
    vf.set_defaults(func=cmd_verify)

    cp = sub.add_parser("corpus", help="run the decision corpus and compare with expectations")
    cp.add_argument("--corpus", default=str(CORPUS_DIR))
    cp.add_argument("--ledger", help="keep the ledger the run produces at this path")
    cp.set_defaults(func=cmd_corpus)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
