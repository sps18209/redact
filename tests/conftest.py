"""Shared fixtures.

The suite's whole point is that it routes to the *best available* backend, so a
test asserting "this went to builtin" is really asserting "nothing better is
installed" — which silently became false the moment Presidio landed in this
environment. Tests that care about the builtin path build an explicit registry
instead; tests that care about routing *policy* assert on priority.
"""

import pytest

from redact import BackendRegistry, RedactionSuite
from redact.backends.builtin import BuiltinBackend


@pytest.fixture
def builtin_only_suite():
    """A suite with just the builtin backend, independent of the environment."""
    return RedactionSuite(registry=BackendRegistry([BuiltinBackend()]))
