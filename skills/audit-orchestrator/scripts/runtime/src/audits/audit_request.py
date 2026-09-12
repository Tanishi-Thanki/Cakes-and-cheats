"""Audit Request models for the orchestrator.

Defines the structured input to the audit pipeline. The orchestrator accepts
either a plain URL string (backward-compatible) or a full AuditRequest that
carries task context, configuration, and optional metadata.

Design principle: lightweight and provider-neutral. The evaluator interacts
through free-form agent requests, so the internal API must be generic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class AuditContext:
    """Optional task/objective context attached to an audit request.

    Attributes:
        task_intent: High-level intent string (e.g. "purchase shoes",
            "compare prices", "register for service"). Provider-neutral.
        task_description: Free-form description of the task the agent
            is trying to accomplish.
        target_objectives: Named objectives this audit should evaluate
            relevance against. Empty list means no specific objectives.
        extra: Arbitrary configuration payload for extensibility.
            Audits or downstream components may read from this dict
            but should not assume specific keys exist.
    """

    task_intent: Optional[str] = None
    task_description: Optional[str] = None
    target_objectives: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditRequest:
    """Structured audit request accepted by the orchestrator.

    Attributes:
        target_url: The URL to audit. Must be http or https.
        context: Optional task/objective context for relevance scoring
            and objective mapping. When None, the pipeline runs with
            default evaluation parameters.
        audits: Optional tuple of audit names to run. When None, all
            registered default audits execute. Overrides AuditOptions.audits
            if both are provided.
        timeout_seconds: HTTP read/connect timeout. Uses crawler default
            when None.
        user_agent: User-Agent header. Uses crawler default when None.
        include_details: Whether Finding.details should be included in
            serialised report output.
    """

    target_url: str
    context: Optional[AuditContext] = None
    audits: Optional[tuple[str, ...]] = None
    timeout_seconds: Optional[float] = None
    user_agent: Optional[str] = None
    include_details: bool = False
