"""M3 proposal-sync reference agent.

Read-only reconciliation of M3 purchase-order proposals (table MPOPLP) and
outbound-only, at-least-once delivery to an idempotent receiver.

Source behaviour in this repository is simulated. Nothing here has been run
against an M3 installation. See M3_VALIDATION.md.
"""

__version__ = "0.1.0"
