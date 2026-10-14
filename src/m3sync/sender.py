"""Outbound-only client of the receiver.

The agent opens HTTPS connections to exactly one configured base URL, presents
its client certificate (mTLS) and verifies the receiver against a pinned CA
file. Three calls exist: register (obtain the installation generation), read
the inventory the receiver holds for this installation, and post a proposal
state. Responses are parsed as bounded JSON objects and never interpreted as
commands. There is no tunnel and no remote call path into M3.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.parse

from .config import DeliveryConfig

MAX_RESPONSE = 64 * 1024


class DeliveryTransportError(Exception):
    pass


class Sender:
    def __init__(self, cfg: DeliveryConfig) -> None:
        u = urllib.parse.urlsplit(cfg.url)
        if u.scheme != "https":
            raise ValueError("delivery URL must be https")
        self.host, self.port, self.base = u.hostname, u.port or 443, u.path.rstrip("/")
        self.timeout = cfg.timeout_s
        self.ctx = ssl.create_default_context(cafile=cfg.ca_file)
        self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if cfg.client_cert:
            self.ctx.load_cert_chain(cfg.client_cert, cfg.client_key)

    def call(self, method: str, path: str, body: str | None = None, headers: dict | None = None) -> tuple[int, dict | None]:
        """Returns (HTTP status, JSON object or None when the body is not a bounded JSON object)."""
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self.ctx)
        try:
            conn.request(method, self.base + path, body=body.encode() if body is not None else None,
                         headers={"Content-Type": "application/json", **(headers or {})})
            resp = conn.getresponse()
            raw = resp.read(MAX_RESPONSE + 1)
            doc = None
            if len(raw) <= MAX_RESPONSE:
                try:
                    parsed = json.loads(raw)
                    doc = parsed if isinstance(parsed, dict) else None
                except ValueError:
                    doc = None
            return resp.status, doc
        except (OSError, http.client.HTTPException, socket.timeout) as exc:
            raise DeliveryTransportError(f"{type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()

    def register(self) -> tuple[int, dict | None]:
        return self.call("POST", "/v1/installations/register", "{}")

    def inventory(self, after: str = "", limit: int = 1000) -> tuple[int, dict | None]:
        q = urllib.parse.urlencode({"after": after, "limit": limit})
        return self.call("GET", f"/v1/installations/inventory?{q}")

    def post_state(self, body: str, delivery_id: str) -> tuple[int, dict | None]:
        return self.call("POST", "/v1/proposal-states", body, {"Idempotency-Key": delivery_id})
