# Acceptance scenarios

Recorded run: inside the Linux test image (`scripts/run_evidence.sh`), on the code of this commit
(tree ids in [evidence/RUN_INFO.txt](evidence/RUN_INFO.txt)), 2026-10-14. Raw output:
[evidence/pytest_output.txt](evidence/pytest_output.txt), [evidence/junit.xml](evidence/junit.xml),
one trace per test in [evidence/runs/](evidence/runs/). Result: **66 passed, 1 skipped** (the live gate).

Every result is against the **simulated** M3 source in `sim/m3sim.py`. The agent, the reference
receiver, SQLite persistence, TLS, process kills and sockets are real. Logs marked "SIMULATED
elapsed time" use a fake clock; process and network logs use real processes and wall-clock time.

## Scenarios from the original acceptance table

| Priority | Scenario | Test | Result | Log |
|---|---|---|---|---|
| P0 | Missed notification; proposal remains present | `test_p0_01_missed_notification_recovered`, `test_headline_sequence` | passed: recovered 13 min and 1 min after creation, 15 min interval | [log](evidence/runs/test_p0_01_missed_notification_recovered.log) |
| P0 | Created and removed between sweeps | `test_p0_02_created_and_removed_between_scans_is_a_blind_spot` | passed: zero receiver effects, stated as a blind spot | [log](evidence/runs/test_p0_02_created_and_removed_between_scans_is_a_blind_spot.log) |
| P0 | Enumeration exceeds the cap | `test_p0_03_enumeration_exceeds_cap` | passed: 22 lines over 11 proposals at `page_cap=5`, identical LMTS; a PLPN filling a page makes the scan incomplete | [log](evidence/runs/test_p0_03_enumeration_exceeds_cap.log) |
| P0 | Middle page fails or is truncated | `test_p0_04_middle_page_fails_or_truncated` | passed for HTTP 500, truncated record, MI NOK | [log](evidence/runs/test_p0_04_middle_page_fails_or_truncated.log) |
| P0 | Disappears between enumeration and detail read | `test_p0_05_disappears_between_enumeration_and_detail_read` | passed | [log](evidence/runs/test_p0_05_disappears_between_enumeration_and_detail_read.log) |
| P0 | Receiver commits, acknowledgement lost | `test_p0_06_receiver_commits_ack_lost` | passed: 2 effects for 2 delivery ids; altered payload under the same id gets 409 | [log](evidence/runs/test_p0_06_receiver_commits_ack_lost.log) |
| P0 | Process killed at persistence boundaries | `test_p0_07_*` (4 cases), `test_a03_*`, `test_chaos_random_sigkill_while_source_mutates` | passed: `os._exit(137)` at each boundary, including inside the terminal-refusal transaction; 15 random SIGKILLs while the source changes: 0 mismatches, no id with two effects | [logs](evidence/runs/) |
| P0 | Duplicate and out-of-order observations | `test_p0_08_duplicate_and_out_of_order` | passed: A to B to A ends at A; seq 5 after seq 6 is "stale" | [log](evidence/runs/test_p0_08_duplicate_and_out_of_order.log) |
| P0 | Two installations share identifiers; wrong company | `test_p0_09_two_installations_same_identifiers`, `test_a04_*` | passed: kept apart; identity mismatch refused at registration; session company 200 refuses the scan, even with no rows | [log](evidence/runs/test_p0_09_two_installations_same_identifiers.log) |
| P0 | Eligibility changes | `test_p0_10_eligibility_changes`, `test_r10_*` | passed | [log](evidence/runs/test_p0_10_eligibility_changes.log) |
| P1 | Money and quantity cases | `test_p1_money_cases`, `test_a17_*` | passed under the declared policy (per-item conversions, exact comparison) | [log](evidence/runs/test_p1_money_cases.log) |
| P1 | Authentication and MI failures | `test_p1_auth_and_mi_failures`, `test_p1_ion_profiles[2]`, `test_a05_*`, `test_a06_*` | passed | [log](evidence/runs/test_p1_auth_and_mi_failures.log) |
| P1 | Network boundary | `test_p1_network_boundary`, `test_r07_*` | passed: `lsof` shows no listening socket on the running agent | [log](evidence/runs/test_p1_network_boundary.log) |
| P1 | Slow source, backlog, disk full | `test_p1_rate_limit_slow_source_backlog_disk_full`, `test_a08_*`, `test_a13_*` | passed: at most 20 calls in any second at 20/s; 30 changes with room for 10 drain in four passes; disk full leaves the checkpoint | [log](evidence/runs/test_p1_rate_limit_slow_source_backlog_disk_full.log) |
| P1 | Restart, upgrade, second instance | `test_p0_07_*`, `test_p1_second_instance_is_refused`, recovery tests below | restart and second instance passed; **upgrade across schema changes not tested** | [log](evidence/runs/test_p1_second_instance_is_refused.log) |
| Live gate | Customer-version validation | `tests/test_live_gate.py` | **not run** | M3_VALIDATION.md section 5 |

## Sequences reproduced by the external review (A01 to A19)

Each was first run against the previous tree, where all 19 failed
([evidence/judge_sequences_before_fix.txt](evidence/judge_sequences_before_fix.txt); `a01b` is an
extra guard that already held). All pass now, in `tests/test_judge_sequences.py`.

| Id | Sequence | Now |
|---|---|---|
| A01 | Lost volume, then A deleted and B made ineligible | resync from the receiver inventory: A `not_observed`, B `out_of_scope`; an incomplete first scan withdraws nothing (`a01b`) |
| A02 | HTTP 200 with HTML, `{}`, unknown result, other delivery id | stays pending, health degraded, delivered once the receiver answers properly |
| A03 | Kill inside the terminal-refusal record | one transaction; after restart the proposal is delivered |
| A04 | Empty session in company 200 | session probe refuses the scan; probe failing disables absence |
| A05 | v2 `notProcessed`, `errorType` only, two results, other transaction | protocol or MI error, never empty |
| A06 | v1 Program-only envelope | refused as unverified empty shape |
| A07 | Reinstall on a host clock behind the old one | generation comes from the receiver; 999 delivered |
| A08 | Notifications beyond `max_pending` | capped at the outbox capacity |
| A09 | Untrusted receiver CA; sustained 503 | health degraded |
| A10 | MI errors carrying proposal and item numbers | report holds error categories only |
| A11 | Conversion 12 changed to 24 | value 240 re-sent |
| A12 | Index grouped by warehouse skips PLPN 200/300 | scan not authoritative without a verified index contract: nothing withdrawn, warning in health |
| A13 | Backoff with 1024 / 5000 attempts | exponent saturated, no overflow |
| A14 | Restored backup | resync, 10 re-sent |
| A15 | Targeted read answered with another key | refused |
| A16 | Compatibility check on an empty source | dependent checks `not_exercised` (the CLI then exits 3, inconclusive), only invoked transactions listed |
| A17 | 99.995 against 100; missing unit | not eligible; value unknown |
| A18 | Cloned volume running next to the original | later registration fences the other copy; no 409; receiver converges |
| A19 | Separators `#` and `*` | refused by configuration |

## Earlier review regressions

`tests/test_review_regressions.py` (r01 to r10): reinstall, wrong URL fixed, two agents with one
identity (now fenced), terminal refusal re-sent, `page_cap` ceiling, released status and filters,
listener binding, stuck-scan health, fixed EXPORTMI table, discovery filter. All passed.
