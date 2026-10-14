"""Regressions from an independent adversarial review of this repository.
Each test replays a sequence that broke an earlier version."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from m3sync.config import ConfigError, from_dict
from m3sync.exportmi import build_query
from m3sync.listener import NotificationListener
from m3sync.config import EventsConfig

from conftest import ROOT, Harness, _toml, base_config, receiver_view


def sweep(h):
    rep = h.agent.reconcile()
    h.agent.deliver_due()
    return rep


def ppqt(receiver, key, tenant="tenant-a"):
    return receiver.proposals()[(tenant, "m3-prod", key)]["payload"]["fields"]["PPQT"]


# --- delivery after reinstall / misconfiguration / duplicate agent -----------------------
def test_r01_volume_lost_agent_reinstalled_same_identity(sim, receiver, make_harness, clock, trace, tmp_path, pki):
    """Volume /data lost, agent reinstalled with the same tenant/installation: sequence numbers restart at 1
    under a newer installation epoch; the receiver converges to the source, no 409."""
    for i in range(1, 6):
        sim.put_proposal(4200000 + i, PPQT=10)
    h = make_harness(sim, receiver)
    sweep(h)
    for q in (11, 12, 13):
        sim.put_proposal(4200005, PPQT=q)
        clock.advance(900)
        sweep(h)
    trace(f"before loss: generation {h.agent.generation}, receiver {receiver_view(receiver)}")
    h.store.close()
    shutil.rmtree(h.cfg.data_dir)
    trace("volume lost; source changes 4200001 -> PPQT 999 and 4200005 -> PPQT 777; agent reinstalled")
    sim.put_proposal(4200001, PPQT=999)
    sim.put_proposal(4200005, PPQT=777)
    h2 = Harness(tmp_path, pki, sim, receiver, clock, trace)
    clock.advance(900)
    sweep(h2)
    trace(f"new generation {h2.agent.generation}; agent outbox {h2.store.outbox_counts()}")
    trace(f"receiver PPQT 4200001={ppqt(receiver, '100/4200001/1/0')} 4200005={ppqt(receiver, '100/4200005/1/0')}")
    assert h2.store.outbox_counts() == {"delivered": 2}   # only the two changed proposals are re-sent
    h2.close()
    assert ppqt(receiver, "100/4200001/1/0") == "999" and ppqt(receiver, "100/4200005/1/0") == "777"


def test_r02_wrong_delivery_url_then_fixed(sim, receiver, make_harness, clock, trace, tmp_path, pki):
    """Delivery URL mistyped (404): a configuration failure, kept pending and reported in health; after the
    administrator fixes the URL and restarts, the backlog is delivered."""
    for i in range(1, 4):
        sim.put_proposal(4300000 + i)
    h = make_harness(sim, receiver, delivery={"url": receiver.url + "/typo"})
    sweep(h)
    for _ in range(2):
        clock.advance(900)
        sweep(h)
    health = h.agent.health()
    trace(f"with the typo: outbox {h.store.outbox_counts()}, health {health['status']}: {health['problems']}")
    assert h.store.outbox_counts() == {"pending": 3} and health["status"] == "degraded"
    h.store.close()
    h2 = Harness(tmp_path, pki, sim, receiver, clock, trace)
    clock.advance(900)
    sweep(h2)
    clock.advance(900)
    sweep(h2)
    health = h2.agent.health()["status"]
    trace(f"after the fix: outbox {h2.store.outbox_counts()}, receiver {receiver_view(receiver)}, health {health}")
    h2.close()
    assert len(receiver_view(receiver)) == 3 and health == "ok"


def test_r03_two_agents_same_identity(sim, receiver, clock, trace, tmp_path, pki):
    """Two agents wrongly running with the same identity on two volumes (blue/green left running): the
    receiver fences the agent that registered first; the other converges the receiver to the source."""
    sim.put_proposal(4700001, PPQT=1)
    sim.put_proposal(4700002, PPQT=2)
    a = Harness(tmp_path / "a", pki, sim, receiver, clock, trace)
    sweep(a)
    sim.put_proposal(4700002, PPQT=50)
    b = Harness(tmp_path / "b", pki, sim, receiver, clock, trace)
    clock.advance(900)
    sweep(b)
    sim.put_proposal(4700001, PPQT=60)
    clock.advance(900)
    sweep(a)
    clock.advance(900)
    sweep(b)
    outcomes = receiver.query("SELECT outcome, count(*) FROM deliveries GROUP BY outcome")
    trace(f"A outbox {a.store.outbox_counts()}, B outbox {b.store.outbox_counts()}, receiver outcomes {outcomes}")
    trace(f"receiver PPQT 4700001={ppqt(receiver, '100/4700001/1/0')} 4700002={ppqt(receiver, '100/4700002/1/0')}")
    counts = str(a.store.outbox_counts()) + str(b.store.outbox_counts())
    a_problems = " ".join(a.agent.health()["problems"])
    a.close()
    b.close()
    assert "conflict" not in counts
    assert ppqt(receiver, "100/4700001/1/0") == "60" and ppqt(receiver, "100/4700002/1/0") == "50"
    assert a.agent.fenced and "fenced" in a_problems


def test_r04_terminal_refusal_is_resent_at_next_scan(sim, receiver, make_harness, clock, trace):
    """A delivery refused for good (400) is not forgotten: the key is re-observed and re-sent at the next scan."""
    sim.put_proposal(4400001, PPQT=10)
    receiver.force_reply = [(400, {"result": "invalid"})]
    h = make_harness(sim, receiver)
    sweep(h)
    trace(f"after refusal: outbox {h.store.outbox_counts()}, receiver {receiver_view(receiver)}")
    clock.advance(900)
    sweep(h)
    trace(f"next scan: outbox {h.store.outbox_counts()}, receiver {receiver_view(receiver)}")
    assert receiver_view(receiver)["100/4400001/1/0"] == ("active", 2)


# --- traversal, filters, listener, table --------------------------------------------------
def test_r05_page_cap_bounded_by_documented_ceiling(sim, receiver, make_harness, trace, tmp_path):
    """page_cap above the documented 10,000 ceiling is refused; at 10,000, a 10,000-row page is a full page."""
    with pytest.raises(ConfigError) as ei:
        from_dict(base_config(tmp_path / "x", extract={"page_cap": 20_000}))
    trace(f"page_cap=20000: {ei.value}")
    for i in range(10_005):
        sim.proposals[(100, 6000000 + i, 1, 0)] = {**sim.put_proposal(5999999), "PLPN": str(6000000 + i)}
    sim.delete_proposal(5999999)
    h = make_harness(sim, receiver, extract={"page_cap": 10_000})
    rep = h.agent.reconcile()
    trace(f"10,005 rows at page_cap=10000: complete={rep.complete} rows={rep.rows}")
    assert rep.complete and rep.rows == 10_005


def test_r06_released_status_does_not_bypass_filters(sim, receiver, make_harness, clock, trace):
    """A proposal outside the filters is never sent, even with a 'released' status; a key already exported
    moves to released."""
    sim.put_proposal(4400001, WHLO="999", PSTS="90")
    sim.put_proposal(4400002, WHLO="100", PSTS="20")
    h = make_harness(sim, receiver, filters={"warehouses": ["100"], "released_statuses": ["90"]})
    sweep(h)
    sim.put_proposal(4400002, PSTS="90")
    clock.advance(900)
    sweep(h)
    trace(f"receiver: {receiver_view(receiver)}")
    assert receiver_view(receiver) == {"100/4400002/1/0": ("released", 2)}


def test_r07_listener_never_binds_all_interfaces(tmp_path, trace):
    """Events enabled without an explicit LAN bind_host, or with 0.0.0.0 / ::, are refused."""
    for host in ("", "0.0.0.0", "::"):
        with pytest.raises(ConfigError) as ei:
            from_dict(base_config(tmp_path / "d", events={"enabled": True, "bind_host": host,
                                                          "allowed_sources": ["10.0.0.5"]}))
        trace(f"bind_host={host!r}: {ei.value}")
        with pytest.raises(ValueError):
            NotificationListener(EventsConfig(enabled=True, bind_host=host, allowed_sources=frozenset({"10.0.0.5"})))


def test_r08_stuck_scans_are_visible_in_health(sim, receiver, make_harness, clock, trace, tmp_path):
    """A value containing the separator blocks every scan: the reason names it, health turns degraded once
    the last complete scan is older than health_max_age, and `m3sync health` exits 1."""
    sim.put_proposal(4500001)
    h = make_harness(sim, receiver, extract={"page_cap": 2})
    sweep(h)
    sim.put_proposal(4500002, ITNO="AB|C")
    for _ in range(4):
        clock.advance(900)
        rep = sweep(h)
    health = h.agent.health()
    trace(f"scan: complete={rep.complete} reason={rep.reason!r}")
    trace(f"health: {health['status']} {health['problems']}")
    assert not rep.complete and "separator" in rep.reason and health["status"] == "degraded"
    cfg_path = tmp_path / "health.toml"
    cfg_path.write_text(_toml(base_config(h.cfg.data_dir)), encoding="utf-8")
    r = subprocess.run([os.sys.executable, "-m", "m3sync", "health", "--config", str(cfg_path)],
                       env=dict(os.environ, PYTHONPATH=str(ROOT / "src")), capture_output=True, text=True)
    trace(f"m3sync health: exit {r.returncode}")
    assert r.returncode == 1


def test_r09_exportmi_table_is_fixed(tmp_path, trace):
    """EXPORTMI can read any table the account may read; the agent's table and prefix are fixed to MPOPLP/PO."""
    for over in ({"table": "MITMAS"}, {"table": "CMNUSR", "prefix": "JU"}, {"prefix": "MM"}):
        with pytest.raises(ConfigError) as ei:
            from_dict(base_config(tmp_path / "d", extract=over))
        trace(f"{over}: {ei.value}")
    cfg = from_dict(base_config(tmp_path / "d"))
    q = build_query(cfg.extract, ["POPLPN >= 1"])
    trace(f"query: {q}")
    assert " from MPOPLP where " in q


def test_r10_leaving_discovery_filter_is_out_of_scope(sim, receiver, make_harness, clock, trace):
    """A proposal that leaves the discovery filter (FACI A01 -> B02) still exists: out_of_scope, not not_observed."""
    sim.put_proposal(4600001, FACI="A01")
    h = make_harness(sim, receiver, extract={"discovery_equals": {"FACI": "A01"}})
    sweep(h)
    sim.put_proposal(4600001, FACI="B02")
    for _ in range(3):
        clock.advance(900)
        sweep(h)
    payload = receiver.proposals()[("tenant-a", "m3-prod", "100/4600001/1/0")]["payload"]
    trace(f"receiver: {receiver_view(receiver)} reasons={payload['eligibility_reasons']}")
    assert receiver_view(receiver)["100/4600001/1/0"][0] == "out_of_scope"
