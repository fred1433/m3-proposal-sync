"""Durable local state (SQLite, WAL, synchronous=FULL).

One transaction per scan persists, together: the observed state of every
changed key, the outbox rows and the scan checkpoint. Either all of it is
durable or none of it is. A write failure (disk full, I/O error) aborts the
scan: the checkpoint does not advance and the next scan starts from the last
durable state.

The local database is a cache of what the receiver holds, never the authority:
at every start the agent registers with the receiver, receives a new
installation generation (fencing token) and the receiver's inventory for this
installation, and `resync()` rewrites the local view to match it. A lost
volume, a restored backup or a cloned volume therefore starts from what the
receiver actually holds, and an older copy still running is fenced.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import faults

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observed (
  key TEXT PRIMARY KEY,
  state TEXT NOT NULL,              -- active | out_of_scope | not_observed | released | ineligible
  exported INTEGER NOT NULL,        -- 1 when the receiver holds (or will hold) a state for this key
  fingerprint TEXT NOT NULL,        -- fingerprint of what the receiver holds; '' = re-send at next observation
  last_seq INTEGER NOT NULL DEFAULT 0,
  missing_complete_scans INTEGER NOT NULL DEFAULT 0,
  last_seen_scan INTEGER
);
CREATE TABLE IF NOT EXISTS outbox (
  seq INTEGER PRIMARY KEY,
  key TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | delivered | conflict | rejected | dropped_by_resync
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS scans (
  scan_id INTEGER PRIMARY KEY,
  started_at REAL NOT NULL,
  finished_at REAL,
  complete INTEGER NOT NULL,
  reason TEXT,
  rows INTEGER NOT NULL
);
"""


class LockedError(Exception):
    pass


class PersistenceError(Exception):
    pass


class BacklogFull(Exception):
    pass


@dataclass
class Change:
    key: str
    state: str           # state to store locally
    fingerprint: str
    emit: dict | None    # payload body to queue, or None
    seen: bool           # observed in this scan / read
    missing: int = 0


def payload_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Store:
    def __init__(self, data_dir: str | os.PathLike, tenant: str, installation: str) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tenant, self.installation = tenant, installation
        self._lock_fh = open(self.dir / "agent.lock", "a+")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_fh.close()
            raise LockedError(f"another agent instance holds {self.dir / 'agent.lock'}") from exc
        self.db = sqlite3.connect(self.dir / "state.db", isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        ident = f"{tenant}:{installation}"
        row = self.db.execute("SELECT v FROM meta WHERE k='identity'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta VALUES ('identity', ?)", (ident,))
        elif row[0] != ident:
            self.close()
            raise PersistenceError(f"data dir belongs to {row[0]}, not {ident}")
        self.fail_next_write: str | None = None  # harness hook: simulate disk full

    def close(self) -> None:
        self.db.close()
        self._lock_fh.close()

    # --- reads -------------------------------------------------------------
    def observed(self) -> dict[str, dict]:
        cur = self.db.execute("SELECT key, state, exported, fingerprint, last_seq, missing_complete_scans FROM observed")
        return {r[0]: {"state": r[1], "exported": bool(r[2]), "fingerprint": r[3], "last_seq": r[4], "missing": r[5]}
                for r in cur}

    def meta(self, k: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def pending(self) -> list[dict]:
        cur = self.db.execute(
            "SELECT seq, key, payload, attempts, next_attempt_at FROM outbox WHERE status='pending' ORDER BY seq")
        return [dict(zip(("seq", "key", "payload", "attempts", "next_attempt_at"), r)) for r in cur]

    def outbox_counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT status, count(*) FROM outbox GROUP BY status").fetchall())

    def capacity_left(self, max_pending: int) -> int:
        n = self.db.execute("SELECT count(*) FROM outbox WHERE status='pending'").fetchone()[0]
        return max(0, max_pending - n)

    def oldest_pending_at(self) -> float | None:
        row = self.db.execute("SELECT min(CAST(json_extract(payload, '$.observed_at') AS REAL)) FROM outbox "
                              "WHERE status='pending'").fetchone()
        return row[0]

    # --- writes ------------------------------------------------------------
    def commit(self, changes: list[Change], scan: dict | None, now: float, max_pending: int) -> list[dict]:
        """Persist changes, queue payloads and (if given) the scan record in one
        transaction. Raises BacklogFull, without writing, when the emitting changes
        exceed the outbox capacity (callers trim them first)."""
        queued = []
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if self.fail_next_write:
                msg, self.fail_next_write = self.fail_next_write, None
                raise sqlite3.OperationalError(msg)
            emits = sum(1 for c in changes if c.emit is not None)
            if emits > self.capacity_left(max_pending):
                self.db.execute("ROLLBACK")
                raise BacklogFull(f"{emits} new deliveries exceed outbox capacity")
            seq = int(self.meta("seq", "0"))
            scan_id = None
            if scan is not None:
                cur = self.db.execute(
                    "INSERT INTO scans (started_at, finished_at, complete, reason, rows) VALUES (?,?,?,?,?)",
                    (scan["started_at"], now, int(scan["complete"]), scan.get("reason"), scan["rows"]))
                scan_id = cur.lastrowid
            for ch in changes:
                prev = self.db.execute("SELECT exported, last_seq FROM observed WHERE key=?", (ch.key,)).fetchone()
                exported, last_seq = (prev or (0, 0))
                if ch.emit is not None:
                    seq += 1
                    body = dict(ch.emit, seq=seq, tenant_id=self.tenant, installation_id=self.installation)
                    self.db.execute("INSERT INTO outbox (seq, key, payload, next_attempt_at) VALUES (?,?,?,?)",
                                    (seq, ch.key, json.dumps(body, sort_keys=True), now))
                    exported, last_seq = 1, seq
                    queued.append(body)
                self.db.execute(
                    "INSERT INTO observed (key, state, exported, fingerprint, last_seq, missing_complete_scans, last_seen_scan) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state, "
                    "exported=excluded.exported, fingerprint=excluded.fingerprint, last_seq=excluded.last_seq, "
                    "missing_complete_scans=excluded.missing_complete_scans, "
                    "last_seen_scan=COALESCE(excluded.last_seen_scan, observed.last_seen_scan)",
                    (ch.key, ch.state, exported, ch.fingerprint, last_seq, ch.missing,
                     scan_id if ch.seen else None))
            self.db.execute("INSERT INTO meta VALUES ('seq', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(seq),))
            if scan is not None and scan["complete"]:
                for k, v in (("last_complete_scan", str(scan_id)), ("last_complete_at", repr(now))):
                    self.db.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
            self.db.execute("COMMIT")
        except sqlite3.Error as exc:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise PersistenceError(str(exc)) from exc
        return queued

    def resync(self, generation: int, inventory: dict[str, dict]) -> dict[str, int]:
        """Make the local view mirror the receiver's inventory for this installation,
        in one transaction:
          * pending deliveries are dropped: the next scan re-queues whatever differs
            from what the receiver holds (they were queued against a view that may
            be stale: restored backup, cloned volume);
          * keys the receiver holds take the receiver's state and fingerprint;
          * keys the receiver does not hold are marked not exported, fingerprint '';
          * absence counters restart at 0, so nothing is withdrawn before
            `absence_confirmations` new authoritative scans."""
        stats = {"dropped_pending": 0, "imported": 0, "reset": 0}
        try:
            self.db.execute("BEGIN IMMEDIATE")
            stats["dropped_pending"] = self.db.execute(
                "UPDATE outbox SET status='dropped_by_resync' WHERE status='pending'").rowcount
            local = {r[0] for r in self.db.execute("SELECT key FROM observed")}
            for key in local - set(inventory):
                self.db.execute("UPDATE observed SET exported=0, fingerprint='', missing_complete_scans=0 "
                                "WHERE key=?", (key,))
                stats["reset"] += 1
            for key, item in inventory.items():
                self.db.execute(
                    "INSERT INTO observed (key, state, exported, fingerprint, missing_complete_scans) "
                    "VALUES (?,?,1,?,0) ON CONFLICT(key) DO UPDATE SET state=excluded.state, exported=1, "
                    "fingerprint=excluded.fingerprint, missing_complete_scans=0",
                    (key, item["state"], item["fingerprint"]))
                stats["imported"] += 1
            self.db.execute("INSERT INTO meta VALUES ('generation', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                            (str(generation),))
            self.db.execute("COMMIT")
        except sqlite3.Error as exc:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise PersistenceError(str(exc)) from exc
        return stats

    def mark_delivered(self, seq: int) -> None:
        self.db.execute("UPDATE outbox SET status='delivered', last_error=NULL WHERE seq=?", (seq,))

    def mark_terminal(self, seq: int, key: str, status: str, error: str) -> None:
        """Record a terminal refusal AND forget what was sent for the key, in one
        transaction, so the next scan queues a fresh delivery of the current state."""
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("UPDATE outbox SET status=?, last_error=? WHERE seq=?", (status, error, seq))
            faults.crash_point("terminal_mid_transaction")
            self.db.execute("UPDATE observed SET fingerprint='' WHERE key=? AND last_seq=?", (key, seq))
            self.db.execute("COMMIT")
        except sqlite3.Error as exc:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise PersistenceError(str(exc)) from exc

    def mark_retry(self, seq: int, next_at: float, error: str) -> None:
        self.db.execute("UPDATE outbox SET attempts=attempts+1, next_attempt_at=?, last_error=? WHERE seq=?",
                        (next_at, error, seq))

    def compact(self, keep: int) -> int:
        """Bound disk use: keep the last `keep` finished outbox rows and scan records.
        The sequence counter lives in meta and is never removed."""
        n = self.db.execute("DELETE FROM outbox WHERE status != 'pending' AND seq <= "
                            "(SELECT max(seq) FROM outbox) - ?", (keep,)).rowcount
        self.db.execute("DELETE FROM scans WHERE scan_id <= (SELECT max(scan_id) FROM scans) - ?", (keep,))
        return n
