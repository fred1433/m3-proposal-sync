# Optional ION path: decision note (not built)

No BOD converter ships in this repository. Calling one "ION support" without an installation to
validate it against would be decoration.

## The lead

Infor's initial-load mapping for M3 Core 2026.10 (cloud) lists "Sync/Show Requisition MPOPLP"
and "Sync/Show PurchaseOrder MPHEAD" ([tpe1511966504478](https://docs.infor.com/m3core/2026.10/en-us/useradminlib_cloud/m3cecoreag_cloud/tpe1511966504478.html)).
So the BOD to investigate for purchase proposals is **Requisition**, not PurchaseOrder, which
maps to the order header table. ItemMaster (MITMAS) and SupplierPartyMaster (CIDMAS) appear in the
same list for enrichment. This is a cloud-generation page; it is not evidence that the mapping is
installed, configured or sufficient on an on-premises installation.

## Questions to answer on the target before building anything

| Question | Why |
|---|---|
| Which Requisition BOD version is published, and by which M3/ION component? | Contract fixture to parse. |
| Does it cover purchase proposals from MPOPLP only, or other proposal types? | Scope and filtering. |
| Which identifiers does it carry (CONO, PLPN, PLPS, PLP2)? | Must map to the same source key as the EXPORTMI path. |
| How are updates and removals expressed (action codes, Show vs Sync)? | Lifecycle: does removal mean deletion, release, or a filter change? |
| Can BODs be lost or replayed, and is there a sequence? | Ordering and idempotency at the receiver. |
| How does a consumer outside ION receive them without opening an inbound port? | The agent must stay outbound-only. |

## How it would fit

A BOD would be one more **notification**: it names a key, the agent reads the row itself with
the same exact-key EXPORTMI read, and the result goes through the same fingerprint, sequence and
outbox. Reconciliation stays the source of truth. If the questions above show that BODs carry a
validated, ordered change history, that changes the contract (transitions, not just state), and
the receiver contract would need a source sequence field.
