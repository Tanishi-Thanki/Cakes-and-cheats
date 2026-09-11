---
name: Audit Orchestrator
description: Single composition and entrypoint layer for the marketplace. Fetches each audited URL once, observes it once, runs every registered audit against the same observation, conservatively aggregates findings, and emits the final structured report as JSON.
---

# Audit Orchestrator

## Purpose

The Audit Orchestrator is the **single entrypoint** of the marketplace. It composes the
crawler, observation layer, and registered audit skills into one deterministic pipeline
and produces the final structured report. It is not an audit itself: it coordinates,
aggregates, and reports.

## Inputs

- A target URL string, or an `AuditRequest` carrying:
  - `target_url` (required)
  - optional `context` with `task_intent`, `task_description`, `target_objectives`
  - optional `audits` selection, HTTP `timeout_seconds`, `user_agent`, `include_details`

## Execution Flow

```
URL / AuditRequest
  ├─ fetch_page      (crawler: one read-only HTTP GET)
  ├─ observe_page    (observation: one HTML parse, no network)
  ├─ audit 1..N      (each registered audit receives the SAME PageObservation)
  ├─ tag findings    (source audit attribution)
  ├─ aggregate       (deduplicate + sort)
  ├─ objective mapping + priority signals   (run_audit_full only)
  └─ build_report    (final structured report)
```

## Key Behaviors

- **Shared observation / reuse**: one `fetch_page` and one `observe_page` call per run;
  every audit consumes the *identical* `PageObservation` object. No audit re-crawls or re-parses.
- **Finding aggregation**: all audit findings are combined, source-tagged, deduplicated, and ordered.
- **Deduplication**: conservative. Two findings collapse only when the rule id is identical
  AND evidence is identical up to whitespace. Distinct or differently-evidenced findings are never merged.
- **Priority / severity preservation**: findings are ordered by the audit-supplied
  `priority` (ascending) then `severity` (critical → low). The orchestrator never invents,
  rewrites, or reinterprets severity or priority values.
- **Evidence preservation**: full evidence strings and structured `details` survive into the
  report unchanged. Nothing is truncated, paraphrased, or collapsed into an opaque string.
- **Suggested actions**: every finding's `suggested_action` (summary + priority) is preserved verbatim.
- **Error isolation**: an exception in any single audit is captured as a structured
  `audit_errors` entry; the remaining audits still run. A pipeline-level failure produces a
  structured `failure` block, never a Python traceback in the consumer-facing report.
- **Final reporting**: `build_report` turns the orchestrated result into an `AuditReport`,
  which serializes to the JSON schema below (deterministic, sorted keys).

## Output Schema

`run_audit` returns an `AuditResult`; `run_audit_full` returns `(AuditResult, AuditReport)`.
The report serializes to:

```json
{
  "site": "https://example.com/",
  "audited_at": null,
  "summary": {
    "total_findings": 3,
    "high": 1,
    "medium": 1,
    "low": 1
  },
  "audits_run": ["machine_answerability", "task_readiness"],
  "findings": [
    {
      "id": "MA-IMG-001",
      "title": "Image content dependency",
      "severity": "medium",
      "evidence": "Observed facts supporting the finding.",
      "suggested_action": { "summary": "Remediation advice.", "priority": 2 },
      "priority": 2,
      "confidence": "high",
      "source_audit": "machine_answerability",
      "details": { "structured": "diagnostics" }
    }
  ]
}
```

- `audited_at` is only present when supplied by the caller — never auto-generated — so output is reproducible.
- On pipeline failure, a concise `failure` block (`url`, `status_code`, `message`) replaces any traceback.
- Per-audit failures appear in `audit_errors`.

## Future Audit Integration

Any future audit that conforms to the existing contract — a callable
`audit_x(obs: PageObservation) -> list[Finding]` producing `Finding` objects — plugs into the
orchestrator registry by name with **no changes to the pipeline, aggregation, or reporting code**:

1. Add the audit module.
2. Register it in the audit registry under a stable name.
3. Optionally add it to the default audit set.

Its findings then flow automatically through tagging, deduplication, priority ordering,
and the final report. The orchestrator imposes no fixed audit count.

## Design Constraints

- No LLM, no external AI APIs, no browser automation, no analytics, no database.
- No writes to the audited site (read-only throughout).
- Deterministic: same URL + same HTML → same findings and same report.