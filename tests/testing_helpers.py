"""Minimal shared test helpers for the audit-orchestrator test suite.

Kept intentionally small. Shared patch logic lives here instead of being
duplicated throughout individual test modules.
"""

from __future__ import annotations

import functools
from collections.abc import Callable

from src.orchestrator import AUDIT_REGISTRY, _ensure_optional_audits_registered


def _stub_t4_no_findings(func: Callable) -> Callable:
    """Run an orchestrator-mechanics test with the always-on t4_indexability audit
    stubbed to return no findings. These tests deliberately drive MA/TR with mocks and
    assert on their findings only; T4 emits informational findings unconditionally."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        _ensure_optional_audits_registered()
        original = AUDIT_REGISTRY.get("t4_indexability")
        AUDIT_REGISTRY["t4_indexability"] = lambda _obs: []
        try:
            return func(*args, **kwargs)
        finally:
            if original is not None:
                AUDIT_REGISTRY["t4_indexability"] = original

    return wrapper