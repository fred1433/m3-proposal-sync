"""M3 source SIMULATOR. Not M3.

Every behaviour below is either taken from a cited public source (see
M3_VALIDATION.md, column "simulator") or marked SYNTHETIC: chosen for the
harness, unknown on a real installation, and never relied upon by the agent
as a guarantee.

Serves, on 127.0.0.1 only:
  GET  /m3api-rest/execute/{PGM}/{TRN}[;maxrecs=N]?...      grid_basic_v1 (Basic auth)
  GET  /M3/m3api-rest/execute/{PGM}/{TRN}[;maxrecs=N]?...   ionapi_v1 (bearer)
  GET  /M3/m3api-rest/v2/execute/{PGM}/{TRN}?...&maxrecs=N  ionapi_v2 (bearer)
  POST /token                                               password grant (bearer)
Transactions: EXPORTMI/Select (MPOPLP only), PPS170MI/GetPOProp, PPS170MI/GetUserInfo,
MMS200MI/GetItmBasic.

EXPORTMI here accepts only the restricted subset the agent uses ("=" and ">="
comparisons joined by "and"). The cited 16.x documentation also describes "or"
and wildcard field selection: the simulator rejects them because the agent never
sends them, not because M3 lacks them.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "PO"
MPOPLP_FIELDS = (
    "CONO", "PLPN", "PLPS", "PLP2", "FACI", "WHLO", "ITNO", "SUNO", "BUYE",
    "PSTS", "PPQT", "PUUN", "PUPR", "PPUN", "PUCD", "CUCD", "DLDT",
    "LMDT", "CHNO", "LMTS",
)
NUMERIC = {"CONO", "PLPN", "PLPS", "PLP2", "PSTS", "PPQT", "PUPR", "PUCD", "DLDT", "LMDT", "CHNO", "LMTS"}
EXPORTMI_HARD_LIMIT = 10_000          # cited: EXPORTMI returns 10,000 records at most
V1_DEFAULT_MAXRECS = 100              # SYNTHETIC: v1 default unknown; the agent always sends maxrecs
_FORBIDDEN = re.compile(r"\border\s+by\b|\bin\s*\(|\bjoin\b|\bor\b|[()*]", re.I)
_SELECT = re.compile(r"^\s*(?P<cols>[A-Za-z0-9]+(?:\s*,\s*[A-Za-z0-9]+)*)\s+from\s+(?P<table>[A-Za-z0-9]+)"
                     r"(?:\s+where\s+(?P<where>.+?))?\s*$", re.I)
_COND = re.compile(r"^([A-Za-z0-9]+)\s*(=|<>|>=|<=|>|<)\s*('([^']*)'|-?\d+(?:\.\d+)?)$")


class Nok(Exception):
    pass


class M3Sim:
    def __init__(self, company: str = "100", user: str = "svc-sync", password: str = "sim-password",
                 ion_client: tuple[str, str] = ("sim-ci", "sim-cs")) -> None:
        self.company = company            # company of the API session (EXPORTMI uses it)
        self.user, self.password = user, password
        self.ion_client = ion_client
        self.proposals: dict[tuple[int, int, int, int], dict[str, str]] = {}
        self.items: dict[str, dict[str, str]] = {}
        self.calls: list[dict] = []
        self.fault = None          # callable(call: dict) -> None | "http500" | "timeout" | "nok" | "truncate" | "auth401"
        self.before_call = None    # callable(call: dict) -> None, may mutate data mid-scan
        self.latency_s = 0.0
        self.timeout_sleep_s = 1.0
        self.index_key = None      # callable(row) -> sort key; None = (CONO, PLPN, PLPS, PLP2) (SYNTHETIC 00-index)
        self.raw_response = None   # callable(call) -> (status, doc) | None: inject any response body
        self.tokens: set[str] = set()
        self.lock = threading.RLock()
        self.httpd: ThreadingHTTPServer | None = None
        self._lmts = 1_790_000_000_000

    # --- data -------------------------------------------------------------
    def put_proposal(self, plpn: int, plps: int = 1, plp2: int = 0, cono: str | None = None, **fields) -> dict:
        with self.lock:
            self._lmts += 1000
            cono = cono or self.company
            row = {f: "" for f in MPOPLP_FIELDS}
            row.update({"CONO": cono, "PLPN": str(plpn), "PLPS": str(plps), "PLP2": str(plp2),
                        "FACI": "A01", "WHLO": "100", "ITNO": "ITEM-1", "SUNO": "SUP001", "BUYE": "BUYER1",
                        "PSTS": "20", "PPQT": "10", "PUUN": "PCS", "PUPR": "12.5", "PPUN": "PCS", "PUCD": "1",
                        "CUCD": "EUR", "DLDT": "20261020", "LMDT": "20261014", "CHNO": "1",
                        "LMTS": str(self._lmts)})
            key = (int(cono), plpn, plps, plp2)
            old = self.proposals.get(key)
            if old:
                row.update({k: v for k, v in old.items() if k not in fields})
                row["CHNO"] = str(int(old["CHNO"]) + 1)
                row["LMTS"] = str(self._lmts)
            row.update({k: str(v) for k, v in fields.items()})
            self.proposals[key] = row
            return row

    def delete_proposal(self, plpn: int, plps: int = 1, plp2: int = 0, cono: str | None = None) -> None:
        with self.lock:
            self.proposals.pop((int(cono or self.company), plpn, plps, plp2), None)

    def put_item(self, itno: str, itgr: str) -> None:
        with self.lock:
            self.items[itno] = {"ITNO": itno, "ITDS": f"Item {itno}", "ITGR": itgr, "STAT": "20"}

    # --- transactions -----------------------------------------------------
    def _exportmi(self, params: dict, maxrecs: int) -> list[dict[str, str]]:
        q = params.get("QERY", "")
        sep = params.get("SEPC", ";")
        if _FORBIDDEN.search(q):
            raise Nok("EXPORTMI: unsupported syntax (IN(), ORDER BY, OR, joins, parentheses, *)")
        m = _SELECT.match(q)
        if not m:
            raise Nok("EXPORTMI: query not understood")
        table = m.group("table")
        if table != "MPOPLP":
            raise Nok(f"EXPORTMI: table {table} not available in simulator")
        cols = [c.strip() for c in m.group("cols").split(",")]
        for c in cols:
            if c != c.upper() or not c.startswith(PREFIX) or c[len(PREFIX):] not in MPOPLP_FIELDS:
                raise Nok(f"EXPORTMI: field {c} does not exist (fields need capitals and the table prefix)")
        conds = []
        if m.group("where"):
            for part in re.split(r"\s+and\s+", m.group("where").strip(), flags=re.I):
                cm = _COND.match(part.strip())
                if not cm or cm.group(1) != cm.group(1).upper() or not cm.group(1).startswith(PREFIX) \
                        or cm.group(1)[len(PREFIX):] not in MPOPLP_FIELDS:
                    raise Nok(f"EXPORTMI: condition {part!r} not understood")
                lit = cm.group(4) if cm.group(4) is not None else cm.group(3)
                conds.append((cm.group(1)[len(PREFIX):], cm.group(2), lit))
        with self.lock:
            rows = [r for k, r in sorted(self.proposals.items()) if r["CONO"] == self.company]  # 00-index order (SYNTHETIC key)
        if self.index_key:
            rows.sort(key=self.index_key)
        def ok(r):
            for f, op, lit in conds:
                a, b = r[f], lit
                if f in NUMERIC and re.fullmatch(r"-?\d+(\.\d+)?", b):
                    a, b = float(a or 0), float(b)
                if not {"=": a == b, "<>": a != b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]:
                    return False
            return True
        rows = [r for r in rows if ok(r)]
        limit = EXPORTMI_HARD_LIMIT if maxrecs == 0 else min(maxrecs, EXPORTMI_HARD_LIMIT)
        rows = rows[:limit]
        out = []
        if params.get("HDRS") == "1":
            out.append({"REPL": sep.join(cols)})
        for r in rows:
            if sep in "#*":   # documented: these separators give field-name-prefixed values
                out.append({"REPL": sep.join(f"{c} {r[c[len(PREFIX):]]}" for c in cols)})
            else:
                out.append({"REPL": sep.join(r[c[len(PREFIX):]] for c in cols)})
        return out

    def _getpoprop(self, params: dict) -> list[dict[str, str]]:
        # SYNTHETIC: returns the PLP2 = 0 line; real output fields and PLP2 handling unknown.
        if "PLPN" not in params:
            raise Nok("PPS170MI/GetPOProp: PLPN is mandatory")
        key = (int(self.company), int(params["PLPN"]), int(params.get("PLPS") or 0), 0)
        with self.lock:
            row = self.proposals.get(key)
        if row is None:
            raise Nok(f"Order proposal {params['PLPN']} does not exist")  # SYNTHETIC wording
        return [dict(row)]

    def _getitmbasic(self, params: dict) -> list[dict[str, str]]:
        with self.lock:
            item = self.items.get(params.get("ITNO", ""))
        if item is None:
            raise Nok(f"Item number {params.get('ITNO')} does not exist")  # SYNTHETIC wording
        return [dict(item)]

    # --- HTTP -------------------------------------------------------------
    def start(self) -> "M3Sim":
        sim = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                sim._handle(self)

            def do_POST(self):
                sim._token(self)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def ionapi_json(self) -> str:
        return json.dumps({"ti": "SIM", "ci": self.ion_client[0], "cs": self.ion_client[1],
                           "saak": self.user, "sask": self.password, "pu": self.base_url, "ot": "/token"})

    @staticmethod
    def _send(h, status: int, doc) -> None:
        body = json.dumps(doc).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _token(self, h) -> None:
        if h.path != "/token":
            return self._send(h, 404, {})
        form = dict(urllib.parse.parse_qsl(h.rfile.read(int(h.headers.get("Content-Length", "0"))).decode()))
        if (form.get("grant_type"), form.get("client_id"), form.get("client_secret"), form.get("username"),
                form.get("password")) != ("password", *self.ion_client, self.user, self.password):
            return self._send(h, 401, {"error": "invalid_grant"})
        tok = secrets.token_hex(16)
        self.tokens.add(tok)
        self._send(h, 200, {"access_token": tok, "token_type": "Bearer", "expires_in": 7200})

    def _handle(self, h) -> None:
        u = urllib.parse.urlsplit(h.path)
        m = re.match(r"^(?P<root>/m3api-rest/execute|/M3/m3api-rest/execute|/M3/m3api-rest/v2/execute)/"
                     r"(?P<pgm>[A-Z0-9]+)/(?P<trn>[A-Za-z0-9]+)(?P<matrix>(;[a-z]+=[^;/?]*)*)$", u.path)
        if not m:
            return self._send(h, 404, {})
        v2 = m.group("root").endswith("/v2/execute")
        params = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
        matrix = dict(x.split("=", 1) for x in m.group("matrix").split(";") if "=" in x)
        auth = h.headers.get("Authorization", "")
        if m.group("root") == "/m3api-rest/execute":
            expected = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            authed = auth == expected
        else:
            authed = auth.startswith("Bearer ") and auth[7:] in self.tokens
        call = {"t": time.monotonic(), "program": m.group("pgm"), "transaction": m.group("trn"),
                "params": params, "v2": v2, "n": len(self.calls) + 1}
        with self.lock:
            self.calls.append(call)
        if self.before_call:
            self.before_call(call)
        if self.latency_s:
            time.sleep(self.latency_s)
        fault = self.fault(call) if self.fault else None
        call["fault"] = fault
        injected = self.raw_response(call) if self.raw_response else None
        if injected is not None and authed:
            return self._send(h, injected[0], injected[1])
        if not authed or fault == "auth401":
            return self._send(h, 401, {"error": "unauthorized"})
        if fault == "http500":
            return self._send(h, 500, {"error": "simulated server error"})
        if fault == "timeout":
            time.sleep(self.timeout_sleep_s)
            return self._send(h, 504, {"error": "simulated slow response"})
        maxrecs_raw = params.pop("maxrecs", None) if v2 else matrix.get("maxrecs")
        maxrecs = int(maxrecs_raw) if maxrecs_raw is not None else (0 if v2 else V1_DEFAULT_MAXRECS)
        try:
            if fault == "nok":
                raise Nok("simulated MI error (NOK)")
            key = (m.group("pgm"), m.group("trn"))
            if key == ("EXPORTMI", "Select"):
                rows = self._exportmi(params, maxrecs)
                if fault == "truncate" and rows:
                    sep = params.get("SEPC", ";")
                    rows[-1] = {"REPL": sep.join(rows[-1]["REPL"].split(sep)[:-3])}
            elif key == ("PPS170MI", "GetUserInfo"):
                rows = [{"USID": self.user, "CONO": self.company, "DIVI": ""}]   # SYNTHETIC field names
            elif key == ("PPS170MI", "GetPOProp"):
                rows = self._getpoprop(params)
            elif key == ("MMS200MI", "GetItmBasic"):
                rows = self._getitmbasic(params)
            else:
                raise Nok(f"{key[0]}/{key[1]} not available in simulator")
        except Nok as exc:
            call["result"] = "nok"
            if v2:
                return self._send(h, 200, {"results": [{"transaction": m.group("trn"), "errorMessage": str(exc),
                                                        "errorType": "ServerReturnedNOK", "notProcessed": False,
                                                        "records": []}],
                                           "wasTerminated": False, "nrOfSuccessfullTransactions": 0,
                                           "nrOfFailedTransactions": 1})
            return self._send(h, 200, {"@type": "ServerReturnedNOK", "Message": str(exc)})
        call["result"] = f"{len(rows)} rows"
        if v2:
            return self._send(h, 200, {"results": [{"transaction": m.group("trn"), "records": rows}],
                                       "wasTerminated": False, "nrOfSuccessfullTransactions": 1,
                                       "nrOfFailedTransactions": 0})
        self._send(h, 200, {"Program": m.group("pgm"), "Transaction": m.group("trn"),
                            "MIRecord": [{"RowIndex": str(i), "NameValue": [{"Name": k, "Value": v}
                                                                             for k, v in r.items()]}
                                         for i, r in enumerate(rows)]})
