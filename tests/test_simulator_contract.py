"""The simulator must not be more permissive than the documented EXPORTMI rules
(one table, capital prefixed fields, no IN(), no ORDER BY), and must apply
the documented 10,000-record ceiling. OR and parentheses are rejected too:
their support is unknown, so the agent must not depend on them."""

from __future__ import annotations

import random

import pytest

from m3sync.miclient import BasicAuth, MIClient, MIError, RateLimiter, RetryPolicy, Transport
from m3sync.vault import Secret


@pytest.fixture
def client(sim, clock):
    return MIClient("grid_basic_v1", sim.base_url, BasicAuth(Secret(sim.user), Secret(sim.password)),
                    Transport(timeout_s=2), RateLimiter(1000, 1000, clock), RetryPolicy(1, 0, 0, random.Random(1)), clock)


@pytest.mark.parametrize("query", [
    "POPLPN from MPOPLP where POPLPN in (1, 2)",
    "POPLPN from MPOPLP order by POPLPN",
    "POPLPN from MPOPLP where POPLPN = 1 or POPLPN = 2",
    "POPLPN from MPOPLP where (POPLPN = 1)",
    "PLPN from MPOPLP",
    "poplpn from MPOPLP",
    "POPLPN from MPOPLP, MITMAS",
    "* from MPOPLP",
])
def test_simulator_rejects_unsupported_exportmi(query, client, trace):
    """Simulator contract: unsupported EXPORTMI syntax is an MI error."""
    with pytest.raises(MIError) as ei:
        client.call("EXPORTMI", "Select", {"SEPC": "|", "HDRS": "0", "QERY": query}, maxrecs=10)
    trace(f"{query!r} -> {ei.value}")
    assert ei.value.kind == "nok"


def test_simulator_limits(sim, client, trace):
    """Simulator contract: maxrecs caps the page, the 10,000-record ceiling applies, HDRS=1 adds a header."""
    for p in range(1, 10_006):
        sim.proposals[(100, p, 1, 0)] = {**sim.put_proposal(9_000_000), "PLPN": str(p)}
    sim.delete_proposal(9_000_000)
    rows = client.call("EXPORTMI", "Select", {"SEPC": "|", "HDRS": "0", "QERY": "POPLPN from MPOPLP"}, maxrecs=7)
    full = client.call("EXPORTMI", "Select", {"SEPC": "|", "HDRS": "0", "QERY": "POPLPN from MPOPLP"}, maxrecs=0)
    hdr = client.call("EXPORTMI", "Select", {"SEPC": "|", "HDRS": "1", "QERY": "POPLPN,POITNO from MPOPLP"}, maxrecs=1)
    trace(f"maxrecs=7 -> {len(rows)} rows; maxrecs=0 over 10005 rows -> {len(full)} rows; HDRS=1 -> {hdr}")
    assert len(rows) == 7 and len(full) == 10_000 and hdr[0]["REPL"] == "POPLPN|POITNO"
