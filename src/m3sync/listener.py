"""OPTIONAL notification endpoint, disabled by default.

When events.enabled = true, the agent listens on events.bind_host:bind_port
(an explicit LAN address; empty, 0.0.0.0 and :: are refused) and accepts POST /notify only from
the addresses in events.allowed_sources (the approved upstream component, for
example the host that runs the installation's supported event route). The body
is reduced to a key (CONO, PLPN, PLPS, PLP2); the agent then reads the row
itself. This listener is an acceleration path; reconciliation stays the
source of truth. Enabling it opens an inbound LAN port: it needs the
customer's approval and an internal firewall rule.
"""

from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import EventsConfig


class NotificationListener:
    def __init__(self, cfg: EventsConfig) -> None:
        if not cfg.enabled:
            raise ValueError("events are disabled")
        if cfg.bind_host in ("", "0.0.0.0", "::", "[::]"):
            raise ValueError("refusing to listen on all interfaces; set events.bind_host to a LAN address")
        self.cfg = cfg
        self.queue: queue.Queue[dict] = queue.Queue(maxsize=10_000)
        self.rejected = 0
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                if self.client_address[0] not in listener.cfg.allowed_sources:
                    listener.rejected += 1
                    return self._reply(403)
                if self.path != "/notify":
                    return self._reply(404)
                try:
                    n = min(int(self.headers.get("Content-Length", "0")), 4096)
                    doc = json.loads(self.rfile.read(n))
                    hint = {k: str(doc[k]) for k in ("CONO", "PLPN", "PLPS", "PLP2")}
                except (ValueError, KeyError, TypeError):
                    return self._reply(400)
                try:
                    listener.queue.put_nowait(hint)
                except queue.Full:
                    return self._reply(503)
                self._reply(202)

        self.httpd = ThreadingHTTPServer((cfg.bind_host, cfg.bind_port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
