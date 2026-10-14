"""Read-only compatibility check against a real installation.

Run by the customer's administrator with the service account the agent will
use. It only calls the read-only allowlist, writes a local JSON report and sends
nothing anywhere.

Redaction: keys and business values are replaced by salted hashes; MI errors are
reported as structured categories (kind, code, field) and never as their raw
message, which can embed proposal or item numbers.

Each check is "pass", "fail" or "not_exercised" (no data to exercise it, e.g. an
empty sample). The report lists the transactions actually invoked. Exit code:
1 if any check failed, 3 if none failed but some were not exercised, else 0.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter

from .config import AgentConfig
from .exportmi import build_query
from .miclient import MIClient, MIError


def _err(exc: MIError) -> dict:
    return {"error_kind": exc.kind, "error_code": exc.code, "error_field": exc.field, "http_status": exc.status}


def run_check(cfg: AgentConfig, client: MIClient, sample: int = 200) -> dict:
    salt = os.urandom(8).hex()
    h = lambda v: hashlib.sha256((salt + str(v)).encode()).hexdigest()[:10]  # noqa: E731
    ex = cfg.extract
    checks: list[dict] = []
    client.invoked.clear()

    def add(name, result, detail):
        checks.append({"name": name, "result": result, "detail": detail})

    pgm, trn = cfg.m3.context_probe.split("/")
    try:
        ctx = client.call(pgm, trn, {})
        fields = sorted(ctx[0]) if ctx else []
        val = ctx[0].get(cfg.m3.context_company_field) if ctx else None
        add("session_context", "pass" if val == cfg.m3.company else "fail",
            {"probe": cfg.m3.context_probe, "fields": fields,
             "company_matches": val == cfg.m3.company if val is not None else None})
    except MIError as exc:
        add("session_context", "fail", {"probe": cfg.m3.context_probe, **_err(exc)})

    rows: list[dict] = []
    try:
        raw = client.call("EXPORTMI", "Select", {"SEPC": ex.separator, "HDRS": "0", "QERY": build_query(ex, [])},
                          maxrecs=sample)
        bad = False
        for r in raw:
            parts = r.get("REPL", "").split(ex.separator)
            if len(parts) == len(ex.fields) + 1 and parts[-1] == "":
                parts = parts[:-1]
            if len(parts) != len(ex.fields):
                add("exportmi_field_count", "fail", f"{len(parts)} values for {len(ex.fields)} fields")
                bad = True
                break
            rows.append(dict(zip(ex.fields, (p.strip() for p in parts))))
        if not bad:
            add("exportmi_all_fields", "pass", {"sample_rows": len(rows), "fields": [ex.prefix + f for f in ex.fields]})
    except MIError as exc:
        add("exportmi_all_fields", "fail", _err(exc))
        missing = []
        for f in ex.fields:
            try:
                client.call("EXPORTMI", "Select", {"SEPC": ex.separator, "HDRS": "0",
                                                   "QERY": f"{ex.prefix}{f} from {ex.table}"}, maxrecs=1)
            except MIError as e2:
                missing.append({"field": ex.prefix + f, **_err(e2)})
        add("exportmi_field_probe", "fail" if missing else "pass", missing or "each field accepted alone")

    dependent = ("default_index_order", "company_in_rows", "pps170mi_getpoprop", "mms200mi_itgr")
    if not rows:
        for name in dependent:
            add(name, "not_exercised", "no proposal in the sample")
    else:
        keys = [(int(r["PLPN"]), int(r["PLPS"]), int(r["PLP2"])) for r in rows]
        add("default_index_order", "pass" if keys == sorted(keys) else "fail",
            "sample rows ascending by PLPN/PLPS/PLP2 (necessary, not sufficient: verify the index definition)"
            if keys == sorted(keys) else "rows NOT in key order: traversal unsafe")
        conos = sorted({r["CONO"] for r in rows})
        add("company_in_rows", "pass" if conos == [cfg.m3.company] else "fail", {"matches": conos == [cfg.m3.company]})
        add("status_codes_seen", "pass", dict(Counter(r.get("PSTS", "") for r in rows)))
        add("unit_pairs_seen", "pass", dict(Counter(f"{r.get('PUUN')}->{r.get('PPUN')}" for r in rows)))
        add("currencies_seen", "pass", dict(Counter(r.get("CUCD", "") for r in rows)))
        add("version_fields", "pass", {f: sum(1 for r in rows if r.get(f)) for f in ("LMDT", "CHNO", "LMTS") if f in ex.fields})
        add("sample_keys", "pass", [h("/".join(map(str, k))) for k in keys[:5]])
        first = rows[0]
        try:
            gp = client.call("PPS170MI", "GetPOProp", {"PLPN": first["PLPN"], "PLPS": first["PLPS"]})
            add("pps170mi_getpoprop", "pass", {"records": len(gp), "fields": sorted(gp[0]) if gp else []})
        except MIError as exc:
            add("pps170mi_getpoprop", "fail", _err(exc))
        try:
            it = client.call("MMS200MI", "GetItmBasic", {"ITNO": first["ITNO"]})
            add("mms200mi_itgr", "pass" if it and "ITGR" in it[0] else "fail",
                {"fields": sorted(it[0]) if it else [], "itgr_hash": h(it[0].get("ITGR")) if it else None})
        except MIError as exc:
            add("mms200mi_itgr", "fail", _err(exc))
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "profile": cfg.m3.profile,
            "redaction": "keys and business values hashed; MI errors as categories only; codes kept",
            "transactions_invoked": sorted(client.invoked), "checks": checks}


def write_report(report: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
