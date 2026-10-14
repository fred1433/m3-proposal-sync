"""P0 acceptance scenarios. One test (or one parametrized group) per row of the
acceptance table in ACCEPTANCE.md. Raw traces: evidence/runs/<test name>.log."""

from __future__ import annotations

import json
import sqlite3

import pytest

from m3sync.config import from_dict
from m3sync.faults import CRASH_POINTS
from m3sync.sender import Sender
from m3sync.store import PersistenceError, Store
from sim.m3sim import M3Sim

from conftest import receiver_view, run_agent_process, write_process_config


def sweep(h, trace, title=None):
    if title:
        trace.section(title)
    rep = h.agent.reconcile()
    h.agent.deliver_due()
    return rep


def scan_pages(sim):
    """Page number of an EXPORTMI scan call (not an exact-key read), by distinct query text, so that
    retries of the same page keep the same number and a page fault persists across retries."""
    state: dict[str, int] = {}

    def page_no(call):
        q = call["params"].get("QERY", "")
        if call["program"] != "EXPORTMI" or "PLPN =" in q:
            return None
        if q not in state:
            state[q] = len(state) + 1
        return state[q]
    return state, page_no


def load_fixture_22(sim, lmts="1790000000000"):
    """22 lines over 11 proposals, some PLPS/PLP2 runs straddle page boundaries at
    page_cap=5, all with the SAME modification timestamp."""
    layout = {5000001: [(1, 0), (1, 1), (1, 2)], 5000002: [(1, 0), (2, 0), (3, 0)], 5000003: [(1, 0)],
              5000004: [(1, 0), (1, 1), (1, 2), (1, 3)], 5000005: [(1, 0), (2, 0)],
              5000006: [(1, 0)], 5000007: [(1, 0)], 5000008: [(1, 0)], 5000009: [(1, 0)], 5000010: [(1, 0)],
              5000011: [(1, 0), (2, 0), (3, 0), (4, 0)]}
    keys = []
    for plpn, lines in layout.items():
        for plps, plp2 in lines:
            sim.put_proposal(plpn, plps, plp2, LMTS=lmts, LMDT="20261014")
            keys.append(f"100/{plpn}/{plps}/{plp2}")
    return keys


# ---------------------------------------------------------------------------
def test_headline_sequence(sim, receiver, make_harness, clock, trace):
    """README opening: missed notification -> reconciliation finds it -> receiver commits -> ack lost ->
    retry has no second effect; then an interrupted scan refuses to remove a valid proposal."""
    h = make_harness(sim, receiver, extract={"page_cap": 2})
    sim.put_proposal(4100001, PPQT=40)
    sim.put_proposal(4100002, PPQT=12)
    trace.section("scan 1: two proposals in the source")
    h.agent.cycle()
    trace(f"receiver: {receiver_view(receiver)}")

    clock.advance(120)
    sim.put_proposal(4100003, PPQT=75)
    created = clock.now()
    trace.section("source event")
    trace("source: proposal 100/4100003/1/0 created; notification fixture DROPS its notification")
    receiver.drop_ack_after_commit = 1
    trace("receiver fault armed: next applied delivery commits, then the connection closes before the 200")
    trace.section("scan 2 at the next scheduled poll")
    while "100/4100003/1/0" not in receiver_view(receiver):
        clock.advance(60)
        h.agent.cycle()
    first = receiver.query("SELECT at FROM effects WHERE source_key='100/4100003/1/0'")[0][0]
    trace(f"receiver: 100/4100003/1/0 committed {int(first - created) // 60} min after creation "
          f"(polling interval {int(h.cfg.poll_interval_s // 60)} min; simulated time)")
    for _ in range(3):
        clock.advance(60)
        h.agent.cycle()
    did = h.agent.delivery_id("100/4100003/1/0", 3)
    n_deliv = receiver.query("SELECT count(*) FROM deliveries WHERE delivery_id=?", (did,))[0][0]
    n_eff = receiver.query("SELECT count(*) FROM effects WHERE delivery_id=?", (did,))[0][0]
    trace(f"receiver: delivery {did}: {n_deliv} delivery record, {n_eff} effect; agent outbox {h.store.outbox_counts()}")
    assert n_eff == 1 and h.store.outbox_counts() == {"delivered": 3}

    trace.section("scan 3: 100/4100001/1/0 really deleted in the source; page 2 of the scan fails (HTTP 500, every retry)")
    sim.delete_proposal(4100001)
    state, page_no = scan_pages(sim)
    sim.fault = lambda c: "http500" if page_no(c) == 2 else None
    clock.advance(h.cfg.poll_interval_s)
    rep = h.agent.cycle()
    trace(f"scan result: complete={rep.complete} reason={rep.reason!r}")
    trace(f"receiver: {receiver_view(receiver)}")
    assert not rep.complete
    assert {k: v[0] for k, v in receiver_view(receiver).items()} == {
        "100/4100001/1/0": "active", "100/4100002/1/0": "active", "100/4100003/1/0": "active"}
    assert h.store.meta("last_complete_scan") == "2"


def test_p0_01_missed_notification_recovered(sim, receiver, make_harness, clock, trace):
    """P0 missed notification, proposal remains present: next successful reconciliation discovers it;
    polling interval reported separately from measured recovery latency."""
    h = make_harness(sim, receiver)
    trace.section("scan at T+00:00, source empty")
    h.agent.cycle()
    for offset_min in (2, 14):
        clock.advance(offset_min * 60)   # clock is at the last scan time
        plpn = 4200000 + offset_min
        sim.put_proposal(plpn)
        created = clock.now()
        trace(f"source: 100/{plpn}/1/0 created at minute {offset_min} of the interval; notification dropped")
        while f"100/{plpn}/1/0" not in receiver_view(receiver):
            clock.advance(60)
            h.agent.cycle()
        at = receiver.query("SELECT at FROM effects WHERE source_key=?", (f"100/{plpn}/1/0",))[0][0]
        trace(f"recovered: receiver committed {int(at - created) // 60} min after creation "
              f"(configured interval {int(h.cfg.poll_interval_s // 60)} min)")
        assert at - created <= h.cfg.poll_interval_s + 60
    trace("latency depends on where the change falls in the interval, scan duration and delivery backlog; "
          "the interval is a schedule, not a recovery bound")


def test_p0_02_created_and_removed_between_scans_is_a_blind_spot(sim, receiver, make_harness, clock, trace):
    """P0 proposal created and removed between scans: the harness shows the polling blind spot.
    No invented recovery, no release event, no zero-loss claim."""
    h = make_harness(sim, receiver)
    sim.put_proposal(4300001)
    sweep(h, trace, "scan 1")
    clock.advance(120)
    sim.put_proposal(4300002)
    trace("source: 100/4300002/1/0 created at T+02:00, notification dropped")
    clock.advance(240)
    sim.delete_proposal(4300002)
    trace("source: 100/4300002/1/0 removed at T+06:00, notification dropped")
    clock.advance(h.cfg.poll_interval_s - 360)
    sweep(h, trace, "scan 2 at T+15:00")
    view = receiver_view(receiver)
    trace(f"receiver: {view}")
    trace("BLIND SPOT (by design of state sync): 100/4300002/1/0 existed for 4 minutes and was never observed. "
          "Polling cannot reconstruct it; a durable change source would be required.")
    assert "100/4300002/1/0" not in view
    assert receiver.query("SELECT count(*) FROM effects WHERE source_key='100/4300002/1/0'")[0][0] == 0


def test_p0_03_enumeration_exceeds_cap(sim, receiver, make_harness, clock, trace):
    """P0 enumeration exceeds the cap: every fixture key read through PLPN keyset traversal with
    composite-key boundaries and equal modification values; otherwise the scan is marked incomplete."""
    keys = load_fixture_22(sim)
    h = make_harness(sim, receiver, extract={"page_cap": 5})
    rep = sweep(h, trace, "scan 1: 22 lines, 11 proposals, page_cap=5, identical LMTS on every row")
    queries = [c["params"]["QERY"].split(" from ")[1] for c in sim.calls if c["program"] == "EXPORTMI"]
    for q in queries:
        trace(f"EXPORTMI query: ... from {q}")
    trace(f"scan: complete={rep.complete}, rows={rep.rows}")
    assert rep.complete and rep.rows == 22
    assert sorted(receiver_view(receiver)) == sorted(keys)
    assert all(" in" not in q.lower() and "order" not in q.lower() for q in queries)

    trace.section("scan 2: 5 lines added under one PLPN (fills a page alone); 100/5000003/1/0 deleted")
    for plps in range(1, 6):
        sim.put_proposal(4999999, plps, 0)
    sim.delete_proposal(5000003)
    clock.advance(900)
    rep2 = sweep(h, trace)
    trace(f"scan: complete={rep2.complete} reason={rep2.reason!r}")
    assert not rep2.complete and "page_cap too small" in rep2.reason
    assert receiver_view(receiver)["100/5000003/1/0"][0] == "active"
    assert h.store.meta("last_complete_scan") == "1"


def test_p0_04_middle_page_fails_or_truncated(sim, receiver, make_harness, clock, trace):
    """P0 middle page fails or is truncated: no destructive reconciliation, last complete-scan marker
    does not advance, retry stays safe."""
    keys = load_fixture_22(sim)
    h = make_harness(sim, receiver, extract={"page_cap": 5})
    sweep(h, trace, "scan 1: baseline")
    assert len(receiver_view(receiver)) == 22
    sim.delete_proposal(5000010)
    trace("source: 100/5000010/1/0 really deleted")

    for fault in ("http500", "truncate", "nok"):
        state, page_no = scan_pages(sim)
        sim.fault = lambda c, f=fault: f if page_no(c) == 3 else None
        clock.advance(900)
        rep = sweep(h, trace, f"scan with fault '{fault}' on page 3")
        trace(f"scan: complete={rep.complete} reason={rep.reason!r}; last_complete_scan={h.store.meta('last_complete_scan')}")
        assert not rep.complete
        assert receiver_view(receiver)["100/5000010/1/0"][0] == "active"
        assert h.store.meta("last_complete_scan") == "1"
    sim.fault = None
    for i in (1, 2):
        clock.advance(900)
        rep = sweep(h, trace, f"scan without fault #{i}")
        trace(f"scan: complete={rep.complete}; last_complete_scan={h.store.meta('last_complete_scan')}")
    view = receiver_view(receiver)
    trace(f"receiver 100/5000010/1/0: {view['100/5000010/1/0']}")
    assert view["100/5000010/1/0"][0] == "not_observed"
    assert sum(1 for k in keys if view[k][0] == "active") == 21


def test_p0_05_disappears_between_enumeration_and_detail_read(sim, receiver, make_harness, clock, trace):
    """P0 proposal disappears between enumeration/notification and the targeted read: a missing read is not
    'released'; the documented policy (N complete scans + targeted read) decides."""
    h = make_harness(sim, receiver)
    for p in (4500001, 4500002, 4500003):
        sim.put_proposal(p)
    sweep(h, trace, "scan 1")

    trace.section("notification for 4500001 arrives; the row is deleted before the agent's targeted read")
    sim.before_call = lambda c: sim.delete_proposal(4500001) if "POPLPN = 4500001" in c["params"].get("QERY", "") else None
    res = h.agent.handle_notification({"CONO": "100", "PLPN": "4500001", "PLPS": "1", "PLP2": "0"})
    trace(f"notification outcome: {res}; receiver 4500001: {receiver_view(receiver)['100/4500001/1/0']}")
    assert res == "unresolved" and receiver_view(receiver)["100/4500001/1/0"][0] == "active"
    sim.before_call = None

    trace.section("4500002 hidden from one scan page but present on the targeted read")
    hidden = {}

    def hide(c):
        q = c["params"].get("QERY", "")
        if "PLPN =" not in q and "4500002" not in hidden:
            hidden["4500002"] = sim.proposals.pop((100, 4500002, 1, 0))
        elif "POPLPN = 4500002" in q and hidden:
            sim.proposals[(100, 4500002, 1, 0)] = hidden.pop("4500002")
    sim.before_call = hide
    clock.advance(900)
    sweep(h, trace, "scan 2")
    sim.before_call = None
    clock.advance(900)
    sweep(h, trace, "scan 3")
    view = receiver_view(receiver)
    trace(f"receiver: {view}")
    assert view["100/4500002/1/0"][0] == "active"
    assert view["100/4500001/1/0"][0] == "not_observed"
    assert "released" not in {s for s, _ in view.values()}


def test_p0_06_receiver_commits_ack_lost(sim, receiver, make_harness, clock, trace, pki):
    """P0 receiver commits, acknowledgement is lost: several sends, one durable effect; reuse of the
    delivery identifier with a different payload is detected."""
    h = make_harness(sim, receiver)
    sim.put_proposal(4600001, PPQT=10)
    receiver.drop_ack_after_commit = 2
    trace("receiver fault: the next 2 applied deliveries commit and then drop the connection")
    sweep(h, trace, "scan 1")
    for _ in range(3):
        clock.advance(60)
        h.agent.deliver_due()
    sim.put_proposal(4600001, PPQT=11)
    clock.advance(900)
    sweep(h, trace, "scan 2: quantity change")
    for _ in range(3):
        clock.advance(60)
        h.agent.deliver_due()
    rows = receiver.query("SELECT delivery_id, outcome FROM deliveries ORDER BY delivery_id")
    eff = receiver.query("SELECT delivery_id, count(*) FROM effects GROUP BY delivery_id")
    trace(f"receiver deliveries: {rows}")
    trace(f"receiver effects per delivery id: {eff}; HTTP requests received: {receiver.requests}")
    assert receiver.requests >= 4 and all(n == 1 for _, n in eff) and len(eff) == 2

    trace.section("same delivery id, different payload")
    row = h.store.db.execute("SELECT seq, key, payload FROM outbox WHERE seq=1").fetchone()
    body, did = h.agent.stamped({"seq": row[0], "key": row[1], "payload": row[2]})
    payload = json.loads(body)
    payload["fields"]["PPQT"] = "999"
    status, doc = h.agent.sender.post_state(json.dumps(payload), did)
    trace(f"receiver answer: {status} {doc}")
    assert (status, doc["result"]) == (409, "conflict")
    assert receiver_view(receiver)["100/4600001/1/0"] == ("active", 2)


@pytest.mark.parametrize("crash_at", CRASH_POINTS[:3] + ("after_receiver_commit_ack_lost",))
def test_p0_07_process_killed_at_persistence_boundaries(crash_at, sim, receiver, pki, tmp_path, trace):
    """P0 agent process killed (os._exit, no cleanup) at each persistence boundary: no checkpoint advances
    beyond durably recoverable work; after restart every proposal has exactly one receiver effect."""
    for p in (4700001, 4700002, 4700003):
        sim.put_proposal(p)
    trace.clock = None   # real processes, wall-clock time
    cfg = write_process_config(tmp_path, pki, sim, receiver)
    if crash_at == "after_receiver_commit_ack_lost":
        receiver.drop_ack_after_commit = 1
        point = "after_send_before_ack_record"
        r0 = run_agent_process(cfg)
        trace("run 0: receiver commits seq 1 and drops its acknowledgement; agent keeps seq 1 pending "
              f"(exit {r0.returncode}, receiver effects={len(receiver.effects())})")
        trace("run 1 below resends seq 1 and is killed right after the receiver's answer")
    else:
        point = crash_at
    r1 = run_agent_process(cfg, crash_at=point)
    trace(f"run 1 (M3SYNC_CRASH_AT={point}): exit code {r1.returncode}")
    db = sqlite3.connect(tmp_path / "data" / "state.db")
    scans = db.execute("SELECT scan_id, complete FROM scans").fetchall()
    outbox = db.execute("SELECT seq, status FROM outbox").fetchall()
    marker = db.execute("SELECT v FROM meta WHERE k='last_complete_scan'").fetchone()
    trace(f"after crash: agent scans={scans} outbox={outbox} last_complete_scan={marker}")
    trace(f"after crash: receiver effects={len(receiver.effects())}")
    db.close()
    assert r1.returncode == 137
    r2 = run_agent_process(cfg)
    r3 = run_agent_process(cfg)
    trace(f"run 2 exit {r2.returncode}; run 3 exit {r3.returncode}")
    for line in (r2.stderr + r3.stderr).splitlines():
        trace("  agent: " + line.split(" ", 1)[1])
    eff = receiver.effects()
    per_key = {}
    for e in eff:
        per_key[e[3]] = per_key.get(e[3], 0) + 1
    trace(f"receiver effects per key: {per_key}")
    db = sqlite3.connect(tmp_path / "data" / "state.db")
    trace(f"agent outbox: {db.execute('SELECT seq, status FROM outbox').fetchall()}")
    assert r2.returncode == 0 and r3.returncode == 0
    assert per_key == {"100/4700001/1/0": 1, "100/4700002/1/0": 1, "100/4700003/1/0": 1}
    # pending rows from before a restart are dropped by the start-up resync and re-queued by the next scan
    assert {s for (s,) in db.execute("SELECT status FROM outbox")} <= {"delivered", "dropped_by_resync"}
    db.close()


def test_p0_08_duplicate_and_out_of_order(sim, receiver, make_harness, clock, trace):
    """P0 duplicate and out-of-order observations: event + poll do not duplicate effects, an older result
    does not overwrite a newer one, A -> B -> A keeps its final transition."""
    h = make_harness(sim, receiver)
    sim.put_proposal(4800001, PPQT=10)
    sweep(h, trace, "scan 1: 4800001 qty 10")
    sim.put_proposal(4800001, PPQT=15)
    hint = {"CONO": "100", "PLPN": "4800001", "PLPS": "1", "PLP2": "0"}
    trace.section("qty -> 15; notification delivered twice, then a scan sees the same state")
    trace(f"notification 1: {h.agent.handle_notification(hint)}")
    trace(f"notification 2: {h.agent.handle_notification(hint)}")
    h.agent.deliver_due()
    clock.advance(900)
    sweep(h, trace)
    n = receiver.query("SELECT count(*) FROM effects WHERE source_key='100/4800001/1/0'")[0][0]
    trace(f"receiver effects for 4800001: {n}")
    assert n == 2

    trace.section("A -> B -> A across three scans (qty 15 -> 30 -> 15)")
    for q in (30, 15):
        sim.put_proposal(4800001, PPQT=q)
        clock.advance(900)
        sweep(h, trace)
    final = receiver.proposals()[("tenant-a", "m3-prod", "100/4800001/1/0")]
    trace(f"receiver final: seq={final['seq']} PPQT={final['payload']['fields']['PPQT']}")
    assert final["payload"]["fields"]["PPQT"] == "15" and final["seq"] == 4

    trace.section("out of order: seq 6 reaches the receiver before seq 5")
    for q in (40, 50):
        sim.put_proposal(4800001, PPQT=q)
        clock.advance(900)
        h.agent.reconcile()
    r6 = h.store.db.execute("SELECT seq, key, payload FROM outbox WHERE seq=6").fetchone()
    trace(f"seq 6 sent first: {h.agent.sender.post_state(*h.agent.stamped({'seq': r6[0], 'key': r6[1], 'payload': r6[2]}))}")
    rep = h.agent.deliver_due()
    trace(f"then agent delivers in order: {rep.sent}")
    final = receiver.proposals()[("tenant-a", "m3-prod", "100/4800001/1/0")]
    trace(f"receiver final: seq={final['seq']} PPQT={final['payload']['fields']['PPQT']}")
    assert [r for _, _, r in rep.sent] == ["stale", "duplicate"]
    assert final["seq"] == 6 and final["payload"]["fields"]["PPQT"] == "50"


def test_p0_09_two_installations_same_identifiers(sim, receiver, make_harness, clock, trace, pki, tmp_path):
    """P0 two customers or installations share proposal identifiers: separate state, queues, checkpoints
    and receiver identities; a wrong company context fails visibly."""
    sim_b = M3Sim().start()
    try:
        sim.put_proposal(4900001, PPQT=10)
        sim_b.put_proposal(4900001, PPQT=99)
        ha = make_harness(sim, receiver, tenant="tenant-a")
        hb = make_harness(sim_b, receiver, tenant="tenant-b")
        sweep(ha, trace, "tenant-a scan")
        sweep(hb, trace, "tenant-b scan")
        rows = receiver.proposals()
        for k, v in sorted(rows.items()):
            trace(f"receiver row {k}: PPQT={v['payload']['fields']['PPQT']} seq={v['seq']}")
        assert len(rows) == 2
        assert rows[("tenant-a", "m3-prod", "100/4900001/1/0")]["payload"]["fields"]["PPQT"] == "10"
        assert rows[("tenant-b", "m3-prod", "100/4900001/1/0")]["payload"]["fields"]["PPQT"] == "99"

        trace.section("agent configured as tenant-c but holding tenant-b's certificate")
        hc = make_harness(sim, receiver, tenant="tenant-c", cert=pki.client("tenant-b", "m3-prod"))
        sweep(hc, trace)
        trace(f"agent outbox: {hc.store.outbox_counts()}; receiver tenants: {sorted({k[0] for k in receiver.proposals()})}")
        trace(f"agent health: {hc.agent.health()['status']} {hc.agent.health()['problems']}")
        assert hc.store.outbox_counts() == {"pending": 1}   # registration refused: nothing sent
        assert "configuration failure" in " ".join(hc.agent.health()["problems"])
        assert "tenant-c" not in {k[0] for k in receiver.proposals()}

        trace.section("source session is company 200, agent expects 100")
        sim_c = M3Sim(company="200").start()
        sim_c.put_proposal(4900001)
        hd = make_harness(sim_c, receiver, tenant="tenant-d")
        rep = sweep(hd, trace)
        trace(f"scan: complete={rep.complete} reason={rep.reason!r} queued={rep.queued}")
        sim_c.stop()
        assert not rep.complete and "session company" in rep.reason and rep.queued == []

        trace.section("data directory reused for another identity")
        with pytest.raises(PersistenceError) as ei:
            hd.store.close()
            Store(hd.cfg.data_dir, "tenant-a", "m3-prod")
        trace(f"refused: {ei.value}")
        hd.store = Store(hd.cfg.data_dir, "tenant-d", "m3-prod")
    finally:
        sim_b.stop()


def test_p0_10_eligibility_changes(sim, receiver, make_harness, clock, trace):
    """P0 eligibility changes: status, buyer, value and item-group (enrichment only, proposal row unchanged);
    previously exported proposals do not stay incorrectly active."""
    sim.put_item("ITEM-1", "RAW")
    sim.put_item("ITEM-4", "RAW")
    sim.put_proposal(5100001)                       # status
    sim.put_proposal(5100002)                       # buyer
    sim.put_proposal(5100003)                       # value
    sim.put_proposal(5100004, ITNO="ITEM-4")        # item group via item master only
    sim.put_proposal(5100005, BUYE="OTHER")         # never eligible
    h = make_harness(sim, receiver, filters={"statuses": ["20"], "buyers": ["BUYER1"], "item_groups": ["RAW"],
                                             "min_value": {"EUR": 100}, "item_group_ttl_s": 3600})
    sweep(h, trace, "scan 1: filters status=20, buyer=BUYER1, item group=RAW, value >= 100 EUR (10 x 12.50)")
    trace(f"receiver: {receiver_view(receiver)}")
    assert set(receiver_view(receiver)) == {f"100/510000{i}/1/0" for i in (1, 2, 3, 4)}

    sim.put_proposal(5100001, PSTS="30")
    sim.put_proposal(5100002, BUYE="BUYER9")
    sim.put_proposal(5100003, PUPR="5")
    sim.put_item("ITEM-4", "PACK")
    trace("source: 5100001 status 30, 5100002 buyer BUYER9, 5100003 price 5, item ITEM-4 regrouped to PACK "
          "(MMS200MI only; proposal row 5100004 unchanged)")
    clock.advance(900)
    sweep(h, trace, "scan 2 (item group cache still fresh)")
    trace(f"receiver: {receiver_view(receiver)}")
    clock.advance(3600)
    sweep(h, trace, "scan 3 (item group cache expired)")
    view = receiver_view(receiver)
    trace(f"receiver: {view}")
    assert {k: s for k, (s, _) in view.items()} == {
        "100/5100001/1/0": "out_of_scope", "100/5100002/1/0": "out_of_scope",
        "100/5100003/1/0": "out_of_scope", "100/5100004/1/0": "out_of_scope"}
    payload = receiver.proposals()[("tenant-a", "m3-prod", "100/5100003/1/0")]["payload"]
    trace(f"5100003 reasons: {payload['eligibility_reasons']}")

    sim.put_proposal(5100001, PSTS="20")
    clock.advance(900)
    sweep(h, trace, "scan 4: 5100001 back to status 20")
    assert receiver_view(receiver)["100/5100001/1/0"][0] == "active"
    assert "100/5100005/1/0" not in receiver_view(receiver)
