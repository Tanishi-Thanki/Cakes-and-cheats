---
name: Crawl / Render Audit
status: available
description: Evaluates whether technical delivery, accessibility, or rendering characteristics prevent automated agents and AI crawlers from reliably accessing and extracting website content from the delivered representation. Consumes the shared PageObservation; emits Finding objects.
---

# Crawl / Render Audit

> **STATUS: AVAILABLE — IMPLEMENTED**
>
> Implemented in `src/audits/crawl_render.py` as `audit_crawl_render(obs) -> list[Finding]`
> and registered in the orchestrator under the stable name `crawl_render` as part of the
> default six-audit pipeline (callers may also explicitly select a subset via
> `AuditOptions.audits` / `AuditRequest.audits`).

## Purpose

Answers the question: *"Is there evidence that important website content may be difficult for
an AI crawler/agent to discover, access, or extract from the delivered representation?"*

It is **not a generic SEO checker** — it focuses on genuine discovery/access/rendering barriers
for automated consumers, using only evidence available in the shared `PageObservation`.

## Problem Detected

- Pages that are unreachable or delivered with access failures (4xx/5xx, network/connection errors).
- Initial HTML containing little extractable content despite meaningful structural shell
  (headings/links/images), indicating substantive content may depend on client-side execution.
- Redirects that downgrade a secure `https` request to plaintext `http`.
- Invalid or malformed canonical declarations that misdirect authority.

## Inputs

A single `PageObservation` (URL, final URL, status code, success flag, error message,
canonical URL, headings, visible text, links, images). Consumes observation only — **no
re-fetch, no raw HTML, no rendered DOM, no browser automation**.

## Output / Finding Contract

Returns a list of `Finding` objects conforming to the shared contract
(`id`, `title`, `severity`, `evidence`, `suggested_action`, `priority`, `confidence`, `details`).

## Detection Philosophy

- Evidence before inference: only observed delivery/accessibility facts become findings.
- Conservative severity: normal redirects, minimal pages, and standard JavaScript usage are
  deliberately **not** flagged.
- Cautious language: the audit never claims "JavaScript makes this page inaccessible" — it
  reports that content may depend on client-side rendering.
- Read-only, deterministic, provider-neutral.

## Implemented Rules

| id | Category | Severity |
|----|----------|----------|
| `CR-001` | Access failure (HTTP 4xx/5xx or connection/timeout errors) | high |
| `CR-002` | Limited initial HTML / client-side render gap (combined structural shell + negligible visible text) | medium |
| `CR-003` | Problematic redirect (secure-to-insecure protocol downgrade) | medium |
| `CR-004` | Invalid/malformed canonical declaration (unsupported scheme or unresolvable) | medium |

Rule guardrails:

- `CR-001` short-circuits: if the page failed to deliver, content-level checks are skipped.
- `CR-002` does **not** fire when visible text is sufficient (>= 25 words) and does **not** fire
  on intentionally minimal navigation/legal pages.
- `CR-003` permits normal upgrades (`http -> https`) and standard path redirects to valid pages.
- `CR-004` never fires on a missing canonical (absence alone is not a failure).

## Example Finding Categories

- Requests that return 404/500 or time out, blocking any content extraction.
- A delivered HTML shell with loading-placeholder phrasing, many links and headings, but almost
  no visible words — substantive content likely depends on client-side execution.
- An `https://` URL redirected to an `http://` destination.
- A `<link rel="canonical">` pointing at a `javascript:` or otherwise invalid URL.

## Generalization

Industry-neutral: reachability, protocol delivery, structural shell detection, and canonical
validity are generic concerns present in every vertical. No website, industry, or provider is
hard-coded.

## Limitations

- Operates on the single fetched `PageObservation`; it does not execute JavaScript or render
  screenshots (browser automation is out of scope by design).
- `CR-002` is a heuristic signal of potential client-side dependence, not proof that content
  is inaccessible after rendering.
- `CR-003`/`CR-004` cover the most consequential redirect/canonical failures, not every
  possible invalid variant.