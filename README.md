# Munim

A deterministic bookkeeper that stands between an AI agent and a merchant's
money on Razorpay.

A munim is the bookkeeper in an Indian merchant house, the person who checks
every outgoing rupee against the books before it leaves. This is that role for
agents that hold Razorpay tools: refunds, payouts, payment links, captures.
Every proposed money action is checked against a mandate the merchant wrote
and against the merchant's own records, then written to an append-only,
hash-chained ledger with the full clause-by-clause evaluation.

The gate never reads the text the agent was exposed to. It constrains the
action. An agent that has been talked into refunding ₹48,000 to a stranger
can believe whatever it likes; the refund still has to reference a captured
payment, stay under the cap, and clear the human-approval threshold.

## In one screen

| what | result |
|---|---|
| Core: 17 clauses, hash-chained ledger | 75 tests pass |
| Decision corpus, run in order against one ledger | 53 cases: 14 allow, 19 hold, 20 deny, 0 mismatches |
| AgentDojo banking suite, attacker's exact goal action, no model | 0 of 144 pairs allowed |
| AgentDojo banking suite, benign tasks' own ground-truth calls, no model | 12 of 16 pass entirely; the four that do not are named |
| AgentDojo live, gpt-oss-120b under attack, 27 pairs per attack | attack success 74.1% and 51.9% without the gate, 0.0% with it on both attacks; utility under attack 40.7% to 44.4% on one, 48.1% to 40.7% on the other |
| Precision and recall over the calls gpt-oss-120b actually made, both attacks | 33 of 33 malicious calls blocked, 0 of 12 legitimate calls blocked |
| AgentDojo live, GLM 5.3 Flash, 27 pairs | 0% attack success with and without the gate; utility identical, nothing held |
| Razorpay's own tools, real test-mode account, 13 operator tasks | 13 of 13 expectations met over REST, real refunds executed, ledgers intact; the same tasks run through the official `razorpay/mcp` server in Docker |

A decision from the real account. The owner asked for an order that routes
₹2,000 of it to a linked account nobody has paid before. The order passed;
the transfer inside it did not, because the day's mandate grants no payout
authority. This is ledger entry 3 of that task, abridged:

```json
{
  "seq": 3,
  "prev_hash": "236e73c365ceaae7...",
  "hash": "0ffe80ea7f35b5d5...",
  "disposition": "deny",
  "action": {
    "kind": "create_payout",
    "params": {"amount": 200000, "currency": "INR", "fund_account_id": "acc_UnknownVendor99",
               "purpose": "Route transfer on order KD-1054"},
    "idempotency_key": "chatcmpl-tool-971d24a068802f59#transfer0"
  },
  "clauses": [{"id": "G01", "name": "authority in scope", "outcome": "deny",
               "detail": "payout authority was not granted by mandate m_kirana_ops_day (granted: collect, refund)"}],
  "reason": "DENIED: create_payout ₹2,000.00 to acc_UnknownVendor99; G01 authority in scope: ..."
}
```

Reproduce the core in one line, no keys, no network:

```bash
python -m pytest -q && python -m munim corpus
```

## Why this exists

The official Razorpay MCP server gives an agent refunds, captures, orders
and payment links with no policy layer between the model and the money. A
model that reads a customer ticket or an order note can be talked into a
refund, a payout, or a settlement change, and that is a class of loss no
fraud model built on transaction features sees coming: the transaction is
well formed, the amount is plausible, and the merchant's own agent placed
it. Munim is a verifier for that loss class. It is strictly defensive: it
never probes, never attacks, never inspects the text; it decides whether a
proposed money action is inside the merchant's standing authority, and
writes down why. The false-positive cost is a legitimate action held for a
person, and it is measured and reported alongside the recall.

## How a decision is made

Seventeen clauses run on every proposed action. Each is a pure function of
the action, the mandate, the records, the clock, and what the ledger already
shows for today. A denial anywhere beats a hold anywhere. Denials are for
the impossible or malformed; holds are for the possible but unauthorised,
and a person decides.

| Clause | Checks | On failure |
|---|---|---|
| G00 | Amount is positive integer paise where money moves; currency and required ids present; an account change changes something | deny |
| G01 | The action's authority class (collect, refund, payout, admin) is in the mandate's scopes | deny |
| G02 | The mandate is in force at evaluation time | deny |
| G03 | Currency is allowed | deny |
| G04 | An idempotency key is present; a reused key with a changed request is refused | deny |
| G05 | A refund references a captured payment and does not exceed what is refundable | deny |
| G06 | A capture references an authorised payment for the full authorised amount | deny |
| G07 | A payout recipient has been paid before; a recipient the principal named but who has no history is paid only an amount the principal named with that recipient | hold |
| G08 | Amount within the per-action cap, unless the principal named that amount; the cap also bounds the day's total of unnamed amounts against the same payment or recipient, so it cannot be split around | hold |
| G09 | Today's allowed spend plus this amount within the daily budget | hold |
| G10 | Above the evidence threshold, at least one evidence reference is attached | hold |
| G11 | Below the human-approval threshold; nothing named can bypass this | hold |
| G12 | Money-out actions fall within active hours (IST) | hold |
| G13 | Free-text fields carry no UPI handles, links, account numbers, IFSC codes, or IBANs the principal did not name | hold |
| G14 | Records are fresh enough to ground a money-out action; otherwise the gate is degraded and holds | hold |
| G15 | An account change touches only fields the principal named, with the value the principal gave where they gave one | hold |
| G16 | A recurring payout was asked for by the principal | hold |

An identical request with an identical idempotency key returns the earlier
decision as a duplicate and is never executed twice. Credential-like fields
are redacted before anything is written to the ledger; the fingerprint still
covers the real values. The agent is told which clauses declined a call and
nothing numeric; caps, budgets and thresholds stay in the ledger, for
people.

The fourth authority class, admin, covers changes to who the account is and
where its money goes: credentials, contact details, the settlement
destination. It is the class business-email-compromise fraud targets, and it
is never granted by an ordinary day's mandate.

## What was measured

**A public benchmark, before and after a model is involved.** AgentDojo's
banking suite (NeurIPS 2024) is 16 user tasks by 9 injection goals over
tools like `send_money` and `update_password`. With no model, the attacker's
exact goal action is held or denied in all 144 pairs, and 12 of 16 benign
tasks pass their own ground-truth calls; the four that do not, and why, are
in [eval/agentdojo/README.md](eval/agentdojo/README.md). Live, on a model
that falls for these injections, `gpt-oss-120b`, the gate takes attack
success from 74.1% to 0.0% under one attack and from 51.9% to 0.0% under a
held-out one, on 27 pairs each. Utility under attack rose from 40.7% to
44.4% on the first, because a hijacked agent is told to stop retrying and
finish the user's task, and fell from 48.1% to 40.7% on the second, which
is the cost of a hard block and is reported as such. On GLM 5.3 Flash,
which refuses these injections on its own, attack success is 0 of 27 in
both arms and utility is identical: the gate costs nothing on a model that
does not need it. Both arms of every comparison ran with identical
sampling.

**Razorpay's own tools, on a real test-mode account.** An operations agent
holding the official server's tools ran thirteen operator tasks, some
carrying injections in customer tickets and order notes, one with the
account unreachable. 13 of 13 expectations met on a simulated backend and
on the real account over REST, and the same tasks ran through the official
`razorpay/mcp` server in Docker with the same gate; see
[eval/razorpay/README.md](eval/razorpay/README.md).

**A code review that found three holes the offline matrix could not see.**
Named amounts were not bound to the recipients they were named with,
establishment could be earned mid-episode, and a large action could be
split around the cap. All three are fixed, each with a corpus case that
pins the exact clause that now catches it.

## Layout

```
munim/            the gate: actions, mandate, records, 17 clauses, gate, ledger, corpus runner, CLI
corpus/           53 proposed actions with the disposition and clauses each must produce
tests/            75 tests
eval/agentdojo/   the gate as an AgentDojo defense; offline checks and live runs
eval/razorpay/    an operations agent over Razorpay's tools; MCP, REST, and simulated backends
docs/             ARCHITECTURE.md
runs/             traces, ledgers, and reports from every run reported here
```

## Running it

The core needs Python 3.10+ and nothing else. The evaluations need a
virtual environment with `agentdojo`, `openai`, `mcp`, and `httpx`, plus
keys in a gitignored `.env`: `FIREWORKS_API_KEY` for the model,
`RAZORPAY_KEY_ID` (or `RAZORPAY_API`) and `RAZORPAY_KEY_SECRET` for the
real backends. The key id must start with `rzp_test_`; both real backends
refuse anything else.

```bash
python -m munim corpus                                            # the decision corpus
python -m munim evaluate action.json --mandate m.json --records r.json  # one action
python -m munim verify ledger.jsonl                               # check a chain
python -m munim show -v runs/razorpay/mcp-clean-t04/t04-ticket-legit.jsonl   # read a ledger, every clause
.venv/Scripts/python eval/agentdojo/run.py --goal-matrix          # 144 pairs, no model
.venv/Scripts/python eval/agentdojo/run.py --limit 3              # 27-pair live pilot
.venv/Scripts/python eval/razorpay/agent.py --backend sim         # 13 tasks, simulated
.venv/Scripts/python eval/razorpay/agent.py --backend rest        # 13 tasks, real test account
.venv/Scripts/python eval/razorpay/agent.py --backend mcp         # via the official server in Docker
```

## Units and vocabulary

Amounts are integer paise, the unit Razorpay's API uses. Payments are
`pay_...` ids, payout destinations are `fa_...` fund accounts or `acc_...`
linked accounts, and the merchant's day and active hours are reckoned in
IST.

## Prior art

Constraining the action rather than inspecting the text is the defense class
that AgentDojo (Debenedetti et al., NeurIPS 2024) and CaMeL (2025) identify
as durable. Mandate Labs published a mandate-based gate on AgentDojo's
banking suite, 1.4% attack success at 68.8% utility against 75.0% and 81.9%
with no defense on GPT-4o, and reported credential changes riding legitimate
admin authority as the residual. That residual is closed here by binding
account changes to named fields and values. What this repository adds is the
merchant-side policy model for Razorpay's own money actions, the binding of
named amounts to named recipients, the anti-splitting per-target cap, the
ledger, and the same gate running unchanged over the official Razorpay
tools.

## License

GNU General Public License v3.0 or later. See [LICENSE](LICENSE).
