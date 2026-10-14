"""Command line: m3sync run | check | vault."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import queue
import sys
import time

from .agent import Agent
from .check import run_check, write_report
from .clock import RealClock
from .config import AgentConfig, load
from .miclient import BasicAuth, IonServiceAccountAuth, MIClient, RateLimiter, RetryPolicy, Transport
from .sender import Sender
from .store import Store
from .vault import Vault, create_key_file


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, file=sys.stderr, flush=True)


def build_client(cfg: AgentConfig, clock) -> MIClient:
    vault = Vault(cfg.vault_file, cfg.vault_key_file)
    transport = Transport(cfg.m3.ca_file, cfg.m3.timeout_s)
    if cfg.m3.profile == "grid_basic_v1":
        auth = BasicAuth(vault.get(cfg.m3.user_secret), vault.get(cfg.m3.password_secret))
    else:
        auth = IonServiceAccountAuth(vault.get(cfg.m3.ionapi_secret), transport, clock)
    return MIClient(cfg.m3.profile, cfg.m3.base_url, auth, transport,
                    RateLimiter(cfg.m3.rate_per_s, cfg.m3.burst, clock),
                    RetryPolicy(cfg.m3.retry_attempts, cfg.m3.retry_base_s, cfg.m3.retry_cap_s), clock, log)


def cmd_run(args) -> int:
    cfg = load(args.config)
    clock = RealClock()
    agent = Agent(cfg, build_client(cfg, clock), Store(cfg.data_dir, cfg.tenant_id, cfg.installation_id),
                  Sender(cfg.delivery), clock, log)
    if args.once:
        rep = agent.reconcile()
        agent.deliver_due()
        log(f"once: complete={rep.complete} reason={rep.reason} rows={rep.rows} queued={len(rep.queued)} "
            f"outbox={agent.store.outbox_counts()}")
        return 0 if rep.persisted else 2
    listener = None
    if cfg.events.enabled:
        from .listener import NotificationListener
        listener = NotificationListener(cfg.events)
        log(f"notification endpoint enabled on {cfg.events.bind_host}:{listener.port}")
    else:
        log("polling mode, no listening socket")
    while True:
        try:
            agent.cycle()
            if listener:
                try:
                    for _ in range(100):          # bounded: reconciliation and delivery keep running
                        agent.handle_notification(listener.queue.get_nowait())
                except queue.Empty:
                    pass
                agent.deliver_due()
            agent.last_error = None
        except Exception as exc:  # keep running; the state on disk is the source of truth
            agent.last_error = f"cycle failed: {type(exc).__name__}: {exc}"
            log(f"ERROR {agent.last_error}")
            agent.write_health()
        time.sleep(1.0)


def cmd_check(args) -> int:
    cfg = load(args.config)
    report = run_check(cfg, build_client(cfg, RealClock()), sample=args.sample)
    write_report(report, args.out)
    log(f"report written to {args.out}")
    results = {c["result"] for c in report["checks"]}
    return 1 if "fail" in results else (3 if "not_exercised" in results else 0)


def cmd_health(args) -> int:
    """Exit 0 when healthy, 1 when degraded, 2 when the agent has not written its
    health file recently (not running, or stuck)."""
    cfg = load(args.config)
    path = os.path.join(cfg.data_dir, "health.json")
    try:
        with open(path, encoding="utf-8") as fh:
            h = json.load(fh)
        stale = time.time() - os.path.getmtime(path) > cfg.health_max_age
    except (OSError, ValueError) as exc:
        print(f"health: unavailable ({exc})")
        return 2
    print(json.dumps(h, indent=1))
    if stale:
        print("health: file not updated recently; agent not running or stuck")
        return 2
    return 0 if h.get("status") == "ok" else 1


def cmd_vault(args) -> int:
    if args.action == "init-key":
        create_key_file(args.key)
        log(f"key written to {args.key} (mode 600)")
        return 0
    v = Vault(args.vault, args.key)
    if args.action == "put":
        value = sys.stdin.readline().rstrip("\n") if args.stdin else getpass.getpass(f"{args.name}: ")
        v.put(args.name, value)
        log(f"stored {args.name}")
    elif args.action == "list":
        print("\n".join(v.names()))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="m3sync")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--once", action="store_true")
    c = sub.add_parser("check")
    c.add_argument("--config", required=True)
    c.add_argument("--out", default="compatibility-report.json")
    c.add_argument("--sample", type=int, default=200)
    hp = sub.add_parser("health")
    hp.add_argument("--config", required=True)
    v = sub.add_parser("vault")
    v.add_argument("action", choices=["init-key", "put", "list"])
    v.add_argument("--vault", default="/data/vault.json")
    v.add_argument("--key", default="/run/secrets/m3sync_vault_key")
    v.add_argument("--name")
    v.add_argument("--stdin", action="store_true")
    args = p.parse_args(argv)
    return {"run": cmd_run, "check": cmd_check, "health": cmd_health, "vault": cmd_vault}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
