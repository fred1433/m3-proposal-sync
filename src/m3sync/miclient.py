"""Read-only M3 MI client with explicit endpoint profiles.

Profiles (see M3_VALIDATION.md for the source of each shape):
  grid_basic_v1  on-premises Grid, {base}/m3api-rest/execute/{PGM}/{TRN}, HTTP Basic,
                 response {"MIRecord":[{"NameValue":[{"Name","Value"}]}]}.
  ionapi_v1      ION API gateway, {base}/M3/m3api-rest/execute/{PGM}/{TRN}, bearer
                 token from a service-account password grant, MIRecord response.
  ionapi_v2      ION API gateway, {base}/M3/m3api-rest/v2/execute/{PGM}/{TRN}, bearer
                 token, response {"results":[{"records":[{...}], "errorMessage"...}]}.
Deployment location, authentication and response format are separate
concerns; each profile fixes all three explicitly.

Safety rules enforced here, not left to callers:
  * only the transactions in READ_ONLY_ALLOWLIST can be called;
  * HTTPS is required except for loopback hosts (simulator);
  * an HTTP 200 whose body carries an MI error is an error, never an empty result;
  * a body that matches neither success nor error shape is a protocol error;
  * all calls (sweeps, event reads, retries) share one rate limiter.
"""

from __future__ import annotations

import base64
import http.client
import json
import random
import socket
import ssl
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

from .vault import Secret

READ_ONLY_ALLOWLIST = frozenset({
    ("EXPORTMI", "Select"),
    ("PPS170MI", "GetPOProp"),
    ("MMS200MI", "GetItmBasic"),
    ("PPS170MI", "GetUserInfo"),   # session context probe (generic transaction)
})


class MIError(Exception):
    """kind: forbidden | auth | transport | http | nok | protocol"""

    def __init__(self, kind: str, message: str, *, transient: bool = False,
                 code: str | None = None, field: str | None = None, status: int | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.transient = transient
        self.code = code
        self.field = field
        self.status = status


class RateLimiter:
    """Token bucket shared by every MI call of one agent."""

    def __init__(self, rate_per_s: float, burst: int, clock) -> None:
        self.rate = float(rate_per_s)
        self.capacity = max(1, int(burst))
        self.tokens = float(self.capacity)
        self.clock = clock
        self.last = clock.now()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = self.clock.now()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            self.clock.sleep(wait)


@dataclass
class RetryPolicy:
    attempts: int = 4
    base_s: float = 1.0
    cap_s: float = 30.0
    rng: random.Random = field(default_factory=random.Random)

    def delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter."""
        return self.rng.uniform(0, min(self.cap_s, self.base_s * (2 ** attempt)))


class Transport:
    """Minimal HTTP(S) transport. TLS verification cannot be disabled."""

    def __init__(self, ca_file: str | None = None, timeout_s: float = 30.0) -> None:
        self.timeout_s = timeout_s
        self.ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()

    def request(self, method: str, url: str, headers: dict, body: bytes | None = None) -> tuple[int, bytes]:
        u = urllib.parse.urlsplit(url)
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port, timeout=self.timeout_s, context=self.ctx)
        elif u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"):
            conn = http.client.HTTPConnection(u.hostname, u.port, timeout=self.timeout_s)
        else:
            raise MIError("transport", f"refusing non-HTTPS endpoint {u.scheme}://{u.hostname}")
        path = u.path + (f"?{u.query}" if u.query else "")
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        except ssl.SSLCertVerificationError as exc:
            raise MIError("auth", f"untrusted server certificate: {exc.verify_message}") from exc
        except (OSError, http.client.HTTPException, socket.timeout) as exc:
            raise MIError("transport", f"{type(exc).__name__}: {exc}", transient=True) from exc
        finally:
            conn.close()


class BasicAuth:
    def __init__(self, user: Secret, password: Secret) -> None:
        self._user, self._password = user, password

    def headers(self) -> dict:
        tok = base64.b64encode(f"{self._user.reveal()}:{self._password.reveal()}".encode()).decode()
        return {"Authorization": f"Basic {tok}"}

    def invalidate(self) -> bool:
        return False


class IonServiceAccountAuth:
    """Service-account (resource-owner password) grant, as described for ION API
    backend service accounts. Field names follow the .ionapi file (ci, cs, saak,
    sask, pu, ot). Token renewal: re-requested before expiry and once on 401."""

    def __init__(self, ionapi: Secret, transport: Transport, clock) -> None:
        self._conf = json.loads(ionapi.reveal())
        self._transport = transport
        self._clock = clock
        self._token: str | None = None
        self._exp = 0.0

    def _fetch(self) -> None:
        c = self._conf
        body = urllib.parse.urlencode({
            "grant_type": "password", "client_id": c["ci"], "client_secret": c["cs"],
            "username": c["saak"], "password": c["sask"],
        }).encode()
        status, raw = self._transport.request(
            "POST", c["pu"].rstrip("/") + "/" + c["ot"].lstrip("/"),
            {"Content-Type": "application/x-www-form-urlencoded"}, body)
        if status != 200:
            raise MIError("auth", f"token endpoint returned HTTP {status}", status=status)
        doc = json.loads(raw)
        self._token = doc["access_token"]
        self._exp = self._clock.now() + float(doc.get("expires_in", 3600)) - 60

    def headers(self) -> dict:
        if not self._token or self._clock.now() >= self._exp:
            self._fetch()
        return {"Authorization": f"Bearer {self._token}"}

    def invalidate(self) -> bool:
        self._token = None
        return True


def _load(raw: bytes) -> dict:
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise MIError("protocol", "response is not JSON") from exc
    if not isinstance(doc, dict):
        raise MIError("protocol", "response is not a JSON object")
    return doc


def _records(recs, label: str) -> list[dict]:
    if not isinstance(recs, list) or not all(isinstance(r, dict) for r in recs):
        raise MIError("protocol", f"{label}: records are not a list of objects")
    return recs


def parse_v1(raw: bytes, program: str, transaction: str) -> list[dict[str, str]]:
    """Success requires an MIRecord list (possibly empty). A Program-only envelope is
    NOT accepted as an empty success: that shape is unverified (M3_VALIDATION C5)."""
    doc = _load(raw)
    err_type = doc.get("@type") or doc.get("ErrorType")
    if err_type or "ErrorMessage" in doc or ("Message" in doc and "MIRecord" not in doc):
        raise MIError("nok", str(doc.get("Message") or doc.get("ErrorMessage") or err_type),
                      code=doc.get("@code") or doc.get("ErrorCode"), field=doc.get("@field") or doc.get("ErrorField"))
    if "MIRecord" not in doc:
        raise MIError("protocol", "v1 response without MIRecord (unverified empty shape is not accepted)")
    for k, expected in (("Program", program), ("Transaction", transaction)):
        if k in doc and doc[k] != expected:
            raise MIError("protocol", f"v1 response for {k} {doc[k]!r}, expected {expected!r}")
    rows = []
    for rec in _records(doc["MIRecord"], "MIRecord"):
        nv = rec.get("NameValue")
        if not isinstance(nv, list) or not all(isinstance(x, dict) and "Name" in x for x in nv):
            raise MIError("protocol", "MIRecord without a NameValue list")
        rows.append({x["Name"]: (x.get("Value") or "").rstrip() for x in nv})
    return rows


def parse_v2(raw: bytes, program: str, transaction: str) -> list[dict[str, str]]:
    """Success requires exactly one result for the expected transaction, executed
    (not notProcessed), with no error indicator anywhere in the envelope."""
    doc = _load(raw)
    results = doc.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise MIError("protocol", "v2 response must carry exactly one result")
    r = results[0]
    if any(r.get(k) for k in ("errorMessage", "errorCode", "errorType", "errorField")) \
            or doc.get("wasTerminated") or doc.get("nrOfFailedTransactions") or doc.get("terminationErrorType"):
        raise MIError("nok", str(r.get("errorMessage") or doc.get("terminationReason") or r.get("errorType")
                                 or "transaction failed"), code=r.get("errorCode"), field=r.get("errorField"))
    if r.get("notProcessed") or doc.get("nrOfNotProcessedTransactions"):
        raise MIError("protocol", "v2 transaction not processed")
    if r.get("transaction") not in (None, transaction):
        raise MIError("protocol", f"v2 result for {r.get('transaction')!r}, expected {transaction!r}")
    if doc.get("nrOfSuccessfullTransactions") not in (None, 1):
        raise MIError("protocol", "v2 response does not report one successful transaction")
    return [{k: ("" if v is None else str(v)).rstrip() for k, v in rec.items()}
            for rec in _records(r.get("records"), "v2 result")]


class MIClient:
    def __init__(self, profile: str, base_url: str, auth, transport: Transport, limiter: RateLimiter,
                 retry: RetryPolicy, clock, log: Callable[[str], None] = lambda s: None) -> None:
        self.profile = profile
        self.base = base_url.rstrip("/")
        self.auth = auth
        self.transport = transport
        self.limiter = limiter
        self.retry = retry
        self.clock = clock
        self.log = log
        self.calls = 0
        self.invoked: set[str] = set()

    def _url(self, program: str, transaction: str, inputs: dict, maxrecs: int | None) -> str:
        q = {k: v for k, v in inputs.items()}
        if self.profile in ("grid_basic_v1", "ionapi_v1"):
            root = "/m3api-rest/execute" if self.profile == "grid_basic_v1" else "/M3/m3api-rest/execute"
            matrix = f";maxrecs={maxrecs}" if maxrecs is not None else ""
            return f"{self.base}{root}/{program}/{transaction}{matrix}?{urllib.parse.urlencode(q)}"
        if maxrecs is not None:
            q["maxrecs"] = str(maxrecs)
        return f"{self.base}/M3/m3api-rest/v2/execute/{program}/{transaction}?{urllib.parse.urlencode(q)}"

    def call(self, program: str, transaction: str, inputs: dict, maxrecs: int | None = None) -> list[dict[str, str]]:
        if (program, transaction) not in READ_ONLY_ALLOWLIST:
            raise MIError("forbidden", f"{program}/{transaction} is not in the read-only allowlist")
        url = self._url(program, transaction, inputs, maxrecs)
        parse = parse_v2 if self.profile == "ionapi_v2" else parse_v1
        reauthed = False
        last: MIError | None = None
        attempt = 0
        while attempt < self.retry.attempts:
            self.limiter.acquire()
            self.calls += 1
            self.invoked.add(f"{program}/{transaction}")
            try:
                status, raw = self.transport.request("GET", url, {"Accept": "application/json", **self.auth.headers()})
            except MIError as exc:
                if not exc.transient:
                    raise
                last = exc
            else:
                if status == 401 and not reauthed and self.auth.invalidate():
                    reauthed = True
                    continue
                if status in (401, 403):
                    raise MIError("auth", f"HTTP {status} from {program}/{transaction}", status=status)
                if status == 429 or status >= 500:
                    last = MIError("http", f"HTTP {status}", transient=True, status=status)
                elif status != 200:
                    raise MIError("http", f"HTTP {status}", status=status)
                else:
                    return parse(raw, program, transaction)
            delay = self.retry.delay(attempt)
            self.log(f"MI {program}/{transaction} attempt {attempt + 1} failed ({last}); retry in {delay:.2f}s")
            self.clock.sleep(delay)
            attempt += 1
        assert last is not None
        raise last

