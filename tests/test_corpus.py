"""Every corpus case must get exactly the disposition and the failing clauses it expects."""

from pathlib import Path

import pytest

from munim.corpus import CORPUS_DIR, load_cases, run_corpus
from munim.ledger import Ledger

CASE_NAMES = [case.name for case in load_cases(CORPUS_DIR / "actions.jsonl")]


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    ledger_path = tmp_path_factory.mktemp("ledger") / "corpus.ledger.jsonl"
    results = {o.case.name: o for o in run_corpus(CORPUS_DIR, ledger_path)}
    results["__ledger__"] = ledger_path
    return results


@pytest.mark.parametrize("name", CASE_NAMES)
def test_case(outcomes, name):
    outcome = outcomes[name]
    assert outcome.ok, f"{name}: {outcome.mismatch()}\n  reason: {outcome.decision.reason}"


def test_corpus_has_every_disposition(outcomes):
    dispositions = {o.decision.disposition for k, o in outcomes.items() if k != "__ledger__"}
    assert dispositions == {"allow", "hold", "deny"}


def test_corpus_ledger_is_intact(outcomes):
    ledger = Ledger(outcomes["__ledger__"])
    result = ledger.verify()
    assert result.ok, result.detail
    assert result.entries == len(CASE_NAMES)


def test_every_clause_is_exercised(outcomes):
    fired = set()
    for name, o in outcomes.items():
        if name != "__ledger__":
            fired.update(o.decision.failing_ids)
    expected = {f"G{i:02d}" for i in range(17)}
    assert fired == expected, f"clauses never failed by any case: {sorted(expected - fired)}"
