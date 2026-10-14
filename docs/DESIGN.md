# Design and operations

## Reading M3

| Step | Call | Notes |
|---|---|---|
| Session context | `PPS170MI/GetUserInfo` (configurable) | Establishes the API session's company before every scan, independently of business rows. Wrong company: the scan is refused. Probe failing: rows are still delivered but nothing is withdrawn. Transaction and field name are unverified (M3_VALIDATION C23). |
| Enumerate | `EXPORTMI/Select` on MPOPLP (fixed in code) | Restricted subset by choice: prefixed fields, `=` and `>=` joined by `and`. Keyset on POPLPN: a full page is accepted up to its last PLPN, the next page restarts at `POPLPN >= last`. `page_cap` at most 10,000 (the documented ceiling). |
| Targeted read | `EXPORTMI/Select` with the full key, no discovery filter | Same field set as the scan. The returned key is compared with the requested one. `GetPOProp` takes no PLP2. |
| Enrich | `MMS200MI/GetItmBasic` | Item group, cached with a TTL, only when an item-group filter is set. |
| Check | `PPS170MI/GetPOProp` | Compatibility report only. |

**Authoritative scan** = traversal complete, session company confirmed by the probe, and
`extract.index_contract_verified = true`. The order check inside and across pages detects
disorder, but cannot prove that the index EXPORTMI uses orders rows by PLPN first: an index
grouped by another field can return sorted pages and still skip rows
([`test_a12`](../evidence/runs/test_a12_index_not_ordered_by_plpn_never_withdraws.log)). So the
index contract is a prerequisite you set only after verifying it on the installation. Only
authoritative scans count towards `not_observed`; other scans still deliver what they positively
observed.

Endpoint profiles fix location, authentication and response format together: `grid_basic_v1`
(Grid, Basic, MIRecord), `ionapi_v1` (ION API gateway, service-account token, MIRecord),
`ionapi_v2` (gateway, token, `results/records`). A v1 success needs an `MIRecord` list; a
Program-only envelope is not taken as empty. A v2 success needs exactly one processed result for
the expected transaction with no error indicator (`errorMessage/Code/Type/Field`, `notProcessed`,
`nrOfNotProcessedTransactions`, `nrOfFailedTransactions`, `wasTerminated`). HTTPS is mandatory
except towards loopback. One token bucket limits every MI call; retries use exponential backoff
with full jitter, with the exponent saturated so a large attempt count cannot overflow.

## Value policy (not M3 semantics)

`value = PPQT x PUPR / PUCD`, PPQT assumed in PUUN. A PUUN/PPUN conversion must be declared per
item (`"ITNO:BOX->PCS" = 12`), because M3 defines alternate units per item (MMS015); there is no
global packaging factor. Missing unit, unknown conversion, zero price: value unknown, handled by
`unknown_value`. The exact amount is compared with the per-currency threshold, never a rounded one.
Discounts and currency conversion are out of scope.

## Delivery contract

- Registration: `POST /v1/installations/register` returns a generation (monotonic, held by the
  receiver). Inventory: `GET /v1/installations/inventory`. State: `POST /v1/proposal-states`.
- Delivery identifier: `tenant:installation:g<generation>:<key>:<seq>`. Receiver order:
  (generation, seq). Deliveries from an older generation are refused with `409 fenced`.
- Acknowledgement: HTTP 200 with a JSON object `{"result": applied|duplicate|stale,
  "delivery_id": <the same id>}`. Anything else with a 200 (HTML, `{}`, unknown result, other id)
  keeps the delivery pending and is reported as a protocol failure.
- The fingerprint covers the synchronized projection: state, business fields, item group, derived
  value and eligibility reasons. A configuration change that alters the value or reasons re-sends
  the proposal ([`test_a11`](../evidence/runs/test_a11_configuration_change_resends_derived_values.log)).

## Network boundary

- **Default: polling, no listening socket.** Outbound connections only: the M3 endpoint, the one
  receiver base URL, and for ION profiles the token endpoint.
- **Narrow API, not a tunnel.** mTLS, CA pinned, bounded JSON responses read as data, never as
  commands. The MI allowlist is hard-coded to four read transactions; `RelPOP`, `UpdPOP`,
  `DelPlannedPO` and all others are refused before any network call.
- **One table.** EXPORTMI/Select can read any table the service account may read; the agent fixes
  table and prefix to `MPOPLP`/`PO` in code. Restricting the account on the M3 side is still advised.
- **Optional notification endpoint** (`[events] enabled = true`, experimental): binds an explicit
  LAN address (empty, `0.0.0.0`, `::` refused), accepts `POST /notify` only from
  `allowed_sources`, keeps only the key and reads the row itself. Notifications respect the outbox
  capacity; the loop handles at most 100 per pass. It opens an inbound LAN port: customer approval
  and a firewall rule needed.
- A test proves the topology it runs in, not a customer's firewall.

## Secrets

- M3 credentials (or the `.ionapi` content) live in `vault.json`, AES-256-GCM, entry name bound as
  associated data. The 32-byte key is a separate file mounted read-only, mode 400/600; a key
  readable by group or others is refused. This protects copies of the volume and backups, not
  against an administrator of the host.
- Rotation: `m3sync vault put --name m3.password`, then restart. Trust stores are explicit files;
  verification cannot be disabled.
- Logs: the test checks that the M3 password is absent from the output of a successful run. That is
  not a proof for every error path. MI error messages can carry proposal or item numbers: they stay
  in the local log and are never written to the compatibility report.
- Windows service and DPAPI are not provided: they were not tested.

## Operations

- `/data` must be a persistent volume (outbox, checkpoint, lock). It is a cache: at start the agent
  re-registers and resynchronizes from the receiver, so losing, restoring or cloning it is covered
  (tests `a01`, `a07`, `a14`, `a18`, `r01`). A second instance on the same directory is refused; a
  copy on another directory is fenced as soon as the other registers. Restarting a fenced copy
  re-registers it and fences the other one: run one agent per installation.
- When persistence fails the scan is discarded and the checkpoint does not advance.
- Capacity: `delivery.max_pending` bounds pending deliveries. When a scan finds more changes than
  the room left, it queues what fits, defers the rest to later scans and is not authoritative, so an
  initial population larger than the limit still drains over several passes. Finished outbox rows
  and scan records are compacted to the last `delivery.retention_keep` after each authoritative
  scan; the sequence counter is kept. The SQLite file still grows with the number of distinct keys.
- Health: `/data/health.json` after every scan and delivery pass; `degraded` when there is no
  authoritative scan within `health_max_age_s` (default three intervals), the session context cannot
  be established, delivery fails (transport, protocol, configuration, 5xx), the agent is fenced, the
  oldest pending delivery is older than `health_max_age_s`, or a delivery was refused for good since
  the last success. `m3sync health` exits 0, 1 (degraded) or 2 (stale file); the image uses it as
  `HEALTHCHECK`.
- Upgrade: pull a new image, restart on the same volume; the start-up resync re-aligns with the
  receiver. An upgrade across schema changes has not been tested.

## Why this stack

Python 3.12, standard library plus one dependency (`cryptography`, for AES-GCM and test
certificates). HTTP, TLS, SQLite and the CLI come from the standard library, so the audit surface is
this repository and one well-known package. SQLite in WAL mode with `synchronous=FULL` gives the
single-file, transactional outbox the contract needs, without a database server at the customer.
Shipped as one Docker image, non-root, no exposed port. A Go single binary would be a reasonable
alternative for a Windows-service variant; the container is the only tested target here.
