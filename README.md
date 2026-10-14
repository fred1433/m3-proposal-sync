# M3 proposal-sync reference agent and failure harness

> **The agent and reference receiver run locally. Source behaviour is simulated. No tests have
> been run against an M3 installation; this is not a validated M3 connector.**
> What is known about M3, from which source and version, and what remains to check:
> [M3_VALIDATION.md](M3_VALIDATION.md).

An agent that runs next to an on-premises M3, reads purchase-order proposals (MPOPLP) through
read-only MI calls, and delivers their state to a SaaS over outbound mTLS. No listening port, no
write transaction, no change to M3 code or schema. Around it, a harness injects the failures that
break this kind of integration and records what happens.

```
[T+02:00] source: proposal 100/4100003/1/0 created; notification fixture DROPS its notification
[T+15:00] queued seq=3 100/4100003/1/0 state=active
[T+15:00] ERROR deliver seq=3: transport failure: RemoteDisconnected: Remote end closed connection without response; kept pending, retry in 9.7s
[T+15:00] receiver: 100/4100003/1/0 committed 13 min after creation (polling interval 15 min; simulated time)
[T+16:00] deliver seq=3 -> 200 duplicate
[T+18:00] receiver: delivery tenant-a:m3-prod:g1:100/4100003/1/0:3: 1 delivery record, 1 effect; agent outbox {'delivered': 3}
[T+33:02] scan not authoritative (page 2 failed: http: HTTP 500); no absence processing, checkpoint not advanced
[T+33:02] receiver: {'100/4100001/1/0': ('active', 1), '100/4100002/1/0': ('active', 2), '100/4100003/1/0': ('active', 3)}
```
<sub>Lines from [`test_headline_sequence.log`](evidence/runs/test_headline_sequence.log) (simulated time, simulated M3): a missed notification caught by the next scan, a lost acknowledgement with no second effect, and a failed scan page that withdraws nothing although 4100001 was really deleted.</sub>

**What it guarantees** (each line links to its recorded test in [ACCEPTANCE.md](ACCEPTANCE.md)):

- **State sync.** After authoritative reads succeed, the receiver converges to the current
  observable state. Polling cannot see a proposal created and removed between two scans, nor
  intermediate changes; the 15-minute interval is a schedule, not a recovery bound.
- **One effect per delivery.** At least once from the agent; the receiver applies each delivery
  identifier once, in (generation, sequence) order, and acknowledges only after commit. A 200
  without a valid acknowledgement keeps the delivery pending.
- **Nothing withdrawn on doubt.** `not_observed` only after several authoritative scans (complete
  traversal, session company confirmed, index contract verified) and a targeted read that finds
  nothing. Never "released" by inference.
- **Redeploying is safe.** At every start the agent gets a new generation from the receiver
  (fencing) and resynchronizes with the inventory the receiver holds. Lost volume, restored
  backup, cloned volume and host clock rollback are tested; an older copy still running is fenced.
- **Failures are visible.** Configuration, transport, protocol and fencing problems, stalled scans
  and old pending deliveries turn `m3sync health` (the Docker health check) to degraded.

**What it does not prove:** anything about a real M3. Field names, the MPOPLP index, the session
context probe and response shapes are listed in M3_VALIDATION with the check each still needs.
The live gate is **not run**.

**Run it:** `scripts/run_evidence.sh` (whole suite in the Linux test image, raw outputs in
`evidence/`), or locally `uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -e '.[test]'
&& .venv/bin/python -m pytest`.

**Stack:** Python 3.12, SQLite (WAL, transactional outbox) and one dependency (`cryptography`), in
one non-root Docker image with no exposed port; the reasons are in DESIGN.

**More:** [docs/DESIGN.md](docs/DESIGN.md) (M3 reads, value policy, delivery contract, network
boundary, secrets, operations, why Python and SQLite), [docs/ION_NOTE.md](docs/ION_NOTE.md),
[examples/agent.toml](examples/agent.toml). MIT licence.
