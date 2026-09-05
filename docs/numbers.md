# The numbers

| what | result |
|---|---|
| Core: 17 clauses, hash-chained ledger | 75 tests pass |
| Decision corpus, run in order against one ledger | 53 cases, 0 mismatches |
| AgentDojo banking suite, attacker's exact goal action, no model | 0 of 144 pairs allowed |
| AgentDojo banking suite, benign tasks' own ground-truth calls, no model | 12 of 16 pass entirely, four named with reasons |
| gpt-oss-120b live, important_instructions, 27 pairs | attack success 74.1% without the gate, 0.0% with it; utility 40.7% to 44.4% |
| gpt-oss-120b live, tool_knowledge (held out), 27 pairs | attack success 51.9% without the gate, 0.0% with it; utility 48.1% to 40.7% |
| GLM 5.3 Flash live, both attacks, 27 pairs each | 0.0% in both arms; utility identical; nothing held |
| Razorpay's own tools, real test-mode account, 13 tasks, REST | 13 of 13 |
| Same 13 tasks through the official razorpay/mcp server in Docker | 13 of 13 |
