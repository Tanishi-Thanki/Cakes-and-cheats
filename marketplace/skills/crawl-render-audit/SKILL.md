---
name: Crawl / Render Audit
status: planned
description: PLACEHOLDER - PENDING IMPLEMENTATION. Intended to evaluate whether a page is reachable and accessible to automated agents and whether its content survives client-side rendering. Not yet implemented. Do NOT invoke.
---

# Crawl / Render Audit

> **STATUS: PENDING — NOT IMPLEMENTED**
>
> This skill is a **placeholder for the planned Crawl / Render Audit**. At the time of
> writing it is **not implemented** and is **not registered** in the audit orchestrator.
> It must **not be invoked** and must **not be presented as available**.

## Intended Purpose

When implemented, this skill is expected to evaluate:

- **Reachability**: whether the URL is fetched successfully (HTTP status, HTML content type,
  redirect resolution, error messages).
- **Accessibility to agents**: non-HTML responses, blocked/broken fetches, and pathway errors.
- **Client-side rendering**: whether substantive content appears to require JavaScript execution
  to be available to automated consumers.

## Current Foundation (exists, but this is NOT the audit itself)

The read-only crawler (`src/crawler/crawler.py`) already provides:

- A single, read-only HTTP GET with redirects.
- Structured `FetchedPage` results: URL, final URL, status code, content type, HTML,
  success flag, and a concise error message.
- Failure modes modeled as data (timeout, SSL, connection, HTTP status, non-HTML content),
  not exceptions exposed to consumers.

The observation layer (`src/observation/observation.py`) parses the fetched HTML into a
deterministic `PageObservation`.

## Not Yet Implemented / Planned

- A `crawl/render` audit callable conforming to `audit_crawl_render(obs) -> list[Finding]`.
- Rule ids and finding categories for crawl/render accessibility.
- Registration in the orchestrator registry and default audit set.

## Contract for the Future Implementer

When Person 1 supplies the Crawl / Render Audit, it should:

1. Produce `Finding` objects conforming to the existing contract
   (`id`, `title`, `severity`, `evidence`, `suggested_action`, `priority`, `confidence`, `details`).
2. Consume the shared `PageObservation` (no re-fetch, no re-parse).
3. Register itself in the orchestrator audit registry under a stable name.
4. Preserve the read-only, deterministic, provider-neutral principles of the marketplace.

Nothing else in the pipeline, aggregation, or reporting needs to change.