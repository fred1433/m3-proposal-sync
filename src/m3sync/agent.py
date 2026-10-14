"""The agent: reconcile observable proposal state, queue changes durably, deliver.

Contract (state sync, not event replay):
  After connectivity recovers and authoritative reads succeed, the receiver
  converges to the current observable proposal state. Polling does not
  reconstruct intermediate transitions.

Start-up: the agent registers with the receiver, which issues a new
installation generation (fencing token), then reads the receiver's inventory
for this installation and rewrites its local view to match (Store.resync).
This covers a lost volume, a restored backup and a cloned volume; an older
copy still running is refused by the receiver from then on.

Authoritative scan: complete traversal AND session company established by the
context probe AND index contract verified. Only authoritative scans count
towards absence (not_observed). A non-authoritative scan still delivers rows it
positively observed.

Delivery outcomes:
  * 200 with a valid acknowledgement {"result": applied|duplicate|stale,
    "delivery_id": <same id>}: delivered;
  * 200 with anything else (HTML, {}, unknown result, other id): protocol
    failure, kept pending, retried, reported;
  * 409 fenced: another registration superseded this agent; it stops delivering
    and reports it;
  * 401/403/404/421: configuration failure, kept pending, retried, reported;
  * transport errors, 408/429/5xx: transient, kept pending, retried, reported;
  * any other refusal (409 conflict, 400, 413, ...): terminal for that delivery;
    recorded together with clearing the key's fingerprint, in one transaction,
    so the next scan re-sends the current state.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field

from . import faults
from .config import AgentConfig, VERSION_FIELDS
from .exportmi import read_key, traverse
from .miclient import MIClient, MIError
from .model import evaluate, fingerprint, source_key
from .sender import DeliveryTransportError, Sender
from .store import BacklogFull, Change, PersistenceError, Store

CONFIG_FAILURE = (401, 403, 404, 421)
ACK_RESULTS = ("applied", "duplicate", "stale")


@dataclass
class ScanReport:
    complete: bool
    reason: str | None
    rows: int
    queued: list[tuple[str, str, int]] = field(default_factory=list)
    persisted: bool = True
    authoritative: bool = False


@dataclass
class DeliverReport:
    sent: list[tuple[int, int, str]] = field(default_factory=list)   # (seq, http status, result)
    failed: str | None = None


class Agent:
    def __init__(self, cfg: AgentConfig, client: MIClient, store: Store, sender: Sender, clock,
                 log=print, rng: random.Random | None = None) -> None:
        self.cfg = cfg
        self.client = client
        self.store = store
        self.sender = sender
        self.clock = clock
        self.log = log
        self.rng = rng or random.Random()
        self.item_cache: dict[str, tuple[str, float]] = {}
        self.next_sweep_at = 0.0
        self.last_scan: dict = {}
        self.generation: int | None = None
        self.next_register_at = 0.0
        self.register_attempts = 0
        self.fenced = False
        self.delivery_problem: str | None = None
        self.context_problem: str | None = None
        self.last_delivery_ok_at: float | None = None
        self.terminal_since_ok = 0
        self.last_error: str | None = None

    # --- registration and resynchronization -----------------------------------
    def ensure_registered(self) -> bool:
        if self.generation is not None:
            return True
        now = self.clock.now()
        if now < self.next_register_at or self.fenced:
            return False
        try:
            status, doc = self.sender.register()
            if status != 200 or not doc or not isinstance(doc.get("generation"), int):
                raise _Refused(status, doc)
            gen = doc["generation"]
            inventory, after = {}, ""
            while True:
                st, page = self.sender.inventory(after)
                if st != 200 or not page or not isinstance(page.get("items"), list):
                    raise _Refused(st, page)
                for it in page["items"]:
                    inventory[it["source_key"]] = it
                if not page.get("next"):
                    break
                after = page["next"]
            stats = self.store.resync(gen, inventory)
        except (DeliveryTransportError, _Refused, PersistenceError, KeyError, TypeError) as exc:
            delay = self._backoff(self.register_attempts)
            self.register_attempts += 1
            self.next_register_at = now + delay
            kind = "configuration failure" if isinstance(exc, _Refused) and exc.status in CONFIG_FAILURE else "failure"
            self.delivery_problem = f"registration {kind}: {exc} (check delivery.url, certificate, receiver CA)"
            self.log(f"ERROR {self.delivery_problem}; retry in {delay:.0f}s")
            self.write_health()
            return False
        self.generation = gen
        self.register_attempts = 0
        self.delivery_problem = None
        self.log(f"registered: generation {gen}; receiver holds {len(inventory)} keys; local view resynchronized "
                 f"(dropped {stats['dropped_pending']} pending, imported {stats['imported']}, reset {stats['reset']})")
        return True

    # --- enrichment and session context ---------------------------------------
    def _item_group(self, itno: str) -> str | None:
        if not self.cfg.filters.item_groups:
            return None
        now = self.clock.now()
        hit = self.item_cache.get(itno)
        if hit and now - hit[1] < self.cfg.filters.item_group_ttl_s:
            return hit[0]
        rows = self.client.call("MMS200MI", "GetItmBasic", {"ITNO": itno})
        if len(rows) != 1 or "ITGR" not in rows[0]:
            raise MIError("protocol", "GetItmBasic: expected one record with ITGR")
        self.item_cache[itno] = (rows[0]["ITGR"], now)
        return rows[0]["ITGR"]

    def _session_company(self) -> tuple[str, str | None]:
        """('ok'|'wrong'|'unknown', detail). Independent of business rows."""
        pgm, trn = self.cfg.m3.context_probe.split("/")
        try:
            rows = self.client.call(pgm, trn, {})
        except MIError as exc:
            return "unknown", f"context probe {pgm}/{trn} failed ({exc.kind})"
        fld = self.cfg.m3.context_company_field
        if len(rows) != 1 or not rows[0].get(fld):
            return "unknown", f"context probe {pgm}/{trn} returned no {fld}"
        if rows[0][fld] != self.cfg.m3.company:
            return "wrong", f"API session company is {rows[0][fld]!r}, expected {self.cfg.m3.company!r}"
        return "ok", None

    # --- one observed row -> Change ------------------------------------------
    def _observe(self, row: dict[str, str], prev: dict | None, origin: str) -> Change:
        key = source_key(row)
        itgr = self._item_group(row["ITNO"])
        elig = evaluate(row, itgr, self.cfg.filters)
        outside = tuple(f"{f} {row.get(f)} outside discovery filter {v}"
                        for f, v in self.cfg.extract.discovery_equals if row.get(f) != v)
        if outside:
            elig = type(elig)(False, elig.reasons + outside, elig.value)
        exported = bool(prev and prev["exported"])
        released = row.get("PSTS") in self.cfg.filters.released_statuses
        if released and exported:
            state = "released"            # only for keys the receiver already holds
        elif elig.eligible and not released:
            state = "active"
        else:
            state = "out_of_scope"
        projection = {
            "source_key": key, "state": state,
            "fields": {k: v for k, v in row.items() if k not in VERSION_FIELDS},
            "item_group": itgr,
            "value": {"amount": None if elig.value.amount is None else str(elig.value.amount.normalize()),
                      "currency": elig.value.currency, "status": elig.value.status},
            "eligibility_reasons": list(elig.reasons),
        }
        fp = fingerprint(projection)
        if state == "out_of_scope" and not exported:
            return Change(key, "ineligible", fp, None, True)
        if prev and prev["fingerprint"] == fp and prev["state"] == state:
            return Change(key, state, fp, None, True)
        emit = dict(projection, fingerprint=fp, origin=origin, observed_at=self.clock.now(),
                    source_version={k: row.get(k) for k in VERSION_FIELDS})
        return Change(key, state, fp, emit, True)

    def _trim_to_capacity(self, changes: list[Change]) -> tuple[list[Change], int]:
        """Bounded ingestion: keep at most the outbox capacity of emitting changes; the
        others are left unpersisted and will be observed again at the next scan."""
        room = self.store.capacity_left(self.cfg.delivery.max_pending)
        kept, dropped = [], 0
        for c in changes:
            if c.emit is not None:
                if room == 0:
                    dropped += 1
                    continue
                room -= 1
            kept.append(c)
        return kept, dropped

    # --- reconciliation -------------------------------------------------------
    def reconcile(self) -> ScanReport:
        self.ensure_registered()
        started = self.clock.now()
        cfg = self.cfg
        ctx, ctx_detail = self._session_company()
        self.context_problem = ctx_detail
        if ctx == "wrong":
            self.log(f"scan REFUSED: {ctx_detail}; nothing observed or withdrawn")
            self.last_scan = {"at": started, "complete": False, "reason": ctx_detail, "rows": 0}
            try:
                self.store.commit([], {"started_at": started, "complete": False, "reason": ctx_detail, "rows": 0},
                                  self.clock.now(), cfg.delivery.max_pending)
            except (PersistenceError, BacklogFull):
                pass
            self.write_health()
            return ScanReport(False, ctx_detail, 0)
        res = traverse(self.client, cfg.extract, cfg.m3.company, log=self.log)
        complete, reason = res.complete, res.reason
        prev_all = self.store.observed()
        changes: list[Change] = []
        seen: set[str] = set()
        for row in res.rows:
            key = source_key(row)
            seen.add(key)
            try:
                changes.append(self._observe(row, prev_all.get(key), "reconciliation"))
            except MIError as exc:
                complete, reason = False, f"enrichment failed for {key}: {exc.kind}"
        authoritative = complete and ctx == "ok" and cfg.extract.index_contract_verified
        if authoritative:
            for key, prev in prev_all.items():
                if key in seen or not prev["exported"]:
                    continue
                if prev["state"] == "not_observed" and prev["fingerprint"]:
                    continue                     # already reported (empty fingerprint means: resend)
                cono, plpn, plps, plp2 = key.split("/")
                try:
                    row = read_key(self.client, cfg.extract, cfg.m3.company,
                                   {"CONO": cono, "PLPN": plpn, "PLPS": plps, "PLP2": plp2})
                except MIError as exc:
                    self.log(f"absence check {key}: targeted read failed ({exc.kind}); no state change")
                    continue
                if row is not None:
                    self.log(f"absence check {key}: absent from scan but present on targeted read; kept")
                    try:
                        changes.append(self._observe(row, prev, "reconciliation"))
                    except MIError as exc:
                        self.log(f"absence check {key}: enrichment failed ({exc.kind}); no state change")
                    continue
                missing = prev["missing"] + 1
                if missing >= cfg.absence_confirmations:
                    self.log(f"absence check {key}: not found in {missing} authoritative scans and on targeted read "
                             "-> not_observed")
                    proj = {"source_key": key, "state": "not_observed", "fields": None, "item_group": None,
                            "value": None, "eligibility_reasons": []}
                    fp = fingerprint(proj)
                    changes.append(Change(key, "not_observed", fp, dict(
                        proj, fingerprint=fp, origin="reconciliation", observed_at=self.clock.now(),
                        source_version=None,
                        note="absent from consecutive authoritative scans; cause unknown (not inferred as release)"),
                        False, missing))
                else:
                    self.log(f"absence check {key}: missing {missing}/{cfg.absence_confirmations}; waiting")
                    changes.append(Change(key, prev["state"], prev["fingerprint"], None, False, missing))
        else:
            why = reason if not complete else (ctx_detail if ctx != "ok" else "index contract not verified")
            self.log(f"scan not authoritative ({why}); no absence processing, checkpoint not advanced")
        changes, dropped = self._trim_to_capacity(changes)
        if dropped:
            complete, authoritative = False, False
            reason = f"outbox full: {dropped} changes deferred to later scans"
            self.log(f"WARNING {reason}")
        self.last_scan = {"at": started, "complete": complete, "authoritative": authoritative,
                          "reason": reason, "rows": len(res.rows)}
        faults.crash_point("scan_before_commit")
        try:
            queued = self.store.commit(changes, {"started_at": started, "complete": authoritative,
                                                 "reason": reason, "rows": len(res.rows)},
                                       self.clock.now(), cfg.delivery.max_pending)
        except (PersistenceError, BacklogFull) as exc:
            self.log(f"ERROR persistence failed ({exc}); scan discarded, checkpoint not advanced")
            self.last_scan.update(complete=False, reason=f"persistence: {exc}")
            self.write_health()
            return ScanReport(False, f"persistence: {exc}", len(res.rows), persisted=False)
        for q in queued:
            self.log(f"queued seq={q['seq']} {q['source_key']} state={q['state']}")
        if authoritative:
            self.store.compact(cfg.delivery.retention_keep)
        self.write_health()
        return ScanReport(complete, reason, len(res.rows),
                          [(q["source_key"], q["state"], q["seq"]) for q in queued], True, authoritative)

    # --- optional notification path ----------------------------------------
    def handle_notification(self, hint: dict) -> str:
        """A notification only names a key. The agent never trusts its content:
        it reads the row itself and runs it through the same pipeline."""
        if str(hint.get("CONO")) != self.cfg.m3.company:
            self.log(f"notification for CONO {hint.get('CONO')!r} rejected (expected {self.cfg.m3.company!r})")
            return "rejected"
        if self.store.capacity_left(self.cfg.delivery.max_pending) == 0:
            self.log("notification deferred: outbox full; reconciliation will pick the change up")
            return "backlog_full"
        try:
            row = read_key(self.client, self.cfg.extract, self.cfg.m3.company, hint)
        except (MIError, KeyError, ValueError) as exc:
            self.log(f"notification read failed ({type(exc).__name__}); reconciliation will decide")
            return "read_failed"
        if row is None:
            self.log("notification: proposal not found on targeted read; no state change, reconciliation will decide")
            return "unresolved"
        key = source_key(row)
        try:
            ch = self._observe(row, self.store.observed().get(key), "notification")
        except MIError as exc:
            self.log(f"notification enrichment failed ({exc.kind}); reconciliation will decide")
            return "read_failed"
        try:
            queued = self.store.commit([ch], None, self.clock.now(), self.cfg.delivery.max_pending)
        except BacklogFull:
            return "backlog_full"
        except PersistenceError as exc:
            self.log(f"ERROR persistence failed on notification ({exc})")
            return "persist_failed"
        if queued:
            self.log(f"notification queued seq={queued[0]['seq']} {key} state={queued[0]['state']}")
            return "queued"
        return "unchanged"

    # --- delivery -------------------------------------------------------------
    def _backoff(self, attempts: int) -> float:
        d = self.cfg.delivery
        exp = min(max(attempts, 0), 30)          # saturate before exponentiation (no float overflow)
        return self.rng.uniform(0, min(d.retry_cap_s, d.retry_base_s * (2 ** exp)))

    def delivery_id(self, key: str, seq: int) -> str:
        return f"{self.cfg.tenant_id}:{self.cfg.installation_id}:g{self.generation}:{key}:{seq}"

    def stamped(self, item: dict) -> tuple[str, str]:
        body = json.loads(item["payload"])
        did = self.delivery_id(item["key"], item["seq"])
        body.update(delivery_id=did, generation=self.generation)
        return json.dumps(body, sort_keys=True), did

    def _retry(self, item, now, problem: str, rep: DeliverReport) -> None:
        delay = self._backoff(item["attempts"])
        self.store.mark_retry(item["seq"], now + delay, problem)
        self.delivery_problem = problem
        rep.failed = problem
        self.log(f"ERROR deliver seq={item['seq']}: {problem}; kept pending, retry in {delay:.1f}s")

    def deliver_due(self) -> DeliverReport:
        rep = DeliverReport()
        if not self.ensure_registered():
            return rep
        now = self.clock.now()
        for item in self.store.pending():
            if item["next_attempt_at"] > now:
                break          # strict order: later items wait behind a retrying one
            faults.crash_point("after_commit_before_send")
            body, did = self.stamped(item)
            try:
                status, doc = self.sender.post_state(body, did)
            except DeliveryTransportError as exc:
                self._retry(item, now, f"transport failure: {exc}", rep)
                break
            faults.crash_point("after_send_before_ack_record")
            result = (doc or {}).get("result")
            rep.sent.append((item["seq"], status, str(result)))
            if status == 200:
                if result in ACK_RESULTS and doc.get("delivery_id") == did:
                    self.store.mark_delivered(item["seq"])
                    self.delivery_problem = None
                    self.last_delivery_ok_at = now
                    self.terminal_since_ok = 0
                    self.log(f"deliver seq={item['seq']} -> 200 {result}")
                    continue
                self._retry(item, now, "protocol failure: HTTP 200 without a valid acknowledgement "
                                       f"(result={result!r}, delivery id echoed={doc.get('delivery_id') == did if doc else False})",
                            rep)
                break
            if status == 409 and result == "fenced":
                self.fenced = True
                self.delivery_problem = ("fenced: the receiver issued a newer generation for this installation "
                                         "(another agent registered: restored or cloned copy?); this agent stops delivering")
                self.log(f"ERROR {self.delivery_problem}")
                rep.failed = "fenced"
                break
            if status in CONFIG_FAILURE:
                self._retry(item, now, f"configuration failure: HTTP {status} {result} "
                                       "(check delivery.url, client certificate identity, receiver CA)", rep)
                break
            if status in (408, 429) or status >= 500:
                self._retry(item, now, f"receiver unavailable: HTTP {status}", rep)
                break
            kind = "conflict" if status == 409 else "rejected"
            self.store.mark_terminal(item["seq"], item["key"], kind, f"HTTP {status} {result}")
            self.terminal_since_ok += 1
            self.log(f"ERROR deliver seq={item['seq']} -> {status} {result}: terminal for this delivery; "
                     f"{item['key']} will be re-sent with its current state at the next scan")
        self.write_health()
        return rep

    # --- health ---------------------------------------------------------------
    def health(self) -> dict:
        now = self.clock.now()
        max_age = self.cfg.health_max_age
        last_ok = self.store.meta("last_complete_at")
        age = None if last_ok is None else now - float(last_ok)
        oldest = self.store.oldest_pending_at()
        pending_age = None if oldest is None else now - oldest
        problems, warnings = [], []
        if age is None or age > max_age:
            problems.append("no authoritative scan " + ("yet" if age is None else f"for {int(age)} s")
                            + (f"; last scan: {self.last_scan.get('reason')}" if self.last_scan.get("reason") else ""))
        if self.context_problem:
            problems.append(f"session context: {self.context_problem}; absence transitions disabled")
        if not self.cfg.extract.index_contract_verified:
            warnings.append("index contract not verified (extract.index_contract_verified=false): "
                            "absence transitions disabled")
        if self.fenced:
            problems.append("fenced by the receiver: another agent registered this installation")
        if self.delivery_problem:
            problems.append(self.delivery_problem)
        if pending_age is not None and pending_age > max_age:
            problems.append(f"oldest pending delivery is {int(pending_age)} s old")
        if self.terminal_since_ok:
            problems.append(f"{self.terminal_since_ok} deliveries refused for good since the last successful one")
        if self.last_error:
            problems.append(self.last_error)
        return {"status": "degraded" if problems else "ok", "written_at": now, "problems": problems,
                "warnings": warnings, "last_authoritative_scan_age_s": age, "max_age_s": max_age,
                "last_scan": self.last_scan, "last_delivery_ok_at": self.last_delivery_ok_at,
                "oldest_pending_age_s": pending_age, "outbox": self.store.outbox_counts(),
                "generation": self.generation}

    def write_health(self) -> None:
        """health.json in the data directory, read by `m3sync health` (Docker HEALTHCHECK)."""
        path = os.path.join(self.cfg.data_dir, "health.json")
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(self.health(), fh, indent=1, default=str)
            os.replace(path + ".tmp", path)
        except OSError as exc:
            self.log(f"ERROR cannot write health file ({exc})")

    def cycle(self) -> ScanReport | None:
        rep = None
        if self.clock.now() >= self.next_sweep_at:
            self.next_sweep_at = self.clock.now() + self.cfg.poll_interval_s
            rep = self.reconcile()
        self.deliver_due()
        return rep


class _Refused(Exception):
    def __init__(self, status: int, doc) -> None:
        super().__init__(f"HTTP {status} {(doc or {}).get('result') if isinstance(doc, dict) else ''}".strip())
        self.status = status
