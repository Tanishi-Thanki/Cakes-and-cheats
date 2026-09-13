---
name: t4-indexability
description: Evaluates indexability, robots directives, canonical declarations, and JSON-LD structured data signals that determine whether AI-driven web agents can discover, fetch, and attribute website content. Consumes the shared PageObservation; emits Finding objects.
---

# T4 Indexability Audit

> **STATUS: AVAILABLE — IMPLEMENTED**
>
> Implemented in the bundled runtime (`skills/audit-orchestrator/scripts/runtime/src/audits/t4_indexability.py`) as `audit_t4_indexability(obs) -> list[Finding]`
> and registered in the orchestrator under the stable name `t4_indexability` as part of the
> default seven-audit pipeline (callers may also explicitly select a subset via
> `AuditOptions.audits` / `AuditRequest.audits`).

## Purpose

Answers the question: *"Can an AI-driven web agent find, fetch, and attribute this content
reliably?"*

It checks four independent signal groups that govern whether automated consumers will
discover a URL, be told how to treat the page, and be able to attribute the page to the
right resource identity:

1. **Indexability** — did the page deliver, is it HTML, and is it explicitly or implicitly
   indexable?
2. **Robots directives** — robots meta / X-Robots-Tag exposure.
3. **Canonical** — self-referencing, internal-alternate, external, or malformed declarations.
4. **Structured data** — JSON-LD presence, `@type`/`@context` completeness, and parse validity.

It is **not a generic SEO checker** — it focuses on deterministic, observable signals that an
automated agent can read directly from the delivered representation.

## Problem Detected

- Pages that fail to deliver, deliver non-HTML payloads, or are explicitly excluded from
  indexing.
- Conflicting or incomplete robots instructions (meta vs header).
- Canonical declarations that are missing, malformed, external, or point at a different
  resource.
- JSON-LD that is missing, missing required keys, or malformed — degrading machine
  interpretation.

## Inputs

A single `PageObservation` (URL, final URL, success flag, error message, content type,
robots meta directives, X-Robots-Tag directives, canonical URL, JSON-LD blocks). Consumes
observation only — **no re-fetch, no raw HTML re-parse, no rendered DOM, no browser
automation**.

## Output / Finding Contract

Returns a list of `Finding` objects conforming to the shared contract
(`id`, `title`, `severity`, `evidence`, `suggested_action`, `priority`, `confidence`,
`details`).

## Detection Philosophy

- Evidence before inference: only observed delivery, directive, canonical, and structured-data
  facts become findings.
- Deterministic: the same observation always produces the same findings; no external models or
  APIs.
- Conservative classification: pages with no canonical are reported as a low-risk action, not a
  failure; pages that are implicitly indexable are observations, not defects.
- Read-only, provider-neutral, industry-neutral.

## Implemented Rules

| id | Category | Severity |
|----|----------|----------|
| `T4-001` | Page fetch failed | high |
| `T4-002` | Non-HTML response | medium |
| `T4-003` | Explicit noindex observed | high |
| `T4-004` | Explicit index observed | low (observation) |
| `T4-005` | Indexability not explicitly specified | low (observation) |
| `T4-010` | Robots meta directives present | low (observation) |
| `T4-011` | X-Robots-Tag directives present | low (observation) |
| `T4-020` | Canonical tag missing | low |
| `T4-021` | Canonical points to itself | low (observation) |
| `T4-022` | Canonical URL unresolvable | high |
| `T4-023` | Canonical points to external domain | medium |
| `T4-024` | Canonical points to internal alternate URL | low (observation) |
| `T4-030` | No JSON-LD structured data | low (observation) |
| `T4-031` | JSON-LD blocks detected | low (observation) |
| `T4-032` | Detected @type values | low (observation) |
| `T4-033` | JSON-LD missing @context | medium |
| `T4-034` | JSON-LD missing @type | medium |
| `T4-035` | Malformed JSON-LD block | high |

Rule guardrails:

- `T4-001` short-circuits: if the page failed to deliver, no further content-level checks run.
- The `none` robots token is excluded from `T4-010`/`T4-011` so an empty/none directive list is
  never reported as a directive.
- `noindex` handling treats `none` as equivalent; source attribution distinguishes meta vs
  header.
- Canonical comparison is semantic: tracking parameters are stripped, fragments dropped,
  trailing slashes collapsed, default ports folded; host affinity uses registrable domains so
  `www` vs bare-host variants are never treated as external.
- `T4-033`/`T4-034` only fire when JSON-LD blocks exist.

## Example Finding Categories

- A URL that returns 404/500 or times out, reported as `T4-001` (one per failed page).
- A page delivered as a non-HTML asset such as a PDF or image, reported via `T4-002`.
- A robots meta or `X-Robots-Tag` carrying `noindex`, reported via `T4-003`.
- A page with no canonical, or a canonical that resolves to another host, reported via
  `T4-020`/`T4-023`.
- JSON-LD blocks missing `@context` or `@type`, or failing to parse, reported via
  `T4-033`/`T4-034`/`T4-035`.

## Generalization

Provider-neutral and industry-neutral: indexability, robots exposure, canonical declaration,
and structured-data completeness are generic concerns in every vertical and are read directly
from the shared, provider-agnostic `PageObservation`. No website, CMS, or platform is
hard-coded.

## Limitations

- Operates on the single fetched `PageObservation`; it does not consult `robots.txt` or
  sitemaps (those are not part of the observation contract) and does not render JavaScript.
- It reports whether a page *could deliver* indexability/structured-data signals, not whether
  a particular search engine or agent honors them.
- Canonical and JSON-LD findings are evaluated per page; cross-page duplication is left to the
  orchestrator's aggregation layer.