# runbound live verification

- model: `qwen2.5:1.5b`
- endpoint: `http://localhost:11434/v1`
- runbound: `0.3.0`
- run at: 2026-09-12 10:52:56Z
- total runtime: 43.4s

| Scenario | Result | Evidence |
| --- | --- | --- |
| 1. token budget | PASS | call 9 refused: detector='budget', 324 tokens used against a limit of 300 |
| 2. per-call output cap | PASS | detector='spike', cap=40.0, 200 output tokens on one call |
| 3. spike watch | PASS | normal call 0.09-0.13s, the odd one 5.5s / 319 output tokens; runbound logged "Unusual call for session '1893280d1cc8' (watching): call took 5.5s, this session's normal is 0.1s", and served it |
| 4. tool loop, decorated | PASS | detector='loop' after 3 identical requests; the decorated tool ran 2x and never a third time |
| 5. tool loop, undecorated | PASS | detector='loop', message "Loop detected: model requested tool 'get_weather' repeated 3x in last "; the agent had dispatched 2 of them by hand |
| 6. policy deny | PASS | rule='deny', tool='delete_account', the body ran 0 times |
| 7. error storm + circuit | PASS | 3 connection errors opened the circuit (state 'open'); the 4th call was refused for provider 'openai@localhost:9' without a request going out |
| 8. wall-clock timeout | PASS | detector='timeout' after 1.0s, limit 0.5s |
| 9. fan-out depth | PASS | detector='fanout', rule='depth', depth 2 against a limit of 1 |
| 10. abuse ladder | PASS | action='rollover' at level 3, strike 1; rungs L0a-s0>L0a-s0>L0a-s0>L0a-s0>L0a-s0>L1a-s0>L2a2s0>L2a1s0>L3a0s0 |
| 11. GPU cost budget | PASS | call 9 refused: detector='budget', $0.324 of GPU time against a budget of $0.30 (budget_usd) |
| 12. in-flight cap | PASS | detector='inflight', provider 'openai@localhost:11434': 1 call in flight against a limit of 1, 1 chunks streamed so far |
| 13. per-endpoint circuits | PASS | 2 failures put openai@localhost:9 in 'open' while openai@localhost:11434 stayed 'closed' and answered 'OK' (the 'openai' shape reads 'open': the worst of the two) |

**13 PASS, 0 INFO, 0 FAIL** of 13 scenarios.
