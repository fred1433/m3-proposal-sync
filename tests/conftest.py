"""Harness fixtures. The M3 side is the simulator in sim/m3sim.py; the SaaS side
is the reference receiver. Assertions are made on the receiver's database and
on the simulator's call log, not on the agent's own view of itself."""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from m3sync.agent import Agent
from m3sync.clock import FakeClock
from m3sync.config import from_dict
from m3sync.miclient import BasicAuth, MIClient, RateLimiter, RetryPolicy, Transport
from m3sync.receiver import ReferenceReceiver
from m3sync.sender import Sender
from m3sync.store import Store
from m3sync.vault import Secret, Vault, create_key_file
from sim.m3sim import M3Sim

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "runs"


# --- PKI ---------------------------------------------------------------------
def _name(cn: str, org: str | None = None) -> x509.Name:
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, cn)]
    if org:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, org))
    return x509.Name(attrs)


def _cert(subject, issuer, key, issuer_key, ca=False, san=None):
    now = dt.datetime.now(dt.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
         .not_valid_after(now + dt.timedelta(days=2))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if san:
        b = b.add_extension(x509.SubjectAlternativeName(san), critical=False)
    return b.sign(issuer_key, hashes.SHA256())


def _write(path: Path, cert, key) -> tuple[str, str]:
    path.with_suffix(".crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    path.with_suffix(".key").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                           serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))
    os.chmod(path.with_suffix(".key"), 0o600)
    return str(path.with_suffix(".crt")), str(path.with_suffix(".key"))


class PKI:
    def __init__(self, d: Path) -> None:
        self.dir = d
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca = _cert(_name("harness-ca"), _name("harness-ca"), ca_key, ca_key, ca=True)
        self.ca, _ = _write(d / "ca", ca, ca_key)
        sk = ec.generate_private_key(ec.SECP256R1())
        self.server = _write(d / "server", _cert(_name("localhost"), ca.subject, sk, ca_key, san=[
            x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), sk)
        self._ca, self._ca_key = ca, ca_key
        rogue_key = ec.generate_private_key(ec.SECP256R1())
        self._rogue = (_cert(_name("rogue-ca"), _name("rogue-ca"), rogue_key, rogue_key, ca=True), rogue_key)
        self.rogue_ca, _ = _write(d / "rogue-ca", *self._rogue)

    def client(self, tenant: str, installation: str, rogue: bool = False) -> tuple[str, str]:
        k = ec.generate_private_key(ec.SECP256R1())
        ca, ca_key = self._rogue if rogue else (self._ca, self._ca_key)
        c = _cert(_name(installation, tenant), ca.subject, k, ca_key)
        return _write(self.dir / f"client-{tenant}-{installation}{'-rogue' if rogue else ''}", c, k)


@pytest.fixture(scope="session")
def pki(tmp_path_factory) -> PKI:
    return PKI(tmp_path_factory.mktemp("pki"))


# --- evidence trace -----------------------------------------------------------
class Trace:
    def __init__(self, name: str, clock: FakeClock | None) -> None:
        self.clock = clock
        self.timing: str | None = None
        self.lines: list[str] = []
        self.path = EVIDENCE / f"{name}.log"

    def __call__(self, msg: str) -> None:
        label = self.clock.label() if self.clock else ""
        line = f"[{label}] {msg}" if label else msg
        self.lines.append(line)

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(f"== {title}")

    def flush(self, header: str) -> None:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.path.write_text(header + "\n" + "\n".join(self.lines) + "\n", encoding="utf-8")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def trace(request, clock):
    name = re.sub(r"[^A-Za-z0-9_]+", "_", request.node.name).strip("_")
    t = Trace(name, clock)
    yield t
    rep = getattr(request.node, "rep_call", None)
    outcome = rep.outcome.upper() if rep else "NOT RUN"
    doc = "\n".join("# " + ln.strip() for ln in (request.node.function.__doc__ or "").strip().splitlines())
    timing = t.timing or ("times are SIMULATED elapsed time (FakeClock)" if t.clock else
                          "real agent processes, wall-clock time")
    t.flush(f"# {request.node.nodeid}\n# outcome: {outcome}\n# {timing}; M3 source behaviour is SIMULATED "
            f"(sim/m3sim.py)\n{doc}\n")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call":
        item.rep_call = rep


# --- components ------------------------------------------------------------------
@pytest.fixture
def sim():
    s = M3Sim().start()
    yield s
    s.stop()


@pytest.fixture
def receiver(tmp_path, pki, clock):
    r = ReferenceReceiver(tmp_path / "receiver.db", *pki.server, client_ca=pki.ca, clock=clock).start()
    yield r
    r.stop()


def base_config(data_dir: Path, tenant="tenant-a", installation="m3-prod", **over) -> dict:
    d = {
        "tenant_id": tenant, "installation_id": installation, "data_dir": str(data_dir),
        "poll_interval_s": 900, "absence_confirmations": 2,
        "m3": {"profile": "grid_basic_v1", "base_url": "http://127.0.0.1:1", "company": "100",
               "rate_per_s": 1000, "burst": 1000, "retry_attempts": 3, "retry_base_s": 0.5, "retry_cap_s": 5,
               "timeout_s": 0.5},
        "extract": {"page_cap": 1000, "index_contract_verified": True},   # simulator index is known
        "filters": {},
        "delivery": {"url": "https://localhost:1/v1/proposal-states"},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k] = {**d[k], **v}
        else:
            d[k] = v
    return d


class Harness:
    def __init__(self, tmp_path: Path, pki: PKI, sim: M3Sim, receiver: ReferenceReceiver, clock, log,
                 tenant="tenant-a", installation="m3-prod", cert=None, **over) -> None:
        self.sim, self.receiver, self.clock = sim, receiver, clock
        cert = cert or pki.client(tenant, installation)
        raw = base_config(tmp_path / f"agent-{tenant}-{installation}", tenant, installation, **over)
        raw["m3"]["base_url"] = sim.base_url
        raw["delivery"] = {"url": receiver.url, "ca_file": pki.ca, "client_cert": cert[0], "client_key": cert[1],
                           "retry_base_s": 30, "retry_cap_s": 120, **over.get("delivery", {})}
        self.cfg = from_dict(raw)
        self.log = log
        self.client = MIClient(self.cfg.m3.profile, sim.base_url,
                               BasicAuth(Secret(sim.user), Secret(sim.password)),
                               Transport(timeout_s=self.cfg.m3.timeout_s),
                               RateLimiter(self.cfg.m3.rate_per_s, self.cfg.m3.burst, clock),
                               RetryPolicy(self.cfg.m3.retry_attempts, self.cfg.m3.retry_base_s,
                                           self.cfg.m3.retry_cap_s, random.Random(1)), clock, log)
        self.store = Store(self.cfg.data_dir, tenant, installation)
        self.agent = Agent(self.cfg, self.client, self.store, Sender(self.cfg.delivery), clock, log,
                           rng=random.Random(7))

    def close(self):
        self.store.close()


@pytest.fixture
def make_harness(tmp_path, pki, clock, trace):
    made = []

    def _make(sim, receiver, **kw):
        h = Harness(tmp_path, pki, sim, receiver, clock, trace, **kw)
        made.append(h)
        return h
    yield _make
    for h in made:
        h.close()


def receiver_view(receiver: ReferenceReceiver, tenant="tenant-a") -> dict[str, tuple[str, int]]:
    return {k[2]: (v["state"], v["seq"]) for k, v in receiver.proposals(tenant).items()}


# --- subprocess agent (real process, real kill) ----------------------------------
def write_process_config(tmp: Path, pki: PKI, sim: M3Sim, receiver: ReferenceReceiver, **over) -> Path:
    key = tmp / "vault.key"
    if not key.exists():
        create_key_file(key)
        v = Vault(tmp / "vault.json", key)
        v.put("m3.user", sim.user)
        v.put("m3.password", sim.password)
    cert = pki.client("tenant-a", "m3-prod")
    d = base_config(tmp / "data", **over)
    d["vault_file"], d["vault_key_file"] = str(tmp / "vault.json"), str(key)
    d["m3"]["base_url"] = sim.base_url
    d["delivery"] = {"url": receiver.url, "ca_file": pki.ca, "client_cert": cert[0], "client_key": cert[1],
                     "retry_base_s": 0, "retry_cap_s": 0}
    path = tmp / "agent.toml"
    path.write_text(_toml(d), encoding="utf-8")
    return path


def _toml(d: dict, prefix="") -> str:
    simple = {k: v for k, v in d.items() if not isinstance(v, dict)}
    bare = re.compile(r"^[A-Za-z0-9_-]+$")
    out = [f"{k if bare.match(k) else json.dumps(k)} = {json.dumps(v)}" for k, v in simple.items()]
    for k, v in d.items():
        if isinstance(v, dict):
            out.append(f"\n[{prefix}{k}]")
            out.append(_toml(v, prefix=f"{prefix}{k}.").strip())
    return "\n".join(out) + "\n"


def run_agent_process(config: Path, crash_at: str | None = None, extra_args=("--once",), timeout=60):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    env.pop("M3SYNC_CRASH_AT", None)
    if crash_at:
        env["M3SYNC_CRASH_AT"] = crash_at
    return subprocess.run([sys.executable, "-m", "m3sync", "run", "--config", str(config), *extra_args],
                          env=env, capture_output=True, text=True, timeout=timeout)
