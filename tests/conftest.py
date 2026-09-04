"""Test-suite environment isolation.

The suite must stay hermetic: a developer's local ``.env`` (or exported
``AGENTSUPPORT_*`` variables) must not change which mode tests run in.
Tests that need postgres/temporal/docker opt in explicitly through
``Settings(...)``.
"""

from __future__ import annotations

import os

os.environ["AGENTSUPPORT_PERSISTENCE_MODE"] = "postgres"
os.environ["AGENTSUPPORT_EXECUTION_MODE"] = "temporal"
os.environ["AGENTSUPPORT_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["AGENTSUPPORT_REDIS_URL"] = ""
os.environ["AGENTSUPPORT_AUTO_CREATE_SCHEMA"] = "true"
