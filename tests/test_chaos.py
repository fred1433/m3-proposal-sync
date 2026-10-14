"""Random kill -9 of the real agent process (loop mode) while the source mutates,
then convergence check. Adapted from the independent review's chaos script."""

from __future__ import annotations

import os
import random
import signal
import subprocess
import sys
import time

from conftest import ROOT, write_process_config


def test_chaos_random_sigkill_while_source_mutates(sim, receiver, pki, tmp_path, trace):
    """SIGKILL at random instants (seeded) while proposals are created, changed, deleted and re-statused;
    after restarts the receiver matches the source and no delivery id has two effects."""
    trace.clock = None
    rng = random.Random(3)
    for i in range(1, 41):
        sim.put_proposal(7000000 + i, PPQT=i)
    cfg = write_process_config(tmp_path, pki, sim, receiver, poll_interval_s=0.3, absence_confirmations=1,
                               extract={"page_cap": 7})
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    kills = 0
    for _ in range(15):
        p = subprocess.Popen([sys.executable, "-m", "m3sync", "run", "--config", str(cfg)], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(rng.uniform(0.15, 1.2))
        k = 7000000 + rng.randint(1, 45)
        op = rng.random()
        if op < 0.6:
            sim.put_proposal(k, PPQT=rng.randint(1, 500))
        elif op < 0.8:
            sim.delete_proposal(k)
        else:
            sim.put_proposal(k, PSTS=rng.choice(["20", "30"]))
        time.sleep(rng.uniform(0.0, 0.6))
        p.send_signal(signal.SIGKILL)
        p.wait()
        kills += 1
    for _ in range(3):
        subprocess.run([sys.executable, "-m", "m3sync", "run", "--config", str(cfg), "--once"], env=env,
                       capture_output=True, text=True, timeout=60)
    truth = {f"100/{k[1]}/{k[2]}/{k[3]}": r["PPQT"] for k, r in sim.proposals.items()}
    view = {k[2]: (v["state"], (v["payload"].get("fields") or {}).get("PPQT")) for k, v in receiver.proposals().items()}
    bad = [(key, q, view.get(key)) for key, q in truth.items() if view.get(key) != ("active", q)]
    bad += [(key, None, v) for key, v in view.items() if key not in truth and v[0] != "not_observed"]
    dup = receiver.query("SELECT delivery_id, count(*) c FROM effects GROUP BY delivery_id HAVING c > 1")
    trace(f"SIGKILLs: {kills}; source proposals: {len(truth)}; receiver rows: {len(view)}")
    trace(f"mismatches source vs receiver: {len(bad)} {bad[:5]}")
    trace(f"delivery ids with more than one effect: {dup}")
    trace(f"receiver outcomes: {receiver.query('SELECT outcome, count(*) FROM deliveries GROUP BY outcome')}")
    assert not bad and not dup
