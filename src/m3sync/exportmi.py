"""MPOPLP enumeration through EXPORTMI Select.

Query shape (one table, prefixed fields, AND-ed simple comparisons, no IN(), no
ORDER BY, no OR):

    POCONO|...|POLMTS  ->  "POCONO,POPLPN,... from MPOPLP where POFACI = 'A01' and POPLPN >= 1234"

Traversal strategy (keyset on the leading key field only):
  * rows are expected in the order of the table's primary index
    (CONO, PLPN, PLPS, PLP2); the agent CHECKS that order and stops with an
    incomplete result if it is violated instead of trusting it;
  * a page is requested with maxrecs = page_cap; a short page ends the scan;
  * a full page is accepted up to, but excluding, its last PLPN; the next page
    restarts at "PLPN >= that value", so a proposal whose PLPS/PLP2 lines
    straddle the page boundary is re-read whole on the next page;
  * a full page made of a single PLPN cannot make progress: incomplete;
  * any error, wrong field count, wrong company or ordering anomaly: incomplete.
Only ">=" and "=" are used: a restricted subset chosen on purpose (the 16.x
documentation also describes "or" and wildcard selection; the agent does not
need them). The result is not a transactionally consistent snapshot.

What the order check can and cannot prove: it detects rows out of order inside
a page and across page boundaries. It cannot prove that the index EXPORTMI uses
orders rows by PLPN first; an index grouped by another field can yield sorted
pages and still skip rows. That index contract is a prerequisite to verify on the
installation (extract.index_contract_verified); until it is set, scans never
authorize absence transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import EXPORTMI_DOCUMENTED_CEILING, ExtractConfig
from .miclient import MIClient, MIError


@dataclass
class ScanResult:
    rows: list[dict[str, str]] = field(default_factory=list)
    complete: bool = False
    reason: str | None = None
    pages: int = 0


def build_query(ex: ExtractConfig, extra_where: list[str], discovery: bool = True) -> str:
    cols = ",".join(ex.prefix + f for f in ex.fields)
    conds = ([f"{ex.prefix}{f} = '{v}'" for f, v in ex.discovery_equals] if discovery else []) + extra_where
    q = f"{cols} from {ex.table}"
    if conds:
        q += " where " + " and ".join(conds)
    return q


def _key(row: dict[str, str]) -> tuple[int, int, int]:
    return int(row["PLPN"]), int(row["PLPS"]), int(row["PLP2"])


def traverse(client: MIClient, ex: ExtractConfig, company: str, log=lambda s: None) -> ScanResult:
    res = ScanResult()
    lower: int | None = None
    seen: set[tuple[int, int, int]] = set()
    while True:
        if res.pages >= ex.max_pages:
            res.reason = f"max_pages ({ex.max_pages}) reached"
            return res
        where = [f"{ex.prefix}PLPN >= {lower}"] if lower is not None else []
        query = build_query(ex, where)
        try:
            raw_rows = client.call("EXPORTMI", "Select",
                                   {"SEPC": ex.separator, "HDRS": "0", "QERY": query},
                                   maxrecs=ex.page_cap)
        except MIError as exc:
            res.reason = f"page {res.pages + 1} failed: {exc}"
            return res
        res.pages += 1
        page: list[dict[str, str]] = []
        for rr in raw_rows:
            if "REPL" not in rr:
                res.reason = f"page {res.pages}: record without REPL field"
                return res
            parts = rr["REPL"].split(ex.separator)
            if len(parts) == len(ex.fields) + 1 and parts[-1] == "":
                parts = parts[:-1]
            if len(parts) != len(ex.fields):
                res.reason = (f"page {res.pages}: {len(parts)} values for {len(ex.fields)} fields "
                              f"(truncated record, or the separator {ex.separator!r} occurs inside the data: "
                              "choose another extract.separator)")
                return res
            row = {f: p.strip() for f, p in zip(ex.fields, parts)}
            if row["CONO"] != company:
                res.reason = f"page {res.pages}: row with CONO {row['CONO']!r}, expected {company!r} (wrong company context)"
                return res
            try:
                _key(row)
            except ValueError:
                res.reason = f"page {res.pages}: non-numeric key {row['PLPN']}/{row['PLPS']}/{row['PLP2']}"
                return res
            page.append(row)
        keys = [_key(r) for r in page]
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            res.reason = f"page {res.pages}: rows not in strictly ascending key order; index order assumption violated"
            return res
        if lower is not None and keys and keys[0][0] < lower:
            res.reason = f"page {res.pages}: row below the requested lower bound"
            return res
        if len(page) > ex.page_cap:
            res.reason = f"page {res.pages}: more rows than maxrecs"
            return res
        if len(page) < min(ex.page_cap, EXPORTMI_DOCUMENTED_CEILING):
            for r, k in zip(page, keys):
                if k not in seen:
                    seen.add(k)
                    res.rows.append(r)
            res.complete = True
            log(f"EXPORTMI scan complete: {len(res.rows)} rows in {res.pages} page(s)")
            return res
        last_plpn = keys[-1][0]
        if keys[0][0] == last_plpn:
            res.reason = (f"page {res.pages}: {ex.page_cap} rows all with PLPN {last_plpn}; "
                          "page_cap too small to traverse by PLPN")
            return res
        for r, k in zip(page, keys):
            if k[0] < last_plpn and k not in seen:
                seen.add(k)
                res.rows.append(r)
        lower = last_plpn


def read_key(client: MIClient, ex: ExtractConfig, company: str, key: dict[str, str]) -> dict[str, str] | None:
    """(The discovery filter is NOT applied: a row that left the discovery scope still
    exists and must be reported out_of_scope, not not_observed.)"""
    """Targeted read of one proposal by its full key, through the same EXPORTMI
    query shape as the scan, so that a notification read and a scan read yield
    the same field set. Returns None when the read succeeds with zero rows.
    Raises MIError on any failure: an error is never taken as "not found".

    (PPS170MI GetPOProp is not used here: its public definition takes PLPN,
    CONO and PLPS, not PLP2, so it cannot address a PLP2 line on its own.)"""
    for f in ("PLPN", "PLPS", "PLP2"):
        if not str(key[f]).isdigit():
            raise MIError("protocol", f"non-numeric key field {f}")
    where = [f"{ex.prefix}PLPN = {int(key['PLPN'])}", f"{ex.prefix}PLPS = {int(key['PLPS'])}",
             f"{ex.prefix}PLP2 = {int(key['PLP2'])}"]
    raw = client.call("EXPORTMI", "Select", {"SEPC": ex.separator, "HDRS": "0",
                                             "QERY": build_query(ex, where, discovery=False)},
                      maxrecs=2)
    if not raw:
        return None
    if len(raw) > 1:
        raise MIError("protocol", "exact key read returned more than one row")
    parts = raw[0].get("REPL", "").split(ex.separator)
    if len(parts) == len(ex.fields) + 1 and parts[-1] == "":
        parts = parts[:-1]
    if len(parts) != len(ex.fields):
        raise MIError("protocol", "exact key read: wrong field count")
    row = {f: p.strip() for f, p in zip(ex.fields, parts)}
    if row["CONO"] != company:
        raise MIError("protocol", f"exact key read returned CONO {row['CONO']!r}, expected {company!r}")
    try:
        got = tuple(int(row[f]) for f in ("PLPN", "PLPS", "PLP2"))
    except ValueError as exc:
        raise MIError("protocol", "exact key read: non-numeric key in response") from exc
    if got != tuple(int(key[f]) for f in ("PLPN", "PLPS", "PLP2")):
        raise MIError("protocol", "exact key read returned a different key than requested")
    return row
