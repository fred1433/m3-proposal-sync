"""Sequences A01 to A19 from an external review that reproduced defects in an
earlier version of this repository. Each one is replayed here and must pass."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from decimal import Decimal

import pytest

from m3sync.check import run_check
from m3sync.config import ConfigError, FilterConfig, from_dict
from m3sync.miclient import (BasicAuth, IonServiceAccountAuth, MIClient, MIError, RateLimiter, RetryPolicy,
                             Transport, parse_v1, parse_v2)
from m3sync.model import evaluate
from m3sync.vault import Secret

from conftest import Harness, base_config, receiver_view, run_agent_process, write_process_config


def sweep(h, n=1, clock=None):
    rep = None
    for i in range(n):
        if clock is not None and i:
            clock.advance(900)
        rep = h.agent.reconcile()
        h.agent.deliver_due()
    return rep


def view_ppqt(receiver, key):
    p = receiver.proposals()[("tenant-a", "m3-prod", key)]
    return p["state"], (p["payload"].get("fields") or {}).get("PPQT")


# --- recovery: lost volume, clock rollback, restored backup, cloned volume ---------------
def test_a01_lost_volume_then_deleted_and_ineligible(sim, receiver, clock, trace, tmp_path, pki):
    """A01: deliver A and B; lose /data; delete A and move B out of the warehouse filter; reinstall:
    the start-up resync imports what the receiver holds, so A becomes not_observed and B out_of_scope."""
    sim.put_proposal(5300001, WHLO="100")
    sim.put_proposal(5300002, WHLO="100")
    flt = {"warehouses": ["100"]}
    h = Harness(tmp_path, pki, sim, receiver, clock, trace, filters=flt)
    sweep(h)
    h.close()
    shutil.rmtree(h.cfg.data_dir)
    sim.delete_proposal(5300001)
    sim.put_proposal(5300002, WHLO="999")
    trace("volume lost; source: A deleted, B moved to warehouse 999; agent reinstalled")
    h2 = Harness(tmp_path, pki, sim, receiver, clock, trace, filters=flt)
    clock.advance(900)
    sweep(h2, 3, clock)
    trace(f"receiver: {receiver_view(receiver)}; health {h2.agent.health()['status']}")
    assert receiver_view(receiver)["100/5300001/1/0"][0] == "not_observed"
    assert receiver_view(receiver)["100/5300002/1/0"][0] == "out_of_scope"
    h2.close()


def test_a01b_incomplete_first_scan_after_reinstall_withdraws_nothing(sim, receiver, clock, trace, tmp_path, pki):
    """A01 variant: after a reinstall, a failed first scan must not withdraw anything the receiver holds."""
    for p in (5310001, 5310002, 5310003):
        sim.put_proposal(p)
    h = Harness(tmp_path, pki, sim, receiver, clock, trace, extract={"page_cap": 2})
    sweep(h)
    h.close()
    shutil.rmtree(h.cfg.data_dir)
    sim.fault = lambda c: "http500" if ">=" in c["params"].get("QERY", "") else None
    h2 = Harness(tmp_path, pki, sim, receiver, clock, trace, extract={"page_cap": 2})
    clock.advance(900)
    sweep(h2, 3, clock)
    trace(f"three failing scans after reinstall: receiver {receiver_view(receiver)}")
    assert {s for s, _ in receiver_view(receiver).values()} == {"active"}
    h2.close()


def test_a07_clock_rollback_on_reinstall(sim, receiver, clock, trace, tmp_path, pki, monkeypatch):
    """A07: the reinstalled host's clock is behind the previous one; the generation comes from the receiver,
    so the new agent's deliveries still win."""
    sim.put_proposal(5400001, PPQT=10)
    h = Harness(tmp_path, pki, sim, receiver, clock, trace)
    sweep(h)
    h.close()
    shutil.rmtree(h.cfg.data_dir)
    import time as _time
    monkeypatch.setattr(_time, "time_ns", lambda: 1_000)
    monkeypatch.setattr(_time, "time", lambda: 1.0)
    sim.put_proposal(5400001, PPQT=999)
    h2 = Harness(tmp_path, pki, sim, receiver, clock, trace)
    sweep(h2)
    trace(f"host clock set to 1970; receiver: {view_ppqt(receiver, '100/5400001/1/0')}, generation {h2.agent.generation}")
    assert view_ppqt(receiver, "100/5400001/1/0") == ("active", "999")
    h2.close()


def test_a14_restored_backup(sim, receiver, clock, trace, tmp_path, pki):
    """A14: back up /data at quantity 10, deliver 20, restore the backup while the source is back at 10:
    the restored agent resynchronizes from the receiver and re-sends 10."""
    sim.put_proposal(5500001, PPQT=10)
    h = Harness(tmp_path, pki, sim, receiver, clock, trace)
    sweep(h)
    h.close()
    backup = tmp_path / "backup"
    shutil.copytree(h.cfg.data_dir, backup)
    sim.put_proposal(5500001, PPQT=20)
    h2 = Harness(tmp_path, pki, sim, receiver, clock, trace)
    clock.advance(900)
    sweep(h2)
    h2.close()
    trace(f"after backup, source 20 delivered: {view_ppqt(receiver, '100/5500001/1/0')}")
    shutil.rmtree(h.cfg.data_dir)
    shutil.copytree(backup, h.cfg.data_dir)
    sim.put_proposal(5500001, PPQT=10)
    h3 = Harness(tmp_path, pki, sim, receiver, clock, trace)
    clock.advance(900)
    sweep(h3)
    trace(f"backup restored, source back to 10: {view_ppqt(receiver, '100/5500001/1/0')}")
    assert view_ppqt(receiver, "100/5500001/1/0") == ("active", "10")
    h3.close()


def test_a18_cloned_volume(sim, receiver, clock, trace, tmp_path, pki):
    """A18: a real clone of /data (same counters) runs next to the original: the later registration fences
    the other copy; no 409 conflict; the receiver converges through the active copy."""
    sim.put_proposal(5600001, PPQT=1)
    sim.put_proposal(5600002, PPQT=2)
    a = Harness(tmp_path / "a", pki, sim, receiver, clock, trace)
    sweep(a)
    a.close()
    shutil.copytree(tmp_path / "a", tmp_path / "b", ignore=shutil.ignore_patterns("pki*"))
    a = Harness(tmp_path / "a", pki, sim, receiver, clock, trace)
    sim.put_proposal(5600001, PPQT=10)
    clock.advance(900)
    sweep(a)
    b = Harness(tmp_path / "b", pki, sim, receiver, clock, trace)
    sim.put_proposal(5600002, PPQT=20)
    clock.advance(900)
    sweep(b)
    sim.put_proposal(5600001, PPQT=30)
    clock.advance(900)
    sweep(a)
    sweep(b)
    counts = (a.store.outbox_counts(), b.store.outbox_counts())
    a_health = a.agent.health()["problems"]
    trace(f"A outbox {counts[0]} fenced={a.agent.fenced}; B outbox {counts[1]}")
    trace(f"receiver: {view_ppqt(receiver, '100/5600001/1/0')} {view_ppqt(receiver, '100/5600002/1/0')}")
    a.close()
    b.close()
    assert "conflict" not in str(counts) and a.agent.fenced and any("fenced" in p for p in a_health)
    assert view_ppqt(receiver, "100/5600001/1/0") == ("active", "30")
    assert view_ppqt(receiver, "100/5600002/1/0") == ("active", "20")


# --- acknowledgement contract, terminal crash window, health -----------------------------
@pytest.mark.parametrize("reply", ["<html><body>OK</body></html>", {}, {"result": "not_found"},
                                   {"result": "applied", "delivery_id": "someone-else"}],
                         ids=["html", "empty_object", "unknown_result", "other_delivery_id"])
def test_a02_http_200_without_valid_ack_stays_pending(reply, sim, receiver, make_harness, clock, trace):
    """A02: HTTP 200 without a valid acknowledgement never marks a delivery delivered."""
    sim.put_proposal(5700001)
    receiver.force_reply = [(200, reply)]
    h = make_harness(sim, receiver)
    sweep(h)
    health = h.agent.health()
    trace(f"reply {reply!r}: outbox {h.store.outbox_counts()}, health {health['status']} {health['problems']}")
    assert h.store.outbox_counts() == {"pending": 1} and health["status"] == "degraded"
    clock.advance(600)
    h.agent.deliver_due()
    trace(f"next attempt: outbox {h.store.outbox_counts()}, receiver {receiver_view(receiver)}")
    assert receiver_view(receiver) == {"100/5700001/1/0": ("active", 1)}


def test_a03_crash_inside_terminal_refusal_record(sim, receiver, pki, tmp_path, trace):
    """A03: the process is killed inside the transaction that records a terminal refusal: nothing is
    forgotten; after restart the proposal is delivered."""
    trace.clock = None
    sim.put_proposal(5800001)
    cfg = write_process_config(tmp_path, pki, sim, receiver)
    receiver.force_reply = [(400, {"result": "invalid"})]
    r1 = run_agent_process(cfg, crash_at="terminal_mid_transaction")
    db = sqlite3.connect(tmp_path / "data" / "state.db")
    trace(f"run 1 killed inside the refusal transaction: exit {r1.returncode}; outbox {db.execute('SELECT seq, status FROM outbox').fetchall()}")
    db.close()
    r2 = run_agent_process(cfg)
    trace(f"run 2 exit {r2.returncode}; receiver {receiver_view(receiver)}")
    assert r1.returncode == 137 and receiver_view(receiver)["100/5800001/1/0"][0] == "active"


def test_a09_delivery_failures_make_health_degraded(sim, receiver, make_harness, clock, trace, pki):
    """A09: an untrusted receiver CA, and sustained 5xx, turn health degraded while M3 reads succeed."""
    sim.put_proposal(5900001)
    h = make_harness(sim, receiver, delivery={"ca_file": pki.rogue_ca})
    sweep(h, 2, clock)
    hl = h.agent.health()
    trace(f"untrusted receiver CA: health {hl['status']} {hl['problems']}")
    assert hl["status"] == "degraded"
    h2 = make_harness(sim, receiver, tenant="tenant-b")
    receiver.force_reply = [(503, {"result": "busy"})] * 5
    sweep(h2)
    hl = h2.agent.health()
    trace(f"receiver answering 503: health {hl['status']} {hl['problems']}")
    assert hl["status"] == "degraded"


# --- source context and protocol strictness ------------------------------------------------
def test_a04_empty_session_in_wrong_company_withdraws_nothing(sim, receiver, make_harness, clock, trace):
    """A04: the API session switches to company 200, which has no proposals: the session context is checked
    independently of rows, nothing is withdrawn. If the context cannot be established, no absence either."""
    sim.put_proposal(6000001)
    h = make_harness(sim, receiver)
    sweep(h)
    sim.company = "200"
    rep = sweep(h, 3, clock)
    trace(f"session company 200: scan reason {rep.reason!r}; receiver {receiver_view(receiver)}")
    assert receiver_view(receiver)["100/6000001/1/0"][0] == "active"
    sim.company = "100"
    sim.delete_proposal(6000001)
    sim.fault = lambda c: "nok" if c["transaction"] == "GetUserInfo" else None
    sweep(h, 3, clock)
    trace(f"context probe failing, proposal really deleted: receiver {receiver_view(receiver)}; "
          f"health {h.agent.health()['problems']}")
    assert receiver_view(receiver)["100/6000001/1/0"][0] == "active"
    assert any("session context" in p for p in h.agent.health()["problems"])


def test_a05_v2_not_processed_is_not_an_empty_success(sim, clock, trace):
    """A05: a v2 response with notProcessed (or only an errorType) is an error, never an empty result."""
    cases = {
        "notProcessed": {"results": [{"transaction": "Select", "notProcessed": True, "records": []}],
                         "wasTerminated": False, "nrOfSuccessfullTransactions": 0, "nrOfFailedTransactions": 0,
                         "nrOfNotProcessedTransactions": 1},
        "errorType only": {"results": [{"transaction": "Select", "errorType": "ServerReturnedNOK", "records": []}]},
        "two results": {"results": [{"transaction": "Select", "records": []}, {"transaction": "Select", "records": []}]},
        "other transaction": {"results": [{"transaction": "GetPOProp", "records": []}]},
    }
    for name, doc in cases.items():
        with pytest.raises(MIError) as ei:
            parse_v2(json.dumps(doc).encode(), "EXPORTMI", "Select")
        trace(f"{name}: {ei.value.kind}: {ei.value.message}")
    transport = Transport(timeout_s=2)
    client = MIClient("ionapi_v2", sim.base_url, IonServiceAccountAuth(Secret(sim.ionapi_json()), transport, clock),
                      transport, RateLimiter(1000, 1000, clock), RetryPolicy(1, 0, 0), clock)
    sim.raw_response = lambda c: (200, cases["notProcessed"]) if c["program"] == "EXPORTMI" else None
    with pytest.raises(MIError):
        client.call("EXPORTMI", "Select", {"SEPC": "|", "HDRS": "0", "QERY": "POPLPN from MPOPLP"}, maxrecs=1)
    trace("end to end through the ionapi_v2 profile: refused")


def test_a06_v1_program_only_envelope_is_not_an_empty_success(trace):
    """A06: a v1 envelope without MIRecord is not accepted as an empty result (unverified shape)."""
    with pytest.raises(MIError) as ei:
        parse_v1(json.dumps({"Program": "EXPORTMI", "Transaction": "Select"}).encode(), "EXPORTMI", "Select")
    trace(f"Program-only envelope: {ei.value.kind}: {ei.value.message}")
    assert parse_v1(json.dumps({"Program": "EXPORTMI", "Transaction": "Select", "MIRecord": []}).encode(),
                    "EXPORTMI", "Select") == []


def test_a12_index_not_ordered_by_plpn_never_withdraws(sim, receiver, make_harness, clock, trace):
    """A12: an index grouped by warehouse yields sorted pages yet skips PLPN 200 and 300. Without a verified
    index contract the scan is never authoritative: nothing is withdrawn, and health says why."""
    for plpn, whlo in ((100, "A"), (400, "A"), (200, "B"), (300, "B")):
        sim.put_proposal(plpn, WHLO=whlo)
    h = make_harness(sim, receiver, extract={"page_cap": 2, "index_contract_verified": False})
    sweep(h)
    sim.index_key = lambda r: (r["WHLO"], int(r["PLPN"]))
    rep = sweep(h, 3, clock)
    trace(f"warehouse-grouped index: scan rows={rep.rows} complete={rep.complete} authoritative={rep.authoritative}")
    trace(f"receiver: {receiver_view(receiver)}; warnings {h.agent.health()['warnings']}")
    assert rep.rows == 2 and not rep.authoritative
    assert {s for s, _ in receiver_view(receiver).values()} == {"active"}


def test_a15_exact_key_read_checks_the_returned_key(sim, receiver, make_harness, trace):
    """A15: a targeted read for 8100001 that returns the row of 8100002 is refused."""
    sim.put_proposal(8100002)
    row = sim.proposals[(100, 8100002, 1, 0)]
    fields = ("CONO", "PLPN", "PLPS", "PLP2", "FACI", "WHLO", "ITNO", "SUNO", "BUYE", "PSTS", "PPQT", "PUUN",
              "PUPR", "PPUN", "PUCD", "CUCD", "DLDT", "LMDT", "CHNO", "LMTS")
    wrong = {"Program": "EXPORTMI", "Transaction": "Select",
             "MIRecord": [{"NameValue": [{"Name": "REPL", "Value": "|".join(row[f] for f in fields)}]}]}
    sim.raw_response = lambda c: (200, wrong) if "POPLPN = 8100001" in c["params"].get("QERY", "") else None
    h = make_harness(sim, receiver)
    res = h.agent.handle_notification({"CONO": "100", "PLPN": "8100001", "PLPS": "1", "PLP2": "0"})
    trace(f"notification for 8100001 answered with 8100002's row: {res}; outbox {h.store.outbox_counts()}")
    assert res == "read_failed" and h.store.outbox_counts() == {}


def test_a19_reserved_separators_are_refused(sim, tmp_path, clock, trace):
    """A19: '#' and '*' make EXPORTMI prefix values with field names; the configuration refuses them."""
    for sep in ("#", "*"):
        with pytest.raises(ConfigError) as ei:
            from_dict(base_config(tmp_path / "d", extract={"separator": sep}))
        trace(f"separator {sep!r}: {ei.value}")
    sim.put_proposal(8200001)
    client = MIClient("grid_basic_v1", sim.base_url, BasicAuth(Secret(sim.user), Secret(sim.password)),
                      Transport(timeout_s=2), RateLimiter(100, 10, clock), RetryPolicy(1, 0, 0), clock)
    rows = client.call("EXPORTMI", "Select", {"SEPC": "#", "HDRS": "0", "QERY": "POCONO,POPLPN from MPOPLP"}, maxrecs=1)
    trace(f"simulator with SEPC '#': {rows}")


# --- capacity, retries, configuration changes, values, compatibility report ---------------
def test_a08_notifications_respect_outbox_capacity(sim, receiver, make_harness, trace):
    """A08: notifications cannot exceed max_pending."""
    for p in (8300001, 8300002, 8300003):
        sim.put_proposal(p)
    h = make_harness(sim, receiver, delivery={"max_pending": 1})
    out = [h.agent.handle_notification({"CONO": "100", "PLPN": str(p), "PLPS": "1", "PLP2": "0"})
           for p in (8300001, 8300002, 8300003)]
    trace(f"three notifications with max_pending=1: {out}; outbox {h.store.outbox_counts()}")
    assert h.store.outbox_counts() == {"pending": 1}


def test_a13_backoff_never_overflows(sim, receiver, make_harness, clock, trace):
    """A13: a large persisted attempt count does not overflow the backoff computation (float defaults)."""
    h = make_harness(sim, receiver, delivery={"retry_base_s": 2.0, "retry_cap_s": 300.0})
    d = h.agent._backoff(1024)
    sim.put_proposal(8400001)
    receiver.force_reply = [(503, {"result": "busy"})]
    h.agent.reconcile()
    h.store.db.execute("UPDATE outbox SET attempts=5000")
    h.agent.deliver_due()
    trace(f"_backoff(1024) = {d:.1f}s (cap {h.cfg.delivery.retry_cap_s}); delivery pass with attempts=5000 ran: "
          f"outbox {h.store.outbox_counts()}")
    assert 0 <= d <= h.cfg.delivery.retry_cap_s


def test_a11_configuration_change_resends_derived_values(sim, receiver, clock, trace, tmp_path, pki):
    """A11: changing ITEM-1's BOX->PCS conversion from 12 to 24 changes the delivered value."""
    sim.put_proposal(8500001, PPQT=10, PUUN="BOX", PPUN="PCS", PUPR=1)
    for factor in (12, 24):
        h = Harness(tmp_path, pki, sim, receiver, clock, trace, filters={"unit_conversions": {"ITEM-1:BOX->PCS": factor}})
        sweep(h)
        v = receiver.proposals()[("tenant-a", "m3-prod", "100/8500001/1/0")]["payload"]["value"]
        trace(f"conversion {factor}: receiver value {v}")
        h.close()
        clock.advance(900)
    assert Decimal(v["amount"]) == 240


def test_a17_value_policy_edges(trace):
    """A17: 99.995 does not pass a 100 threshold; a missing unit is unknown, not ok."""
    f = FilterConfig(min_value={"EUR": Decimal("100")}, unknown_value="exclude")
    base = {"ITNO": "I", "PPQT": "10", "PUPR": "9.9995", "PUCD": "1", "PUUN": "PCS", "PPUN": "PCS", "CUCD": "EUR"}
    e = evaluate(base, None, f)
    m = evaluate({**base, "PUPR": "20", "PUUN": ""}, None, f)
    trace(f"99.995 vs 100: eligible={e.eligible} {e.reasons}; missing unit: status={m.value.status} eligible={m.eligible}")
    assert not e.eligible and m.value.status == "missing_unit" and not m.eligible


def _check_client(sim, clock):
    return MIClient("grid_basic_v1", sim.base_url, BasicAuth(Secret(sim.user), Secret(sim.password)),
                    Transport(timeout_s=2), RateLimiter(100, 10, clock), RetryPolicy(1, 0, 0), clock)


def test_a10_compatibility_report_does_not_leak_identifiers(sim, clock, trace, tmp_path):
    """A10: MI error messages carrying proposal or item numbers never reach the report."""
    sim.put_proposal(7800001, ITNO="CONFIDENTIAL-ITEM")
    sim.before_call = lambda c: sim.delete_proposal(7800001) if c["transaction"] == "GetPOProp" else None
    report = run_check(from_dict(base_config(tmp_path / "d")), _check_client(sim, clock))
    text = json.dumps(report)
    for c in report["checks"]:
        trace(f"{c['result']:13s} {c['name']}: {c['detail']}")
    assert "7800001" not in text and "CONFIDENTIAL" not in text


def test_a16_compatibility_check_on_empty_source_is_inconclusive(sim, clock, trace, tmp_path):
    """A16: on an empty source, dependent checks are not_exercised and only invoked transactions are listed."""
    report = run_check(from_dict(base_config(tmp_path / "d")), _check_client(sim, clock))
    for c in report["checks"]:
        trace(f"{c['result']:13s} {c['name']}")
    trace(f"transactions invoked: {report['transactions_invoked']}")
    assert {c["result"] for c in report["checks"]} >= {"not_exercised"}
    assert "PPS170MI/GetPOProp" not in report["transactions_invoked"]
