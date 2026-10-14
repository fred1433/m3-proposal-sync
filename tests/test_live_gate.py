"""Live gate: actual customer-version validation. NOT RUN.

It requires an authorized M3 installation, a service account with the
read-only transactions, and redacted captures of real responses. Until those
exist, this gate stays visibly skipped. See M3_VALIDATION.md, section
"Live gate".
"""

import pytest


@pytest.mark.skip(reason="Live gate NOT RUN: no M3 installation has been available to this project")
def test_live_gate_customer_version_validation():
    """Catalog, parsing, traversal, lifecycle, authentication and load against a real installation."""
