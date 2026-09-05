"""An append-only ledger with a hash chain.

Each line is one entry. Its hash covers its own body and the previous entry's
hash, so editing or removing any line breaks every hash after it. The ledger
is also where the gate learns what has already been allowed today and
whether an idempotency key has been seen.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .actions import AuthorityClass, canonical_json
from .clauses import PriorDecision
from .timeutil import ist_day_bounds

GENESIS = "0" * 64


def entry_hash(seq: int, prev_hash: str, ts: int, kind: str, payload: Any) -> str:
    body = canonical_json({"ts": ts, "kind": kind, "payload": payload})
    return hashlib.sha256(f"{seq}|{prev_hash}|{body}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Entry:
    seq: int
    ts: int
    kind: str
    payload: Any
    prev_hash: str
    hash: str

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "ts": self.ts, "kind": self.kind, "payload": self.payload, "prev_hash": self.prev_hash, "hash": self.hash}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entry":
        return cls(seq=int(data["seq"]), ts=int(data["ts"]), kind=str(data["kind"]), payload=data["payload"], prev_hash=str(data["prev_hash"]), hash=str(data["hash"]))


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    entries: int
    first_bad_seq: int | None
    detail: str


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._seq = 0
        self._head = GENESIS
        if self.path.exists():
            for entry in self.entries():
                self._seq = entry.seq
                self._head = entry.hash

    @property
    def head(self) -> tuple[int, str]:
        return self._seq, self._head

    def entries(self) -> Iterator[Entry]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield Entry.from_dict(json.loads(line))

    def append(self, kind: str, payload: Any, *, ts: int | None = None) -> Entry:
        seq = self._seq + 1
        ts = int(time.time()) if ts is None else int(ts)
        payload = json.loads(canonical_json(payload))
        digest = entry_hash(seq, self._head, ts, kind, payload)
        entry = Entry(seq=seq, ts=ts, kind=kind, payload=payload, prev_hash=self._head, hash=digest)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(entry.to_dict()) + "\n")
        self._seq, self._head = seq, digest
        return entry

    def verify(self) -> VerifyResult:
        expected_seq, prev = 1, GENESIS
        count = 0
        for entry in self.entries():
            count += 1
            if entry.seq != expected_seq:
                return VerifyResult(False, count, entry.seq, f"expected seq {expected_seq}, found {entry.seq}")
            if entry.prev_hash != prev:
                return VerifyResult(False, count, entry.seq, f"seq {entry.seq} does not chain to the previous entry")
            if entry_hash(entry.seq, entry.prev_hash, entry.ts, entry.kind, entry.payload) != entry.hash:
                return VerifyResult(False, count, entry.seq, f"seq {entry.seq} content does not match its hash")
            expected_seq, prev = entry.seq + 1, entry.hash
        return VerifyResult(True, count, None, f"{count} entries, chain intact, head {prev[:12]}")

    # Views the gate needs -------------------------------------------------

    def decisions(self) -> Iterator[Entry]:
        for entry in self.entries():
            if entry.kind == "decision":
                yield entry

    def prior_decision(self, idempotency_key: str | None) -> PriorDecision | None:
        if not idempotency_key:
            return None
        found: PriorDecision | None = None
        for entry in self.decisions():
            if entry.payload.get("idempotency_key") == idempotency_key:
                found = PriorDecision(seq=entry.seq, fingerprint=str(entry.payload.get("fingerprint")), disposition=str(entry.payload.get("disposition")))
        return found

    def _allowed_today(self, now: int) -> Iterator[Any]:
        start, end = ist_day_bounds(now)
        for entry in self.decisions():
            p = entry.payload
            if p.get("disposition") != "allow" or p.get("duplicate_of") is not None:
                continue
            evaluated_at = int(p.get("evaluated_at", 0))
            if start <= evaluated_at < end:
                yield p

    def spend_today(self, authority: AuthorityClass, now: int) -> int:
        """Minor units allowed today (IST) for one authority class. Held, denied,
        and duplicate entries do not count; an allowed action counts as spent
        from the moment it is allowed. An entry may carry a `budget_amount`
        smaller than its amount, for updates that only raise an existing
        commitment by a delta."""
        total = 0
        for p in self._allowed_today(now):
            if p.get("authority_class") == authority.value:
                total += int(p.get("budget_amount") if p.get("budget_amount") is not None else (p.get("amount") or 0))
        return total

    def spend_today_on_target(self, target: str | None, now: int) -> int:
        """Minor units allowed today against one payment or one recipient, not
        counting amounts the principal named outright: the anti-splitting rule
        is about unnamed pieces, and a named amount was authorised as a whole."""
        if not target:
            return 0
        return sum(int(p.get("amount") or 0) for p in self._allowed_today(now) if p.get("target") == target and not p.get("amount_named"))
