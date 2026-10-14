# M3 validation document

**Status: designed for validation against an on-premises M3 installation. Not validated.**
No line of this repository has run against an M3 installation. Every M3 behaviour the
simulator reproduces is listed below with its public source, the product and version that
source documents, how the simulator implements it, and what must still be checked on the
target installation. Sources were opened and read on 2026-10-14.

Source types used, from strongest to weakest:

- **Infor doc**: docs.infor.com, product and version taken from the URL and breadcrumb. Many
  cited pages are "M3 UDI 16.x" (M3 Application Foundation), i.e. a later generation than a
  13.4 installation. A 16.x page is a lead for a 13.4 target, not proof.
- **Infor API catalog**: developer.infor.com. The pages state no product version.
- **Infor forum**: community.infor.com, user posts.
- **Third party**: blogs and vendor documentation, not Infor.

## 1. Installation capability matrix (fill in before any build decision)

"13.4" alone is not a compatibility specification. Record each component separately.

| Component | Value on target | Why it matters | How to obtain |
|---|---|---|---|
| M3 Core Technology | _unknown_ | Grid, router authentication, API Web Services, Event Hub | Grid management pages / administrator |
| M3 Business Engine | _unknown_ | MI program catalog, MPOPLP fields, EXPORTMI behaviour | Administrator; `PPS170MI/GetMIBuild` and `GetFixLevel` appear in the API catalog (not called by the agent) |
| m3api-rest endpoint and router auth methods | _unknown_ | Which profile (Basic, certificate) actually works on the chosen router | Router configuration (see C9) |
| Installed MI transactions | _unknown_ | EXPORTMI, PPS170MI, MMS200MI availability and fields | API Repository / REST metadata (C20), then `m3sync check` |
| Event route available | _unknown_ | Optional notification path: EC/MEC subscriber, ION, or CMS045/CMS049 | Administrator (C15 to C18) |
| ION / ION API gateway | _unknown_ | `ionapi_v1` / `ionapi_v2` profiles, Requisition BOD (C19) | Administrator |
| MPOPLP 00-index key fields | _unknown_ | Traversal order assumed by the agent | `m3sync check` (order probe) + metadata |
| Proposal status codes and their meaning | _unknown_ | Status filter, `released_statuses` | Functional owner; `m3sync check` lists codes seen |

Note on versions: the Core Technology 13.4.x release notes page cited as C22 states that the
M3 Metadata Publisher carries "M3BE 15.1.4 metadata". It does not say that Core 13.4 requires
or targets BE 15.1.x. Core and BE versions must be read on the installation.

## 2. Claims, sources, simulator, remaining checks

| # | Claim the agent relies on | Source (type, product/version) | Simulator | Remaining installation check |
|---|---|---|---|---|
| C1 | EXPORTMI reads one table only; returns each record as "a single long field"; syntax is "not actual SQL"; `IN()` and `order by` unsupported; "Operators 'from', 'where', 'and', and 'or' must be written in lower case" (example: "where MMSTAT = 10 or MMSTAT = 90 and MMITCL = 'Y001'"); "as an exception, you can use the general '*' to select all fields"; "Comparison values are specified with or without surrounding quotation marks"; "You can define a specific index for the table, this will control the sorting... If no index is specified, the 00-index will be used"; fields in capitals with the table prefix; company always the API session's; division filters only for sessions on a blank division. | Infor doc, M3 UDI 16.x: [uof1644857333894](https://docs.infor.com/m3udi/16.x/en-us/m3beud/appfoundhs/uof1644857333894.html) | **Restricted subset by choice**: the agent sends only prefixed fields, `=` and `>=`, joined by `and`. The cited 16.x documentation permits more (`or`, `*`); the simulator rejects those because the agent never sends them, not because M3 lacks them. Rows filtered on the session company; ordered by (CONO, PLPN, PLPS, PLP2), the **assumed** 00-index. | 00-index key of MPOPLP, or the index to name in the query, so that rows come ordered by PLPN first: only then set `extract.index_contract_verified` (an order check on sample pages is necessary, not sufficient). Acceptance of the subset on the target version; literal quoting; behaviour for a division-scoped account. |
| C2 | Parameters `SEPC` (separator) and `HDRS` (header record). "If the character is '*' or '#', the field values are also prefixed with the field name ... followed by a space"; with another character "possible blanks are trimmed from the values". | Infor doc, same page | `SEPC`, `HDRS` honoured; `#` and `*` give `FIELD value` items. The agent's configuration **refuses** `#`, `*` (and `'`). | A value containing the chosen separator makes the scan incomplete (field count) and health degraded: choose a separator absent from the data. The page also describes a fixed-position input layout (SEPC "until position 16", HDRS at position 17); confirm how the REST call maps fields. |
| C3 | Query input field name `QERY`; output field name `REPL`. | **Infor forum only**: [19224](https://community.infor.com/discussion/19224/exportmi-authorization-issue-with-mec) (`QERY = 'F3PK02, ... from CUGEX3 where ...'`), [35373](https://community.infor.com/discussion/35373/usage-of-exportmi-in-smart-office-script) ("item .REPL"). Not named on the Infor doc page. | Uses `QERY` / `REPL`. | Read EXPORTMI/Select input and output fields in the API Repository; `m3sync check` fails visibly if `REPL` is absent. |
| C4 | "EXPORTMI can return 10,000 records at most. An exception applies if you specify an index and additional filter values which returns more than 10,000 records." | Infor doc, M3 UDI 16.x: [gub1592201251431](https://docs.infor.com/m3udi/16.x/en-us/m3beud/appfoundhs/gub1592201251431.html) | Hard ceiling 10,000 rows per call. | The agent pages at `page_cap` (default 1,000; configuration above 10,000 is refused) and treats a full page, or a page of 10,000 rows, as "more may follow", never as complete. Confirm the ceiling and its exception on the target. |
| C5 | v1 REST: `maxrecs` and `returncols` are matrix parameters (`.../LstByNumber;maxrecs=20?CONO=1`); JSON with `Accept: application/json`; records are `MIRecord` with `NameValue` pairs (`{"RowIndex":"0","NameValue":[{"Name":"CONO","Value":"100"}...`); error body `"@type":"ServerReturnedNOK"` with `"Message"`. | **Third party**: [m3ideas.org, 2012](https://m3ideas.org/2012/02/23/how-to-call-m3-api-with-rest-json/) (post and comments). No docs.infor.com page found describing this format. | v1 responses in that shape, `MIRecord: []` when empty; NOK body returned with HTTP 200 (**synthetic**: real status code unknown); default `maxrecs` 100 when absent (**synthetic**). | The agent always sends `maxrecs`; treats a NOK-shaped body as an error whatever the HTTP status; requires an `MIRecord` list in a success body (a Program-only envelope is refused, since that empty shape is unverified) and a matching `Program`/`Transaction` when present. Capture redacted real responses: success, empty result, NOK, auth failure. `Program`, `Transaction`, `Metadata`, `ErrorType`, `ErrorField` were found in no source. |
| C6 | v2 envelope: `results[]` items with `transaction`, `parameters`, `errorMessage`, `errorCode`, `errorField`, `errorType` (e.g. "ServerReturnedNOK"), `notProcessed`, `records[]`; top level `wasTerminated`, `terminationReason`, `nrOfSuccessfullTransactions`, `nrOfNotProcessedTransactions`, `nrOfFailedTransactions`, `terminationErrorType`, `bulkJobId`; `maxrecs`: "0 is unlimited". | Infor API catalog: [PPS170MI GetPOProp](https://developer.infor.com/pps170mi-741339/get-a-planned-purchase-order) (item fields read in the page's raw JSON; the rendered page shows only the envelope) | `ionapi_v2` responses in that shape; MI errors inside HTTP 200. The agent requires exactly one result for the expected transaction and refuses `notProcessed`, `nrOfNotProcessedTransactions`, any `error*` field, `wasTerminated`, `nrOfFailedTransactions`. | The full path `/M3/m3api-rest/v2/execute/...` is **not** shown in the catalog: confirm on the gateway. |
| C7 | ION API gateway path for v1 calls: `ACME_PRD/M3/m3api-rest/execute/CRS610MI/ChgFinancial?...` | Infor doc, Infor OS 2026.10: [fzk1514392112242](https://docs.infor.com/inforos/2026.10/en-us/useradminlib_cloud/apigatewayag_cloud/fzk1514392112242.html) (.NET example) | Profile `ionapi_v1`: v1 shape behind bearer auth. | Which of `ionapi_v1` / `ionapi_v2` the tenant's gateway exposes. |
| C8 | Backend applications: "the recommended grant to use is the resource owner ... only service accounts can be authenticated." | Infor doc, Infor OS 2026.10: [kwl1514558836698](https://docs.infor.com/inforos/2026.10/en-us/useradminlib_cloud/apigatewayag_cloud/kwl1514558836698.html) | `/token` password grant; bearer checked on every call. | The `.ionapi` field names the agent reads (`ci`, `cs`, `saak`, `sask`, `pu`, `ot`) were **not verified** in this pass; token lifetime and renewal; BE authorization context of the service account. Cloud profiles add outbound destinations (token endpoint, gateway) beyond the receiver. |
| C9 | Grid router: "the intersection of these methods and the methods supported by the configured session provider determines how authentication is performed ... Basic authentication is supported by all session providers." Client certificate logon exists over SSL. | Infor doc, M3 Core Technology 13.4.x: [phk1556137591271](https://docs.infor.com/m3coretech/13.4.x/en-us/m3coretechopolh/iongridsecag/phk1556137591271.html), [djf1556137577434](https://docs.infor.com/m3coretech/13.4.x/en-us/m3coretechopolh/iongridsecag/djf1556137577434.html) | Basic auth on `/m3api-rest/execute`. | Whether Basic is enabled on the router the agent reaches. Certificate logon is **not implemented** in the agent. |
| C10 | PPS170MI `GetPOProp` inputs: PLPN (required), CONO, PLPS (no PLP2). `LstPOProp` requires WHLO and PURC. The catalog also lists `LstPlannedPO`, `Select`, `SearchPOP`, `GetPlannedPO`, and write transactions (`RelPOP`, `UpdPOP`, `DelPlannedPO`, `CrtPOP`, ...). | Infor API catalog: [GetPOProp](https://developer.infor.com/pps170mi-741339/get-a-planned-purchase-order), [LstPOProp](https://developer.infor.com/pps170mi-741339/list-planned-purchase-orders-by-warehouse-and-requisitioner) | `GetPOProp` (PLPN, PLPS) returns the PLP2 = 0 line (**synthetic**). Used only by `m3sync check`. | Transactions other than GetPOProp and LstPOProp were read in the catalog page's raw JSON, not in its rendered text. Inspect the installed PPS170MI catalog. EXPORTMI is the proposed enumeration path unless an installed standard list transaction (candidates: `LstPlannedPO`, `Select`, `SearchPOP`) gives the required coverage and traversal semantics. Write transactions are outside the agent's allowlist. |
| C11 | MPOPLP fields with prefix PO: POPLPN (7,0), POPLPS (3,0), POPLP2 (5,0), POCUCD, POPUPR (17,6), POPPUN (purchase price U/M), POPUUN (purchase order U/M), POPSTS (2). | Infor doc, M3 UDI 16.x, MMS940: [kzk1567532067069](https://docs.infor.com/m3udi/16.x/en-us/m3beud/appfoundhs/kzk1567532067069.html) | Same names. | Value policy: conversions between PUUN and PPUN must be declared per item, because "Alternate units of measurement are defined per item in 'Item. Connect Alternate U/M' (MMS015)" (Infor doc, M3 UDI 16.x, [mws043](https://docs.infor.com/m3udi/16.x/en-us/m3beud/scexechs/mws043.html)); without one the value is unknown. Not on the MMS940 page and **assumed**: POFACI, POWHLO, POITNO, POSUNO, POBUYE, POPPQT, POPUCD, PODLDT, POLMDT, POCHNO, POLMTS. No status values are listed: `statuses` and `released_statuses` must come from the functional owner. Value policy assumes PPQT is in PUUN. |
| C12 | "Auto start job (MMS940) creates the final orders based on released order proposals." | Infor doc, M3 UDI 16.x, same page | Not simulated. | Release and order creation are separate steps; the agent never infers `released` from absence, and `released_statuses` stays empty until status semantics are validated. |
| C13 | Proposal identity (CONO, PLPN, PLPS, PLP2). | Inferred from C10/C11 field lists; **no source establishes the physical key** | Simulator key = these four fields. | Confirm the table key; until then identity is "validated source key, namespaced by tenant and installation". |
| C14 | "RGDT, RGTM, and LMDT are stored in the database in the &SYS ... LMTS is a time stamp in milliseconds in the UTC time zone." | Infor doc, M3 UDI 16.x: [drs045](https://docs.infor.com/m3udi/16.x/en-us/m3beud/appfoundhs/drs045.html) | LMDT, CHNO, LMTS populated. | The agent does **not** use LMDT, CHNO or LMTS for change detection or ordering (content fingerprint + agent sequence instead); they are carried as `source_version`. Check LMTS presence on MPOPLP. |
| C15 | Event Hub (13.4): "persisted events are routed to any subscriber that has not already received the event." | Infor doc, M3 Core Technology 13.4.x: [nvu1493403258780](https://docs.infor.com/m3coretech/13.4.x/en-us/m3coretechopolh/m3coreag_ibmi/nvu1493403258780.html) | No Event Hub simulated. The harness has a **notification fixture**, not an Event Hub. | Event Hub is not a disposable webhook; end-to-end losslessness to an external consumer is still not established. |
| C16 | "EC can act as a subscriber application through the MECEventHubSubscriber channel ... ensure that Event Hub and EC are running on the same grid." | Infor doc, M3 Core Technology 13.4.x: [iao1493403551667](https://docs.infor.com/m3coretech/13.4.x/en-us/m3coretechopolh/ientcolpatug/iao1493403551667.html) | Not simulated. | Supported event route if MEC is licensed and deployed; it would call the agent's optional LAN endpoint. |
| C17 | Event subscriber web service (CMS045/CMS049): JSON call, "Fire and forget"; restart autojob CMS949; "Erroneous calls are logged"; "If the read operation does not complete within the set time [MNS090], the process stops and the event is discarded." | Infor doc, M3 UDI 16.x: [zny1567533969128](https://docs.infor.com/m3udi/16.x/en-us/m3beud/appfoundhs/zny1567533969128.html) | Not simulated. | Presence on the target version. This is why the event path is only an acceleration: a discarded event is caught by the next complete reconciliation **if the proposal still exists then** (state sync contract). |
| C18 | Restriction for CloudSuite Fashion 10.2.3 single tenant: custom JARs or external integrations connecting Event Hub / Event Analytics to an ESB "is not permitted"; IEC, MEC or ION recommended. | Infor doc, CloudSuite Fashion 10.2.3: [lsi1509269094610](https://docs.infor.com/m3csfsh/10.2.3/en-us/m3csfshbom/fshtr/lsi1509269094610.html) | n/a | Scope is that product/deployment, not every on-premises 13.4 installation. It is not permission for an unsupported subscriber either: the agent ships none. |
| C19 | Initial load mapping: "Sync/Show Requisition MPOPLP", "Sync/Show PurchaseOrder MPHEAD", ItemMaster MITMAS, SupplierPartyMaster CIDMAS. | Infor doc, M3 Core 2026.10 (cloud): [tpe1511966504478](https://docs.infor.com/m3core/2026.10/en-us/useradminlib_cloud/m3cecoreag_cloud/tpe1511966504478.html) | n/a | Lead for the optional ION path, see `docs/ION_NOTE.md`. Not evidence the mapping is installed on the target. |
| C20 | REST metadata (WADL) at `.../m3api-rest`; API Repository under Tools. | Infor doc, M3 Core Technology 13.4.x: [icj1576017236547](https://docs.infor.com/m3coretech/13.4.x/en-us/m3coretechopolh/m3befndag/icj1576017236547.html) | n/a | First step of the compatibility procedure. |
| C21 | MMS200MI `GetItmBasic`: ITNO required, output ITGR (item group). | Infor API catalog: [get-item-basic-data-1](https://developer.infor.com/mms200mi-494961/get-item-basic-data-1) | `GetItmBasic` returns ITNO, ITDS, ITGR, STAT. | Field present and authorized for the service account. |
| C23 | Session company probe: `PPS170MI/GetUserInfo` appears among the generic transactions of the PPS170MI catalog (raw JSON of the catalog page). Its output fields are **not verified**; the agent reads the field named by `m3.context_company_field` (default `CONO`). | Infor API catalog: [PPS170MI](https://developer.infor.com/pps170mi-741339/get-a-planned-purchase-order) | Returns `USID`, `CONO`, `DIVI` (**synthetic** names). | Confirm the transaction and the field that carries the session company (and division) on the target; set `context_probe` / `context_company_field` accordingly. Until the probe works, nothing is ever withdrawn (`not_observed`) and health says so. |
| C22 | Metadata Publisher in Core 13.4 release notes carries "M3BE 15.1.4 metadata". | Infor doc, M3 Core Technology 13.4.x release notes: [jxu1576017072820](https://docs.infor.com/m3coretech/13.4.x/en-us/m3coretechopolh/m3coretechrn/jxu1576017072820.html) | n/a | See section 1. |

Synthetic behaviours (no source, chosen for the harness only): NOK wording ("does not exist"),
HTTP status of a NOK, v1 default `maxrecs`, `GetPOProp` line selection, the 00-index order, all
fixture values (status "20", buyer "BUYER1", prices, items).

## 3. Design choices made because of what is not known

| Unknown | Agent behaviour |
|---|---|
| Event delivery can drop (C17) and events cannot be relied on to reach an external consumer | Polling is the executable baseline; no listener by default. |
| Polling sees current state only | Contract is state sync: convergence to the observable state after complete reads; intermediate transitions and short-lived proposals are not reconstructed (shown by `test_p0_02`). |
| Session company (C1: always the API session's) | Probed before each scan, independently of rows; wrong company refuses the scan; unknown company disables absence. |
| Index order (C1) | Absence transitions only when `index_contract_verified` is set after checking the index. |
| Total row count, ceiling and page semantics (C4, C5) | PLPN keyset traversal with `>=` only; a full page re-reads its last PLPN; a page filled by one PLPN, a field-count mismatch, a wrong company, an order violation or any error makes the scan **incomplete**. |
| Why a row disappears (C12) | `not_observed` only after `absence_confirmations` complete scans plus a targeted read returning zero rows; never `released` by inference. No absence processing after an incomplete scan. |
| Error vs. empty result | An empty result counts only from a successful call; any MI error, auth error, protocol mismatch or timeout is an error, never "not found". |
| Version fields (C14) | Not used for ordering; agent sequence numbers order deliveries; the receiver ignores older sequences (A to B to A safe). |
| Key uniqueness across customers | Delivery identifiers and receiver rows are namespaced by tenant and installation; the client certificate binds the identity. |
| Which tables EXPORTMI may read for the service account | Table and prefix fixed in code to MPOPLP/PO; restrict the account on the M3 side as well. |
| Write access | Allowlist of four read transactions (EXPORTMI/Select, PPS170MI/GetPOProp, PPS170MI/GetUserInfo, MMS200MI/GetItmBasic); `RelPOP`, `UpdPOP`, `DelPlannedPO` and every other transaction are refused before any network call. |

## 4. Read-only compatibility procedure

Run on the customer side by their administrator, with the service account the agent will use.
Nothing is sent to anyone; the report is a local file to review before sharing.

1. In the API Repository (C20), confirm that EXPORTMI/Select, PPS170MI (list its transactions)
   and MMS200MI/GetItmBasic exist, and note EXPORTMI's input and output field names (C2, C3).
2. Fill section 1 (component versions, router auth methods, event route, ION).
3. Grant the service account MI authority to EXPORTMI, MMS200MI and PPS170MI read transactions
   only, and table access to MPOPLP for EXPORTMI (the forum thread in C3 reports an EXPORTMI
   authorization issue under MEC).
4. Run `m3sync check --config agent.toml --out report.json`. It calls the session context probe, EXPORTMI/Select (sample of
   200 rows, all configured fields; on failure, one probe per field), PPS170MI/GetPOProp on one
   key and MMS200MI/GetItmBasic on one item, then reports: fields accepted, row order of the
   default index, company context, status codes, unit pairs and currencies seen, presence of
   LMDT/CHNO/LMTS, GetPOProp output fields. Keys and business values are replaced by salted
   hashes; MI errors appear as categories (kind, code, field), never as raw messages. Each check is
   pass, fail or not_exercised (empty sample); exit code 3 means inconclusive. The report lists the
   transactions actually invoked.
4b. Verify the MPOPLP index EXPORTMI uses (or the one to name) orders rows by PLPN first, then set
   `extract.index_contract_verified = true`.
5. Capture, redacted, one raw response per transaction: success, empty result, NOK, auth
   failure (C5, C6).
6. Decide traversal (`page_cap`, discovery filter), status configuration and value policy from
   the report, then run the agent in a test company against a non-production receiver.

## 5. Live gate: NOT RUN

| Check | Status |
|---|---|
| Transaction catalog and fields on the customer's version | not run |
| Parsing of real v1/v2 responses (success, empty, NOK, auth) | not run |
| Traversal order and completeness on real MPOPLP volumes | not run |
| Lifecycle (release, order creation, deletion) observed through authorized operator actions and reads; the agent never calls RelPOP/UpdPOP to provoke it | not run |
| Authentication profile on the real router or gateway | not run |
| Load: request rate acceptable to the installation | not run |

## 6. Administrative changes a deployment still needs

No business-code or database-schema modification. Still required: a service account and its MI
authorizations, trust material (M3 CA, receiver CA, client certificate), a persistent volume, and
for the optional event path an event subscription (and autojob restart for CMS045/CMS049) plus an
internal firewall rule to the agent's LAN endpoint, both subject to the customer's approval.
Agent updates need no inbound port: the customer's administrator pulls a new image and restarts
the container on the same volume (procedure not yet tested, see ACCEPTANCE.md).
