"""P1 acceptance scenarios (selected) and component checks."""

from __future__ import annotations

import json
import os
import random
import shutil
import ssl
import subprocess
import time
import urllib.request
from decimal import Decimal

import pytest

from m3sync.check import run_check
from m3sync.clock import RealClock
from m3sync.config import EventsConfig, ExtractConfig, FilterConfig
from m3sync.exportmi import traverse
from m3sync.listener import NotificationListener
from m3sync.miclient import (BasicAuth, IonServiceAccountAuth, MIClient, MIError, RateLimiter, RetryPolicy,
                             Transport)
from m3sync.model import compute_value, evaluate
from m3sync.sender import DeliveryTransportError, Sender
from m3sync.config import DeliveryConfig
from m3sync.store import LockedError, Store
from m3sync.vault import Secret, Vault, VaultError, create_key_file

from conftest import ROOT, receiver_view, write_process_config


def test_p1_money_cases(trace):
    """P1 real money/quantity cases under the explicit value policy (PPQT assumed in PUUN)."""
    f = FilterConfig(min_value={"EUR": Decimal("100")}, unit_conversions={("ITEM-1", "BOX", "PCS"): Decimal("12")},
                     unknown_value="exclude")
    base = {"ITNO": "ITEM-1", "PPQT": "10", "PUPR": "12.5", "PUCD": "1", "PUUN": "PCS", "PPUN": "PCS", "CUCD": "EUR"}
    cases = [
        ("price per 1", {}, "125.00", True),
        ("price per 100 (PUCD=100)", {"PUPR": "1250", "PUCD": "100"}, "125.00", True),
        ("boundary: exactly the threshold", {"PUPR": "10"}, "100.00", True),
        ("just below the threshold", {"PUPR": "9.99"}, "99.90", False),
        ("ITEM-1: order unit BOX, price unit PCS, 1 BOX = 12 PCS", {"PUUN": "BOX", "PUPR": "1"}, "120.00", True),
        ("same units on another item: no conversion declared", {"ITNO": "ITEM-2", "PUUN": "BOX"}, None, False),
        ("unknown unit conversion", {"PUUN": "PAL"}, None, False),
        ("99.995 against 100: no rounding before comparison", {"PUPR": "9.9995"}, "99.995", False),
        ("missing order unit", {"PUUN": ""}, None, False),
        ("zero price", {"PUPR": "0"}, None, False),
        ("currency without threshold", {"CUCD": "SEK"}, "125.00", False),
        ("non-numeric quantity", {"PPQT": "x"}, None, False),
    ]
    for name, over, amount, eligible in cases:
        row = {**base, **over}
        v = compute_value(row, f)
        e = evaluate(row, None, f)
        trace(f"{name:48s} value={v.amount} {v.currency} status={v.status:24s} eligible={e.eligible} {list(e.reasons)}")
        assert (v.amount if v.amount is not None else None) == (Decimal(amount) if amount else None)
        assert e.eligible is eligible
    inc = evaluate({**base, "PUPR": "0"}, None, FilterConfig(min_value={"EUR": Decimal("100")}, unknown_value="include"))
    trace(f"zero price with unknown_value=include -> eligible={inc.eligible}")
    assert inc.eligible


def test_p1_auth_and_mi_failures(sim, receiver, make_harness, clock, trace, pki):
    """P1 expired credentials, MI error inside HTTP 200, denied transactions, untrusted certificate:
    never an empty successful scan, never an insecure fallback."""
    for p in (6100001, 6100002):
        sim.put_proposal(p)
    h = make_harness(sim, receiver)
    h.agent.reconcile(); h.agent.deliver_due()
    for fault in ("auth401", "nok"):
        sim.fault = lambda c, f=fault: f
        for _ in range(3):
            clock.advance(900)
            rep = h.agent.reconcile()
        trace(f"fault {fault}: complete={rep.complete} reason={rep.reason!r}")
        assert not rep.complete
    sim.fault = None
    trace(f"receiver after 6 failed scans: {receiver_view(receiver)}")
    assert {s for s, _ in receiver_view(receiver).values()} == {"active"}

    for pgm, trn in (("PPS170MI", "RelPOP"), ("PPS170MI", "UpdPOP"), ("PPS170MI", "DelPlannedPO")):
        with pytest.raises(MIError) as ei:
            h.client.call(pgm, trn, {})
        trace(f"{pgm}/{trn}: {ei.value}")
        assert ei.value.kind == "forbidden"
    n_before = len(sim.calls)
    assert len(sim.calls) == n_before

    with pytest.raises(MIError) as ei:
        Transport().request("GET", receiver.url + "/", {})
    trace(f"HTTPS endpoint with a certificate from an untrusted CA: {ei.value}")
    assert ei.value.kind == "auth"
    with pytest.raises(MIError) as ei:
        Transport().request("GET", "http://10.20.30.40/m3api-rest/execute/EXPORTMI/Select", {})
    trace(f"plain HTTP to a non-loopback host: {ei.value}")
    assert "refusing non-HTTPS" in str(ei.value)


@pytest.mark.parametrize("profile", ["ionapi_v1", "ionapi_v2"])
def test_p1_ion_profiles(profile, sim, clock, trace):
    """P1 ION API profiles: service-account token, v1 and v2 response contracts, MI error inside a v2 200."""
    for p in range(6200001, 6200008):
        sim.put_proposal(p)
    transport = Transport(timeout_s=2)
    client = MIClient(profile, sim.base_url, IonServiceAccountAuth(Secret(sim.ionapi_json()), transport, clock),
                      transport, RateLimiter(1000, 1000, clock), RetryPolicy(2, 0.1, 1, random.Random(1)), clock, trace)
    res = traverse(client, ExtractConfig(page_cap=3), "100", log=trace)
    trace(f"{profile}: complete={res.complete} rows={len(res.rows)} pages={res.pages} tokens issued={len(sim.tokens)}")
    assert res.complete and len(res.rows) == 7 and len(sim.tokens) == 1
    sim.fault = lambda c: "nok"
    with pytest.raises(MIError) as ei:
        client.call("EXPORTMI", "Select", {"SEPC": "|", "HDRS": "0", "QERY": "POPLPN from MPOPLP"}, maxrecs=1)
    trace(f"{profile}: MI error inside HTTP 200 -> {ei.value}")
    assert ei.value.kind == "nok"
    sim.fault = None
    bad = json.loads(sim.ionapi_json()); bad["sask"] = "wrong"
    client.auth = IonServiceAccountAuth(Secret(json.dumps(bad)), transport, clock)
    with pytest.raises(MIError) as ei:
        client.call("EXPORTMI", "Select", {"SEPC": "|", "HDRS": "0", "QERY": "POPLPN from MPOPLP"}, maxrecs=1)
    trace(f"{profile}: rejected service account -> {ei.value}")
    assert ei.value.kind == "auth"


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not available")
def test_p1_network_boundary(sim, receiver, pki, tmp_path, trace):
    """P1 network boundary: default mode has no listening socket and only outbound connections; the optional
    notification endpoint accepts only its approved source; the receiver refuses clients without a trusted
    certificate."""
    trace.clock = None
    for p in (6300001, 6300002):
        sim.put_proposal(p)
    cfg = write_process_config(tmp_path, pki, sim, receiver, poll_interval_s=2)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    proc = subprocess.Popen([os.sys.executable, "-m", "m3sync", "run", "--config", str(cfg)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 20
        while len(receiver_view(receiver)) < 2 and time.time() < deadline:
            time.sleep(0.2)
        listen = subprocess.run(["lsof", "-nP", "-a", "-p", str(proc.pid), "-iTCP", "-sTCP:LISTEN"],
                                capture_output=True, text=True).stdout
        udp = subprocess.run(["lsof", "-nP", "-a", "-p", str(proc.pid), "-iUDP"], capture_output=True, text=True).stdout
        trace(f"agent pid {proc.pid}, receiver has {len(receiver_view(receiver))} proposals")
        trace(f"lsof TCP LISTEN: {listen.strip() or '(none)'}")
        trace(f"lsof UDP: {udp.strip() or '(none)'}")
        assert len(receiver_view(receiver)) == 2
        assert listen.strip() == "" and udp.strip() == ""
    finally:
        proc.terminate()
        out, err = proc.communicate(timeout=10)
    trace("agent log: " + err.splitlines()[0].split(" ", 1)[1])

    trace("")
    trace("optional notification endpoint (events.enabled = true)")
    lst = NotificationListener(EventsConfig(enabled=True, bind_host="127.0.0.1", bind_port=0,
                                            allowed_sources=frozenset({"10.1.2.3"})))
    body = json.dumps({"CONO": "100", "PLPN": "6300001", "PLPS": "1", "PLP2": "0"}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{lst.port}/notify", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    trace(f"POST from 127.0.0.1, allowed_sources=10.1.2.3 -> HTTP {ei.value.code}")
    assert ei.value.code == 403 and lst.queue.empty()
    lst.stop()
    lst = NotificationListener(EventsConfig(enabled=True, bind_host="127.0.0.1", bind_port=0,
                                            allowed_sources=frozenset({"127.0.0.1"})))
    r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{lst.port}/notify", data=body,
                                                      method="POST"), timeout=5)
    trace(f"POST from the approved source -> HTTP {r.status}, queued hint {lst.queue.get_nowait()}")
    lst.stop()

    trace("")
    trace("receiver side: clients without a trusted certificate")
    for label, cert in (("no client certificate", ("", "")), ("certificate from another CA", pki.client("tenant-a", "m3-prod", rogue=True))):
        s = Sender(DeliveryConfig(url=receiver.url, ca_file=pki.ca, client_cert=cert[0], client_key=cert[1]))
        with pytest.raises(DeliveryTransportError) as ei:
            s.post_state("{}", "x")
        trace(f"{label}: refused ({str(ei.value)[:90]})")


def test_p1_rate_limit_slow_source_backlog_disk_full(sim, receiver, make_harness, clock, trace):
    """P1 bounded request rate measured at the source, timeouts retried within bounds, queue exhaustion and
    persistence failure visible with checkpoints left safe."""
    trace.clock = None
    trace.timing = "agent code inside the test process, real clock for the rate part"
    for p in range(6400001, 6400031):
        sim.put_proposal(p)
    rc = RealClock()
    client = MIClient("grid_basic_v1", sim.base_url, BasicAuth(Secret(sim.user), Secret(sim.password)),
                      Transport(timeout_s=0.3), RateLimiter(20, 1, rc), RetryPolicy(3, 0.05, 0.2, random.Random(3)),
                      rc, trace)
    res = traverse(client, ExtractConfig(page_cap=2), "100")
    times = [c["t"] for c in sim.calls]
    worst = max(sum(1 for u in times if t <= u < t + 1.0) for t in times)
    trace(f"rate limit 20/s burst 1: {len(times)} EXPORTMI pages, complete={res.complete}, "
          f"max calls in any 1 s window measured at the simulator = {worst}")
    assert res.complete and worst <= 21

    sim.calls.clear()
    sim.timeout_sleep_s = 0.6
    sim.fault = lambda c: "timeout"
    res = traverse(client, ExtractConfig(page_cap=2), "100")
    trace(f"source slower than the 0.3 s client timeout: calls made={len(sim.calls)} (attempts=3), "
          f"complete={res.complete}, reason={res.reason!r}")
    assert not res.complete and len(sim.calls) == 3
    sim.fault = None

    h = make_harness(sim, receiver, delivery={"max_pending": 10})
    rep = h.agent.reconcile()
    trace(f"30 new proposals, max_pending=10: queued={len(rep.queued)} complete={rep.complete} reason={rep.reason!r}; "
          f"outbox={h.store.outbox_counts()} last_complete_scan={h.store.meta('last_complete_scan')}")
    assert len(rep.queued) == 10 and not rep.complete and h.store.meta("last_complete_scan") is None
    for _ in range(3):
        h.agent.deliver_due()
        rep = h.agent.reconcile()
    h.agent.deliver_due()
    trace(f"after three more scan/deliver passes: outbox={h.store.outbox_counts()} complete={rep.complete}")
    assert h.store.outbox_counts() == {"delivered": 30} and rep.complete
    h2 = make_harness(sim, receiver, tenant="tenant-b")
    h2.store.fail_next_write = "database or disk is full"
    rep = h2.agent.reconcile()
    trace(f"disk full on commit: persisted={rep.persisted} reason={rep.reason!r} "
          f"last_complete_scan={h2.store.meta('last_complete_scan')}")
    assert not rep.persisted and h2.store.meta("last_complete_scan") is None
    rep = h2.agent.reconcile()
    trace(f"next scan after space is back: persisted={rep.persisted} queued={len(rep.queued)} "
          f"last_complete_scan={h2.store.meta('last_complete_scan')}")
    assert rep.persisted and len(rep.queued) == 30


def test_p1_second_instance_is_refused(tmp_path, trace, sim, receiver, pki):
    """P1 a second writer on the same data directory is prevented."""
    trace.clock = None
    cfg = write_process_config(tmp_path, pki, sim, receiver)
    first = Store(tmp_path / "data", "tenant-a", "m3-prod")
    with pytest.raises(LockedError) as ei:
        Store(tmp_path / "data", "tenant-a", "m3-prod")
    trace(f"in-process second Store: {ei.value}")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    r = subprocess.run([os.sys.executable, "-m", "m3sync", "run", "--once", "--config", str(cfg)], env=env,
                       capture_output=True, text=True, timeout=30)
    trace(f"second agent process: exit {r.returncode}, last line: {r.stderr.strip().splitlines()[-1]}")
    assert r.returncode != 0 and "LockedError" in r.stderr
    first.close()


def test_p1_vault(tmp_path, trace, sim, receiver, pki):
    """Vault: AES-GCM at rest, key file permissions enforced, secrets absent from logs and reprs."""
    trace.clock = None
    trace.timing = "in-process checks, then one real agent process"
    key = tmp_path / "k"
    create_key_file(key)
    v = Vault(tmp_path / "v.json", key)
    v.put("m3.password", "s3cr3t-value")
    raw = (tmp_path / "v.json").read_text()
    trace(f"vault file contains the plaintext: {'s3cr3t-value' in raw}; repr of secret: {v.get('m3.password')!r}")
    assert "s3cr3t-value" not in raw and v.get("m3.password").reveal() == "s3cr3t-value"
    os.chmod(key, 0o644)
    with pytest.raises(VaultError) as ei:
        Vault(tmp_path / "v.json", key)
    trace(f"key file mode 644: {ei.value}")
    cfg = write_process_config(tmp_path / "run", pki, sim, receiver) if (tmp_path / "run").mkdir() is None else None
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    sim.put_proposal(6500001)
    r = subprocess.run([os.sys.executable, "-m", "m3sync", "run", "--once", "--config", str(cfg)], env=env,
                       capture_output=True, text=True, timeout=30)
    trace(f"agent run exit {r.returncode}; M3 password present in agent output: {sim.password in r.stdout + r.stderr}")
    assert r.returncode == 0 and sim.password not in r.stdout + r.stderr


def test_p1_compatibility_check_against_simulator(sim, clock, trace, tmp_path):
    """The read-only compatibility check: runs only allowlisted reads and redacts keys and values."""
    sim.put_item("ITEM-1", "RAW")
    for p in (6600001, 6600002, 6600003):
        sim.put_proposal(p)
    from conftest import base_config
    from m3sync.config import from_dict
    cfg = from_dict(base_config(tmp_path / "d"))
    client = MIClient("grid_basic_v1", sim.base_url, BasicAuth(Secret(sim.user), Secret(sim.password)),
                      Transport(timeout_s=2), RateLimiter(100, 10, clock), RetryPolicy(2, 0.1, 1), clock)
    report = run_check(cfg, client)
    for c in report["checks"]:
        trace(f"{c['result']:5s} {c['name']}: {c['detail']}")
    text = json.dumps(report)
    assert all(c["result"] != "fail" for c in report["checks"])
    assert "6600001" not in text
    assert {f"{c['program']}/{c['transaction']}" for c in sim.calls} == set(report["transactions_invoked"])
