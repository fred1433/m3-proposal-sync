"""Reference receiver: the SaaS-side contract the agent is tested against.

All endpoints are mTLS. The client certificate identifies the installation
(subject O = tenant_id, CN = installation_id).

POST /v1/installations/register
    Issues a new installation generation (monotonic counter held by the
    receiver, a fencing token). From then on, deliveries carrying an older
    generation are refused with 409 {"result":"fenced"}: a restored backup, a
    cloned volume or a second agent started earlier can no longer write.
GET  /v1/installations/inventory?after=<key>&limit=<n>
    What the receiver holds for this installation: source_key, state and the
    fingerprint the agent sent. Used by the agent to resynchronize at start.
POST /v1/proposal-states
    In ONE database transaction:
      1. refuse a generation that is not the current one (409 fenced);
      2. look up (tenant, installation, delivery_id): same payload hash ->
         "duplicate", other hash -> 409 "conflict";
      3. record the delivery; apply the state only if (generation, seq) is
         greater than the stored one for that key, else "stale";
      4. commit, then answer 200 {"result": ..., "delivery_id": ...}.
    The agent accepts an acknowledgement only in that exact shape.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import ssl
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS installations (
  tenant TEXT NOT NULL, installation TEXT NOT NULL, generation INTEGER NOT NULL,
  PRIMARY KEY (tenant, installation));
CREATE TABLE IF NOT EXISTS deliveries (
  tenant TEXT NOT NULL, installation TEXT NOT NULL, delivery_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL, outcome TEXT NOT NULL, received_at REAL NOT NULL,
  PRIMARY KEY (tenant, installation, delivery_id));
CREATE TABLE IF NOT EXISTS proposals (
  tenant TEXT NOT NULL, installation TEXT NOT NULL, source_key TEXT NOT NULL,
  generation INTEGER NOT NULL, seq INTEGER NOT NULL, state TEXT NOT NULL, fingerprint TEXT NOT NULL,
  payload TEXT NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY (tenant, installation, source_key));
CREATE TABLE IF NOT EXISTS effects (
  id INTEGER PRIMARY KEY, tenant TEXT, installation TEXT, delivery_id TEXT,
  source_key TEXT, generation INTEGER, seq INTEGER, state TEXT, at REAL);
"""

REQUIRED = ("delivery_id", "generation", "seq", "tenant_id", "installation_id", "source_key", "state", "fingerprint")
STATES = ("active", "out_of_scope", "not_observed", "released")


def canonical_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ReferenceReceiver:
    def __init__(self, db_path: str | Path, server_cert: str, server_key: str, client_ca: str,
                 host: str = "127.0.0.1", port: int = 0, clock=None) -> None:
        self.db_path = str(db_path)
        self.clock = clock
        db = sqlite3.connect(self.db_path)
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        db.close()
        self.drop_ack_after_commit = 0   # harness: commit, then close the socket without answering
        self.force_reply: list[tuple[int, object]] = []   # harness: answer proposal-states with this, apply nothing
        self.requests = 0
        self.lock = threading.Lock()
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=client_ca)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(server_cert, server_key)
        self.ctx = ctx
        self.host, self._port = host, port
        self.httpd: ThreadingHTTPServer | None = None

    # --- lifecycle --------------------------------------------------------
    def start(self) -> "ReferenceReceiver":
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                receiver._handle(self, "POST")

            def do_GET(self):
                receiver._handle(self, "GET")

            def finish(self):
                try:
                    super().finish()
                except OSError:
                    pass

        class Server(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # a dropped acknowledgement is a harness fault, not an error to print

        httpd = Server((self.host, self._port), Handler)
        httpd.socket = self.ctx.wrap_socket(httpd.socket, server_side=True)
        self._port = httpd.server_address[1]
        self.httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    @property
    def url(self) -> str:
        """Base URL the agent is configured with."""
        return f"https://localhost:{self._port}"

    # --- request handling -------------------------------------------------
    def _now(self) -> float:
        return self.clock.now() if self.clock else time.time()

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, body: bytes, ctype="application/json") -> None:
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _reply(self, h, status: int, doc: dict) -> None:
        self._send(h, status, json.dumps(doc).encode())

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, isolation_level=None, timeout=30)

    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        with self.lock:
            self.requests += 1
        cert = h.connection.getpeercert() or {}
        subject = {k: v for rdn in cert.get("subject", ()) for k, v in rdn}
        tenant, inst = subject.get("organizationName"), subject.get("commonName")
        u = urllib.parse.urlsplit(h.path)
        if method == "POST" and u.path == "/v1/installations/register":
            return self._reply(h, 200, {"result": "registered", "generation": self.register(tenant, inst)})
        if method == "GET" and u.path == "/v1/installations/inventory":
            q = dict(urllib.parse.parse_qsl(u.query))
            return self._reply(h, 200, self.inventory(tenant, inst, q.get("after", ""), min(int(q.get("limit", "1000")), 5000)))
        if method != "POST" or u.path != "/v1/proposal-states":
            return self._reply(h, 404, {"result": "not_found"})
        try:
            n = int(h.headers.get("Content-Length", "0"))
            if n > 1_000_000:
                return self._reply(h, 413, {"result": "too_large"})
            body = json.loads(h.rfile.read(n))
            if any(k not in body for k in REQUIRED) or body["state"] not in STATES or not isinstance(body["seq"], int):
                return self._reply(h, 400, {"result": "invalid"})
        except ValueError:
            return self._reply(h, 400, {"result": "invalid"})
        if tenant != body["tenant_id"] or inst != body["installation_id"]:
            return self._reply(h, 403, {"result": "identity_mismatch"})
        if h.headers.get("Idempotency-Key") != body["delivery_id"]:
            return self._reply(h, 400, {"result": "idempotency_key_mismatch"})
        if self.force_reply:
            status, raw = self.force_reply.pop(0)
            if isinstance(raw, dict):
                raw = json.dumps(raw)
            raw = raw.encode() if isinstance(raw, str) else raw
            return self._send(h, status, raw, "text/html" if raw.startswith(b"<") else "application/json")
        result = self.apply(body)
        if result == "applied" and self.drop_ack_after_commit > 0:
            self.drop_ack_after_commit -= 1
            h.close_connection = True
            h.connection.close()   # committed, but the client never sees the acknowledgement
            return
        self._reply(h, 409 if result in ("conflict", "fenced") else 200,
                    {"result": result, "delivery_id": body["delivery_id"]})

    def register(self, tenant: str, inst: str) -> int:
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO installations VALUES (?,?,1) ON CONFLICT(tenant, installation) "
                       "DO UPDATE SET generation=generation+1", (tenant, inst))
            g = db.execute("SELECT generation FROM installations WHERE tenant=? AND installation=?",
                           (tenant, inst)).fetchone()[0]
            db.execute("COMMIT")
            return g
        finally:
            db.close()

    def inventory(self, tenant: str, inst: str, after: str, limit: int) -> dict:
        rows = self.query("SELECT source_key, state, fingerprint FROM proposals WHERE tenant=? AND installation=? "
                          "AND source_key > ? ORDER BY source_key LIMIT ?", (tenant, inst, after, limit))
        return {"items": [{"source_key": r[0], "state": r[1], "fingerprint": r[2]} for r in rows],
                "next": rows[-1][0] if len(rows) == limit else None}

    def apply(self, body: dict) -> str:
        t, i, did = body["tenant_id"], body["installation_id"], body["delivery_id"]
        ph = canonical_hash(body)
        now = self._now()
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            cur_gen = db.execute("SELECT generation FROM installations WHERE tenant=? AND installation=?",
                                 (t, i)).fetchone()
            if cur_gen is None or body["generation"] != cur_gen[0]:
                db.execute("COMMIT")
                return "fenced"
            row = db.execute("SELECT payload_hash FROM deliveries WHERE tenant=? AND installation=? AND delivery_id=?",
                             (t, i, did)).fetchone()
            if row is not None:
                db.execute("COMMIT")
                return "duplicate" if row[0] == ph else "conflict"
            cur = db.execute("SELECT generation, seq FROM proposals WHERE tenant=? AND installation=? AND source_key=?",
                             (t, i, body["source_key"])).fetchone()
            outcome = "applied" if cur is None or (body["generation"], body["seq"]) > (cur[0], cur[1]) else "stale"
            db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", (t, i, did, ph, outcome, now))
            if outcome == "applied":
                db.execute(
                    "INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant, installation, source_key) "
                    "DO UPDATE SET generation=excluded.generation, seq=excluded.seq, state=excluded.state, "
                    "fingerprint=excluded.fingerprint, payload=excluded.payload, updated_at=excluded.updated_at",
                    (t, i, body["source_key"], body["generation"], body["seq"], body["state"], body["fingerprint"],
                     json.dumps(body, sort_keys=True), now))
                db.execute("INSERT INTO effects (tenant, installation, delivery_id, source_key, generation, seq, state, at) "
                           "VALUES (?,?,?,?,?,?,?,?)",
                           (t, i, did, body["source_key"], body["generation"], body["seq"], body["state"], now))
            db.execute("COMMIT")
            return outcome
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    # --- inspection (tests, demo) -----------------------------------------
    def query(self, sql: str, args=()) -> list[tuple]:
        db = sqlite3.connect(self.db_path)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def proposals(self, tenant: str | None = None) -> dict[tuple, dict]:
        rows = self.query("SELECT tenant, installation, source_key, seq, state, payload, generation FROM proposals")
        return {(r[0], r[1], r[2]): {"seq": r[3], "state": r[4], "payload": json.loads(r[5]), "generation": r[6]}
                for r in rows if tenant is None or r[0] == tenant}

    def effects(self) -> list[tuple]:
        return self.query("SELECT tenant, installation, delivery_id, source_key, seq, state FROM effects ORDER BY id")
