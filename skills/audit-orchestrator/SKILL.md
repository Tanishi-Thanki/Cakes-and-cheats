---
name: audit-orchestrator
description: Single composition and entrypoint layer for the marketplace. Runs a bounded, same-site crawl (robots.txt, sitemap discovery, page/depth limits), observes each fetched page once, runs every registered specialist audit per page plus the multi-page analysis layers, conservatively aggregates and ID-hardens findings, and emits the final structured report as JSON.
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
  ├─ crawl_site     (crawler: bounded, same-site BFS — robots.txt, sitemap
  │                  discovery, page/depth limits; one read-only GET per page)
  ├─ observe_page   (observation: one HTML parse per crawled page, no network)
  ├─ audit 1..N     (each registered specialist audit receives the SAME
  │                  PageObservation per page)
  ├─ p2_evidence + entity_identity   (multi-page layers, run once on the
  │                  complete observation collection)
  ├─ tag findings   (source audit attribution)
  ├─ aggregate      (deduplicate + harden IDs + sort)
  ├─ objective mapping + priority signals   (run_audit_full only)
  └─ build_report   (final structured report)
```

## Key Behaviors

- **Bounded multi-page crawl**: `run_audit` / `run_audit_full` crawl the target
  site with `crawl_site` (page limit, depth limit, per-request timeout, robots.txt,
  sitemap discovery). Each crawled page becomes one `PageObservation`; every audit
  consumes the *identical* per-page observation. No audit re-crawls or re-parses.
- **Shared observation / reuse**: exactly one `crawl_site` and one `observe_page`
  per page per run; every audit consumes the same `PageObservation` objects in the
  same order.
- **Always-on multi-page layers**: `p2_evidence` and `entity_identity` run once on
  the complete observation collection, independent of the per-page audits.
- **Finding aggregation**: all audit findings are combined, source-tagged,
  deduplicated, ID-hardened, and ordered.
- **Deduplication**: conservative. Two findings collapse only when the rule id is identical
  AND evidence is identical up to whitespace. Distinct or differently-evidenced findings are never merged.
- **Finding-ID hardening (P3-4)**: rule IDs that occur once keep their original form;
  when a rule fires multiple times in one report, each instance becomes
  `<RULE-ID>-<12-hex digest>` derived deterministically from the rule id and the
  finding's own canonical evidence (SHA-256). IDs do not depend on generation
  order, object identity, Python hash randomization, time, or randomness, and are
  stable across processes. Aggregation and reporting preserve IDs.
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

Both `run_audit` and `run_audit_full` default to all seven specialist audits.
`run_audit` returns an `AuditResult`; `run_audit_full` is the designated complete marketplace entrypoint
returning `(AuditResult, AuditReport)`. It adds objective mapping, priority engine, and structured
reporting on top of the `run_audit` result.

Default audit set (`_DEFAULT_AUDITS`):
```
machine_answerability, task_readiness, crawl_render, engagement, evidence_consistency, freshness_corroboration, t4_indexability
```
In addition, two multi-page analysis layers always run on the complete observation
collection: the P2 evidence audit (`audit_p2_evidence`, findings tagged
`p2_evidence`) and the entity identity audit (`audit_entity_identity`, findings
tagged `entity_identity`). They are not per-page specialist audits and are not
part of `_DEFAULT_AUDITS` / `audits_run`.
Explicit audit selection (via `AuditRequest.audits` or `AuditOptions.audits`) overrides the default.

### Entity identity (P3-2)

`audit_entity_identity` compares identity signals (domain, phone, email domain,
address, name, sameAs) across the crawled pages. It is deterministic and offline —
no external web search and no LLM. Classification semantics:

- `MATCH` → no entity identity finding
- `INSUFFICIENT_EVIDENCE` → no entity identity finding
- `CONFLICT` → `ENT-ID-001` (high)
- `AMBIGUOUS` → `ENT-ID-002` (medium)

### Evidence semantics (P3-3)

Uncertainty is not a defect. Only a genuine value contradiction (`CONFLICT`)
produces an evidence finding. `MATCH`, `AMBIGUOUS`, `INSUFFICIENT_EVIDENCE`, and
the temporal states (`HISTORICAL`, `EXPIRED`, `FUTURE`) describe uncertainty about
a signal and never generate findings on their own. Cross-page contradictions are
reported as `EVID-001` (from `p2_evidence`); same-page cross-representation
conflicts are reported by the `evidence_consistency` audit.

The report serializes to:

```json
{
  "site": "https://example.com/",
  "audited_at": null,
  "task_context": {
    "task_intent": "purchase running shoes",
    "task_description": null,
    "target_objectives": ["transaction_completion"]
  },
  "summary": {
    "total_findings": 3,
    "high": 1,
    "medium": 1,
    "low": 1
  },
  "audits_run": [
    "machine_answerability",
    "task_readiness",
    "crawl_render",
    "engagement",
    "evidence_consistency",
    "freshness_corroboration",
    "t4_indexability"
  ],
  "findings": [
    {
      "id": "TR-INT-001",
      "title": "Critical task action affordance is non-navigable",
      "severity": "high",
      "evidence": "Observed facts supporting the finding.",
      "suggested_action": { "summary": "Remediation advice.", "priority": 1 },
      "priority": 1,
      "confidence": "high",
      "source_audit": "task_readiness",
      "affected_objectives": ["transaction_completion"],
      "task_relevance": "direct_blocker",
      "priority_reason": "Directly affects the requested transaction_completion objective because the purchase action is not navigable.",
      "details": { "structured": "diagnostics" }
    }
  ]
}
```

- `audited_at` is only present when supplied by the caller — never auto-generated — so output is reproducible.
- `task_context` and per-finding `affected_objectives`, `task_relevance`, and `priority_reason` are included when task context is supplied in `AuditRequest`. When no task context is supplied, the legacy schema is preserved.
- On pipeline failure, a concise `failure` block (`url`, `status_code`, `message`) replaces any traceback.
- Per-audit failures appear in `audit_errors`.

## Future Audit Integration

Any future audit that conforms to the existing contract — a callable
`audit_x(obs: PageObservation) -> list[Finding]` producing `Finding` objects — plugs into the
orchestrator registry by name with **no changes to the pipeline, aggregation, or reporting code**:

1. Add the audit module.
2. Register it in the audit registry under a stable name.
3. Add it to `_DEFAULT_AUDITS` to include it in the default run.

Its findings then flow automatically through tagging, deduplication, priority ordering,
and the final report. The orchestrator imposes no fixed audit count.

## Design Constraints

- No LLM, no external AI APIs, no browser automation, no analytics, no database.
- No writes to the audited site (read-only throughout).
- Deterministic: same URL + same HTML → same findings and same report.