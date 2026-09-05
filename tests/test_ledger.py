import json

from munim.actions import AuthorityClass
from munim.ledger import GENESIS, Ledger
from munim.timeutil import parse_ts


def _decision(amount, evaluated_at, disposition="allow", authority="payout", key="k", duplicate_of=None):
    return {
        "idempotency_key": key,
        "fingerprint": "f" * 64,
        "disposition": disposition,
        "authority_class": authority,
        "amount": amount,
        "evaluated_at": evaluated_at,
        "duplicate_of": duplicate_of,
    }


def test_chain_starts_at_genesis_and_verifies(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl")
    first = ledger.append("note", {"hello": "world"})
    assert first.seq == 1 and first.prev_hash == GENESIS
    second = ledger.append("note", {"hello": "again"})
    assert second.prev_hash == first.hash
    result = ledger.verify()
    assert result.ok and result.entries == 2


def test_tampering_with_an_amount_breaks_the_chain(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger = Ledger(path)
    now = parse_ts("2026-09-05T11:00:00+05:30")
    ledger.append("decision", _decision(500000, now, key="a"))
    ledger.append("decision", _decision(700000, now, key="b"))
    ledger.append("decision", _decision(900000, now, key="c"))
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["payload"]["amount"] = 70
    lines[1] = json.dumps(tampered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = Ledger(path).verify()
    assert not result.ok
    assert result.first_bad_seq == 2


def test_removing_a_line_breaks_the_chain(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger = Ledger(path)
    for i in range(3):
        ledger.append("note", {"i": i})
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = Ledger(path).verify()
    assert not result.ok and result.first_bad_seq == 3


def test_reopened_ledger_continues_the_chain(tmp_path):
    path = tmp_path / "l.jsonl"
    Ledger(path).append("note", {"i": 0})
    reopened = Ledger(path)
    entry = reopened.append("note", {"i": 1})
    assert entry.seq == 2
    assert Ledger(path).verify().ok


def test_spend_today_counts_only_allowed_non_duplicate_entries_in_the_ist_day(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl")
    day = parse_ts("2026-09-05T11:00:00+05:30")
    late = parse_ts("2026-09-05T23:59:00+05:30")
    next_day = parse_ts("2026-09-06T00:01:00+05:30")
    ledger.append("decision", _decision(100, day, key="a"))
    ledger.append("decision", _decision(200, late, key="b"))
    ledger.append("decision", _decision(400, day, disposition="hold", key="c"))
    ledger.append("decision", _decision(800, day, key="a", duplicate_of=1))
    ledger.append("decision", _decision(1600, day, authority="refund", key="d"))
    ledger.append("decision", _decision(3200, next_day, key="e"))
    assert ledger.spend_today(AuthorityClass.PAYOUT, day) == 300
    assert ledger.spend_today(AuthorityClass.REFUND, day) == 1600
    assert ledger.spend_today(AuthorityClass.PAYOUT, next_day) == 3200


def test_prior_decision_returns_the_latest_entry_for_a_key(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl")
    now = parse_ts("2026-09-05T11:00:00+05:30")
    ledger.append("decision", _decision(1, now, key="k1"))
    ledger.append("decision", _decision(2, now, key="k2"))
    ledger.append("decision", _decision(3, now, disposition="hold", key="k1"))
    prior = ledger.prior_decision("k1")
    assert prior is not None and prior.seq == 3 and prior.disposition == "hold"
    assert ledger.prior_decision("missing") is None
    assert ledger.prior_decision(None) is None
