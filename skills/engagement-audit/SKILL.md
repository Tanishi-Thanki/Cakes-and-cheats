---
name: engagement-audit
description: Evaluates post-click orientation and task continuity for users arriving from an AI referral or recommendation — whether the landing page supports orienting the visitor and continuing their intended task. Driver of the CONTINUE THE TASK stage.
---

# Engagement Audit

## Purpose

Assesses whether a page **orients and continues** a visitor who arrives from an AI
recommendation or referral. It checks post-click task continuity using structural evidence
only — no analytics, referral parameters, or external models.

## Problem Detected

Deep landing pages that fail to orient a referred visitor (generic identity, missing
context) or fail to offer task-continuation paths (no relevant action links, no workflow
advancement).

## Inputs

A single `PageObservation` (URL, title, headings, visible text, links, JSON-LD). Consumes
observation only.

## Output / Finding Contract

Returns a list of `Finding` objects with `id`, `title`, `severity`, `evidence`,
`suggested_action`, `priority`, `confidence`, and `details`.

## Detection Philosophy

- Evidence-backed findings: no finding without concrete observed structural facts.
- Conservative false-positive guardrails: prefer no finding when evidence is uncertain.
- Cautious language: problem -> evidence -> mechanism -> suggested action.
- Read-only, deterministic, provider-neutral.

## Implemented Rules

| id | Category |
|----|----------|
| `ENG-001` | Generic landing orientation deficit on a deep page |
| `ENG-002` | Specific landing context mismatch (slug vs. generic fallback) |
| `ENG-003` | Specific task continuation deficit (no continuation affordances) |
| `ENG-004` | Multi-step workflow guidance deficit (no step advancement links) |

## Example Finding Categories

- Deep URLs that render "Home / Welcome" orientation with minimal content and no product schema.
- Deep slugs whose expected context never appears in the visible page.
- Product/service pages with no visible buy, explore, contact, or continuation links.
- Active "Step 1 of 3" workflows with no Next / Continue pathways.

## Generalization

Industry-neutral by design: it operates on generic orientation, context, continuation, and
workflow signals rather than any specific vertical. Informational content (articles, blogs)
is explicitly excluded from several rules to avoid false positives.

## Limitations

- Single-page analysis; it does not follow links or measure real user engagement.
- "Engagement" here means structural affordances for continuing a task — not click or
  session metrics (no analytics dependency).
- Keyword-based continuation detection may miss custom labels that convey the same action.