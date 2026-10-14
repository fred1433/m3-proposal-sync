"""Crash points used by the failure harness.

When the environment variable M3SYNC_CRASH_AT names a crash point, the agent
process terminates with os._exit(137) the Nth time it reaches that point
(N = M3SYNC_CRASH_HIT, default 1). os._exit skips every cleanup handler, which
is the closest in-process equivalent of `kill -9`.

Crash points (in pipeline order):
  scan_before_commit           scan finished, nothing persisted yet
  after_commit_before_send     observation + outbox row committed, not sent
  after_send_before_ack_record receiver answered (committed), local ack not yet persisted
  terminal_mid_transaction     inside the transaction recording a terminal refusal
"""

from __future__ import annotations

import os

CRASH_POINTS = (
    "scan_before_commit",
    "after_commit_before_send",
    "after_send_before_ack_record",
    "terminal_mid_transaction",
)

_hits: dict[str, int] = {}


def crash_point(name: str) -> None:
    if name not in CRASH_POINTS:
        raise ValueError(f"unknown crash point {name}")
    target = os.environ.get("M3SYNC_CRASH_AT")
    if target != name:
        return
    _hits[name] = _hits.get(name, 0) + 1
    if _hits[name] >= int(os.environ.get("M3SYNC_CRASH_HIT", "1")):
        os._exit(137)
