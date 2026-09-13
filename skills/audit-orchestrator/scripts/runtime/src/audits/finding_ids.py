"""P3-4: deterministic finding-ID utilities.

Audits assign a stable *base* rule ID to every finding (e.g. ``MA-IMG-001``,
``ENT-ID-001``, ``T4-024``). When the same rule fires several times within one
report — once per page or once per conflicting target — those findings share
the same base ID. That is correct at the *rule* level but not unique enough at
the *report* level.

This module provides the deterministic disambiguation layer:

- ``stable_digest``  – SHA-256 digest over a canonical (whitespace-normalised,
  ordered) representation. SHA-256 is stable across processes and independent
  of Python hash randomisation (``PYTHONHASHSEED``), object identity, and
  set/dict iteration order.
- ``instance_finding_id`` – base rule ID plus the digest, e.g.
  ``T4-024-a1b2c3d4e5f6``.
- ``disambiguate_finding_ids`` – applied at aggregation. Findings whose base ID
  appears exactly once keep their original ID untouched (preserving existing
  conventions verbatim). Base IDs that appear more than once get a deterministic
  instance suffix derived only from the finding's own semantic evidence, so the
  mapping is independent of generation order.

Guidelines honoured here:
- no ``uuid``, timestamps, ``id()``, ``hash()``, random values, list-position,
  or iteration-order based identity;
- canonicalisation is deterministic (whitespace is collapsed, values are
  compared as their canonical strings);
- the base rule ID is always preserved as the prefix of the final ID, so
  rule-level semantics (objective mapping, priority signals, reporting) and
  downstream consumers that match on the stable prefix keep working.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import replace
from typing import Iterable, Sequence

from src.audits.models import Finding

#: Length of the hex digest shown after the base rule ID (12 hex chars = 48 bits).
_DIGEST_LENGTH: int = 12

_WHITESPACE: re.Pattern[str] = re.compile(r"\s+")

_FIELD_SEPARATOR: str = "\x1f"


def canonicalize(value: str) -> str:
    """Canonicalise a string for digesting: collapse whitespace, strip edges.

    Deterministic across processes and independent of ``PYTHONHASHSEED``.
    """
    return _WHITESPACE.sub(" ", value or "").strip()


def stable_digest(*parts: str) -> str:
    """Return a short stable SHA-256 digest over canonicalised ``parts``.

    ``parts`` are canonicalised (order of call arguments is the only ordering)
    and joined with a reserved field separator, so different field boundaries
    can never accidentally collide.
    """
    canonical = _FIELD_SEPARATOR.join(canonicalize(p) for p in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]


def instance_finding_id(base_id: str, *discriminators: str) -> str:
    """Build a deterministic instance ID: ``<base_id>-<digest>``.

    The discriminator must come from stable semantic fields that define the
    finding (target URL, evidence, selector, ...) — never from position,
    hashing, or runtime-generated values.
    """
    digest = stable_digest(base_id, *discriminators)
    return f"{base_id}-{digest}"


def disambiguate_finding_ids(findings: Sequence[Finding]) -> list[Finding]:
    """Make finding IDs unique within the list while staying deterministic.

    Rule:
    - A base ID that occurs exactly once keeps its original ID verbatim
      (existing single-instance conventions such as ``ENT-ID-001`` and ``T4-005``
      are preserved unchanged).
    - A base ID that occurs more than once is disambiguated per instance using a
      digest of the base ID and the finding's own semantic evidence. Because the
      suffix is derived from each finding's own fields, the resulting IDs do not
      depend on the order the findings were generated (requirement B).

    Args:
        findings: Findings that have already been deduplicated (same base ID and
            identical canonical evidence collapses to one finding).

    Returns:
        A list of findings with deterministic, within-report-unique IDs. Findings
        that needed no disambiguation are returned as-is (same object); others are
        replaced with new frozen dataclass instances carrying the same fields.
    """
    if not findings:
        return list(findings)

    counts = Counter(f.id for f in findings)
    result: list[Finding] = []
    for finding in findings:
        if counts[finding.id] == 1:
            result.append(finding)
            continue
        stable = instance_finding_id(finding.id, finding.evidence)
        result.append(
            replace(finding, id=stable)
            if stable != finding.id
            else finding
        )
    return result


__all__ = [
    "canonicalize",
    "stable_digest",
    "instance_finding_id",
    "disambiguate_finding_ids",
]