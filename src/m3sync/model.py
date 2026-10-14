"""Source keys, value policy, eligibility and the synchronized projection.

Three separate questions:
  discovery    which rows the EXPORTMI scan enumerates (session company + discovery_equals)
  enrichment   data not in MPOPLP (item group from MMS200MI GetItmBasic)
  eligibility  whether an enumerated row is in scope for the SaaS

States sent to the receiver:
  active        row observed and eligible
  out_of_scope  row still exists but is no longer eligible (or left the discovery filter)
  not_observed  absent from N consecutive AUTHORITATIVE scans and a targeted read
                returns zero rows; says nothing about why it disappeared
  released      only for a key the receiver already holds, and only when the observed
                status is in filters.released_statuses (empty by default)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .config import KEY_FIELDS, FilterConfig

STATES = ("active", "out_of_scope", "not_observed", "released")


def source_key(row: dict[str, str]) -> str:
    return "/".join(str(int(row[k])) if k != "CONO" else row[k] for k in KEY_FIELDS)


@dataclass(frozen=True)
class Value:
    amount: Decimal | None   # exact, never rounded before comparison
    currency: str
    status: str  # ok | unknown_price | invalid_price_quantity | missing_unit | unknown_unit_conversion | bad_number


def compute_value(row: dict[str, str], f: FilterConfig) -> Value:
    """value = quantity (converted to the price unit) x price / price quantity.

    Policy, not validated M3 semantics:
      * PPQT is assumed to be expressed in the purchase-order unit PUUN;
      * a unit conversion must be declared for the specific item (ITNO); there is no
        global packaging factor, because conversions are item-specific in M3;
      * a missing unit, a zero/negative price or price quantity gives an unknown value;
      * the exact amount is compared with the threshold, without rounding;
      * discounts and currency conversion are out of scope; thresholds are per currency."""
    cur = row.get("CUCD", "")
    try:
        qty = Decimal(row["PPQT"])
        price = Decimal(row["PUPR"])
        pucd = Decimal(row.get("PUCD") or "1")
    except (InvalidOperation, KeyError):
        return Value(None, cur, "bad_number")
    if price <= 0:
        return Value(None, cur, "unknown_price")
    if pucd <= 0:
        return Value(None, cur, "invalid_price_quantity")
    puun, ppun = row.get("PUUN", ""), row.get("PPUN", "")
    if not puun or not ppun:
        return Value(None, cur, "missing_unit")
    if ppun != puun:
        factor = f.unit_conversions.get((row.get("ITNO", ""), puun, ppun))
        if factor is None:
            return Value(None, cur, "unknown_unit_conversion")
        qty = qty * factor
    return Value(qty * price / pucd, cur, "ok")


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: tuple[str, ...]
    value: Value


def evaluate(row: dict[str, str], item_group: str | None, f: FilterConfig) -> Eligibility:
    reasons = []
    if f.facilities and row.get("FACI") not in f.facilities:
        reasons.append(f"facility {row.get('FACI')} not in filter")
    if f.warehouses and row.get("WHLO") not in f.warehouses:
        reasons.append(f"warehouse {row.get('WHLO')} not in filter")
    if f.buyers and row.get("BUYE") not in f.buyers:
        reasons.append(f"buyer {row.get('BUYE')} not in filter")
    if f.statuses and row.get("PSTS") not in f.statuses:
        reasons.append(f"status {row.get('PSTS')} not in filter")
    if f.item_groups and item_group not in f.item_groups:
        reasons.append(f"item group {item_group} not in filter")
    val = compute_value(row, f)
    if f.min_value:
        threshold = f.min_value.get(val.currency)
        if val.status != "ok" or threshold is None:
            if f.unknown_value == "exclude":
                reasons.append(f"value unknown ({val.status if val.status != 'ok' else 'no threshold for ' + val.currency})")
        elif val.amount < threshold:
            reasons.append(f"value {val.amount.normalize()} {val.currency} below {threshold}")
    return Eligibility(not reasons, tuple(reasons), val)


# Payload keys that describe the observation event rather than the synchronized state.
INCIDENTAL = ("observed_at", "origin", "source_version", "fingerprint")


def fingerprint(projection: dict) -> str:
    """Fingerprint of the semantic projection actually synchronized: state, business
    fields, item group, derived value and eligibility reasons. A configuration change
    that changes the derived value or reasons therefore changes the fingerprint and
    re-sends the proposal. Observation time, origin and version fields are excluded."""
    body = {k: v for k, v in projection.items() if k not in INCIDENTAL}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
