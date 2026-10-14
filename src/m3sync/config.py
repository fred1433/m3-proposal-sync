"""Agent configuration (TOML). Every M3-specific name here is a default to be
confirmed by the read-only compatibility check (see M3_VALIDATION.md)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

# MPOPLP columns read by the agent, without the table prefix.
# Names follow Infor's MMS940/PPS170MI documentation; the prefix "PO" and the
# presence of every column on the target installation are validation items.
DEFAULT_FIELDS = (
    "CONO", "PLPN", "PLPS", "PLP2", "FACI", "WHLO", "ITNO", "SUNO", "BUYE",
    "PSTS", "PPQT", "PUUN", "PUPR", "PPUN", "PUCD", "CUCD", "DLDT",
    "LMDT", "CHNO", "LMTS",
)
KEY_FIELDS = ("CONO", "PLPN", "PLPS", "PLP2")
VERSION_FIELDS = ("LMDT", "CHNO", "LMTS")

_SAFE_LITERAL = re.compile(r"^[A-Za-z0-9_\-]{1,20}$")
_FIELD = re.compile(r"^[A-Z0-9]{4}$")
ENDPOINT_PROFILES = ("grid_basic_v1", "ionapi_v1", "ionapi_v2")
# EXPORTMI/Select can read any table the service account may read. The agent
# reads exactly one: the table and its prefix are fixed, not configurable.
EXPORTMI_TABLE = "MPOPLP"
EXPORTMI_PREFIX = "PO"
EXPORTMI_DOCUMENTED_CEILING = 10_000   # "EXPORTMI can return 10,000 records at most" (M3_VALIDATION C4)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class M3Config:
    profile: str = "grid_basic_v1"
    base_url: str = ""
    company: str = ""
    ca_file: str | None = None
    timeout_s: float = 30.0
    user_secret: str = "m3.user"
    password_secret: str = "m3.password"
    ionapi_secret: str = "m3.ionapi"
    rate_per_s: float = 5.0
    burst: int = 5
    retry_attempts: int = 4
    retry_base_s: float = 1.0
    retry_cap_s: float = 30.0
    # Session context probe: establishes the API session's company independently of
    # business rows. Transaction and field name are UNVERIFIED defaults (M3_VALIDATION C23).
    context_probe: str = "PPS170MI/GetUserInfo"
    context_company_field: str = "CONO"


@dataclass(frozen=True)
class ExtractConfig:
    table: str = "MPOPLP"
    prefix: str = "PO"
    fields: tuple[str, ...] = DEFAULT_FIELDS
    page_cap: int = 1000
    max_pages: int = 1000
    separator: str = "|"
    discovery_equals: tuple[tuple[str, str], ...] = ()
    # Set to true only once the index used by EXPORTMI is verified to order rows by
    # PLPN first (M3_VALIDATION C1). Until then scans never authorize absence.
    index_contract_verified: bool = False

    def validate(self) -> None:
        if self.table != EXPORTMI_TABLE or self.prefix != EXPORTMI_PREFIX:
            raise ConfigError(f"extract.table/prefix are fixed to {EXPORTMI_TABLE}/{EXPORTMI_PREFIX}; "
                              "the agent reads no other table")
        if not 2 <= self.page_cap <= EXPORTMI_DOCUMENTED_CEILING:
            raise ConfigError(f"extract.page_cap must be between 2 and {EXPORTMI_DOCUMENTED_CEILING} "
                              "(documented EXPORTMI ceiling)")
        for f in self.fields:
            if not _FIELD.match(f):
                raise ConfigError(f"bad field name {f!r}")
        for k in KEY_FIELDS:
            if k not in self.fields:
                raise ConfigError(f"key field {k} must be extracted")
        for f, v in self.discovery_equals:
            if not _FIELD.match(f) or not _SAFE_LITERAL.match(v) or f not in self.fields:
                raise ConfigError(f"discovery filter {f}={v!r} rejected (field must be extracted, simple literal)")
        if len(self.separator) != 1 or self.separator.isalnum() or self.separator.isspace():
            raise ConfigError("separator must be one non-alphanumeric, non-space character")
        if self.separator in "#*'":
            raise ConfigError("separators # and * are reserved by EXPORTMI (field-name-prefixed values), "
                              "and ' delimits literals")


@dataclass(frozen=True)
class FilterConfig:
    facilities: frozenset[str] = frozenset()
    warehouses: frozenset[str] = frozenset()
    buyers: frozenset[str] = frozenset()
    statuses: frozenset[str] = frozenset()
    item_groups: frozenset[str] = frozenset()
    min_value: dict[str, Decimal] = field(default_factory=dict)  # per currency
    unknown_value: str = "include"  # include | exclude
    # Per item: (ITNO, order unit, price unit) -> factor. No global packaging factor.
    unit_conversions: dict[tuple[str, str, str], Decimal] = field(default_factory=dict)
    released_statuses: frozenset[str] = frozenset()  # empty: "released" is never emitted
    item_group_ttl_s: float = 3600.0


@dataclass(frozen=True)
class DeliveryConfig:
    url: str = ""
    ca_file: str = ""
    client_cert: str = ""
    client_key: str = ""
    timeout_s: float = 15.0
    retry_base_s: float = 2.0
    retry_cap_s: float = 300.0
    max_pending: int = 100_000
    retention_keep: int = 10_000     # delivered/refused outbox rows and scan records kept


@dataclass(frozen=True)
class EventsConfig:
    enabled: bool = False
    bind_host: str = ""
    bind_port: int = 0
    allowed_sources: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AgentConfig:
    tenant_id: str
    installation_id: str
    data_dir: str
    poll_interval_s: float = 900.0
    absence_confirmations: int = 2
    health_max_age_s: float = 0.0       # 0: 3 x poll_interval_s
    vault_file: str = ""
    vault_key_file: str = ""
    m3: M3Config = field(default_factory=M3Config)
    extract: ExtractConfig = field(default_factory=ExtractConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    events: EventsConfig = field(default_factory=EventsConfig)

    def validate(self) -> None:
        if self.delivery.max_pending < 1 or self.delivery.retention_keep < 1:
            raise ConfigError("delivery.max_pending and delivery.retention_keep must be >= 1")
        for name in ("tenant_id", "installation_id"):
            if not _SAFE_LITERAL.match(getattr(self, name)):
                raise ConfigError(f"{name} must match {_SAFE_LITERAL.pattern}")
        if self.m3.profile not in ENDPOINT_PROFILES:
            raise ConfigError(f"m3.profile must be one of {ENDPOINT_PROFILES}")
        if not self.m3.company:
            raise ConfigError("m3.company (expected CONO) is required")
        if self.filters.unknown_value not in ("include", "exclude"):
            raise ConfigError("filters.unknown_value must be include or exclude")
        if self.absence_confirmations < 1:
            raise ConfigError("absence_confirmations must be >= 1")
        self.extract.validate()
        if self.events.enabled:
            if not self.events.allowed_sources:
                raise ConfigError("events.enabled requires events.allowed_sources")
            if self.events.bind_host in ("", "0.0.0.0", "::", "[::]"):
                raise ConfigError("events.enabled requires an explicit LAN bind_host; 0.0.0.0 and :: are refused")

    @property
    def health_max_age(self) -> float:
        return self.health_max_age_s or 3 * self.poll_interval_s


def _conv_key(k: str) -> tuple[str, str, str]:
    try:
        itno, units = k.split(":", 1)
        a, b = units.split("->")
    except ValueError as exc:
        raise ConfigError(f"unit conversion key {k!r} must be 'ITNO:FROM->TO'") from exc
    return itno, a, b


def _fs(v) -> frozenset[str]:
    return frozenset(str(x) for x in (v or []))


def from_dict(d: dict) -> AgentConfig:
    m3 = d.get("m3", {})
    ex = d.get("extract", {})
    fl = d.get("filters", {})
    dl = d.get("delivery", {})
    ev = d.get("events", {})
    cfg = AgentConfig(
        tenant_id=d["tenant_id"],
        installation_id=d["installation_id"],
        data_dir=d["data_dir"],
        poll_interval_s=float(d.get("poll_interval_s", 900)),
        absence_confirmations=int(d.get("absence_confirmations", 2)),
        health_max_age_s=float(d.get("health_max_age_s", 0)),
        vault_file=d.get("vault_file", ""),
        vault_key_file=d.get("vault_key_file", ""),
        m3=M3Config(**{k: (tuple(v) if isinstance(v, list) else v) for k, v in m3.items()}),
        extract=ExtractConfig(
            table=ex.get("table", "MPOPLP"),
            prefix=ex.get("prefix", "PO"),
            fields=tuple(ex.get("fields", DEFAULT_FIELDS)),
            page_cap=int(ex.get("page_cap", 1000)),
            max_pages=int(ex.get("max_pages", 1000)),
            separator=ex.get("separator", "|"),
            discovery_equals=tuple(sorted((k, str(v)) for k, v in ex.get("discovery_equals", {}).items())),
            index_contract_verified=bool(ex.get("index_contract_verified", False)),
        ),
        filters=FilterConfig(
            facilities=_fs(fl.get("facilities")),
            warehouses=_fs(fl.get("warehouses")),
            buyers=_fs(fl.get("buyers")),
            statuses=_fs(fl.get("statuses")),
            item_groups=_fs(fl.get("item_groups")),
            min_value={k: Decimal(str(v)) for k, v in fl.get("min_value", {}).items()},
            unknown_value=fl.get("unknown_value", "include"),
            unit_conversions={_conv_key(k): Decimal(str(v)) for k, v in fl.get("unit_conversions", {}).items()},
            released_statuses=_fs(fl.get("released_statuses")),
            item_group_ttl_s=float(fl.get("item_group_ttl_s", 3600)),
        ),
        delivery=DeliveryConfig(**dl),
        events=EventsConfig(
            enabled=bool(ev.get("enabled", False)),
            bind_host=ev.get("bind_host", ""),
            bind_port=int(ev.get("bind_port", 0)),
            allowed_sources=_fs(ev.get("allowed_sources")),
        ),
    )
    cfg.validate()
    return cfg


def load(path: str | Path) -> AgentConfig:
    with open(path, "rb") as fh:
        return from_dict(tomllib.load(fh))
