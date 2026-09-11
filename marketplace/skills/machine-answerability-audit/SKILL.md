---
name: Machine Answerability Audit
description: Evaluates whether key website information can be found, read, and understood reliably by AI systems, based on observed structure, visible text, images, and structured metadata. Driver of the FIND -> READ -> UNDERSTAND stages.
---

# Machine Answerability Audit

## Purpose

Determines whether an automated agent can reliably extract and understand the important
information on a page. The focus is **extraction and understanding**, not aesthetics or SEO.

## Problem Detected

Pages that hide information in non-textual or inconsistent representations, making it
harder for an agent to find and read the answer to a question.

## Inputs

A single `PageObservation` (title, headings, visible text, images, links, tables, JSON-LD,
signals). Consumes observation only — never re-fetches or re-parses.

## Output / Finding Contract

Returns a list of `Finding` objects. Each has `id`, `title`, `severity`, `evidence`,
`suggested_action`, `priority`, `confidence`, and `details`.

## Detection Philosophy

- **Evidence before inference**: no finding without concrete observed facts.
- Deterministic checks only; no external models or APIs.
- Conservative severity to avoid false positives on legitimate minimal pages.

## Implemented Rules

| id | Category |
|----|----------|
| `MA-IMG-001` | Important content may be image-dependent (unhelpful alt text, minimal text) |
| `MA-TXT-001` | Very low textual representation on a structurally substantial page |
| `MA-REP-001` | Conflicting structured-vs-visible price representations |
| `MA-REP-002` | Conflicting structured-vs-visible availability representations |
| `MA-CTX-001` | Structured entity declared with no supporting visible context |

## Example Finding Categories

- Informational images without descriptive alt text on content-light pages.
- Pages with strong layout structure but almost no readable text.
- Price or stock stated one way in JSON-LD and another way in visible text.
- A product/entity declared in structured data that never appears in headings or body text.

## Generalization

Provider-neutral and industry-neutral: it inspects generic structural signals
(alt text, text density, JSON-LD vs. visible text, entity presence). It applies to
e-commerce, services, publishing, health, education, and any other domain. Nothing is
hard-coded to a particular website, CMS, or platform.

## Limitations

- Single-page analysis; it does not check render-time JavaScript execution.
- Rewriting or phrased conflicts are not detected — only explicit, deterministic
  mismatches are reported.
- It flags *risk* of reduced machine extraction, not proof of extraction failure.