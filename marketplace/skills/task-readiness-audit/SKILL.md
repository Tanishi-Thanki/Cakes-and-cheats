---
name: Task Readiness Audit
description: Evaluates whether a page structurally supports an agent actually completing a task — navigable actions, actionable endpoints, structured workflows, reachable authentication, and sufficient decision evidence. Driver of the DECIDE/RECOMMEND -> ARRIVE stages.
---

# Task Readiness Audit

## Purpose

Determines whether a page supports **task execution**, not just reading. It checks whether an
automated agent can navigate to an action, find an endpoint, follow a workflow, get through
authentication, and reach enough evidence to make a decision.

## Problem Detected

Pages that look fine to a human but present structural obstacles or missing affordances for an
agent attempting to complete a task.

## Inputs

A single `PageObservation` (title, headings, visible text, links, tables, JSON-LD, signals).
Consumes observation only.

## Output / Finding Contract

Returns a list of `Finding` objects with `id`, `title`, `severity`, `evidence`,
`suggested_action`, `priority`, `confidence`, and `details`.

## Detection Philosophy

- **Task Readiness != Schema Completeness**: rich structured data does not rescue an
  unstructured or non-navigable experience.
- No single signal is sufficient; findings require combined structural evidence.
- Deterministic, read-only, conservative.

## Implemented Rules

| id | Category |
|----|----------|
| `TR-INT-001` | Critical action affordance is non-navigable (`#` or `javascript:` hrefs) |
| `TR-FORM-001` | Advertised capability lacks an actionable endpoint, navigation, or guidance |
| `TR-PROC-001` | Multi-step workflow described in text but unstructured with no step links |
| `TR-AUTH-001` | Mandatory authentication barrier with no navigable login entry point |
| `TR-DATA-001` | Transactional page with decision-evidence deficit across all channels |

## Example Finding Categories

- "Add to cart" / "Checkout" links that are JavaScript-only or dead-end `#` links.
- Pages advertising search/booking/quote capabilities with no reachable endpoint.
- Multi-step processes buried in paragraphs rather than structured steps.
- Gated pages that never expose a login or account link.
- Product/service pages missing price, specs, or description support for decisions.

## Generalization

Provider-neutral and industry-neutral. Triggers are generic task primitives
(action verbs, workflow indicators, authentication language, transactional evidence).
Applies to shopping, booking, finance, services, government, and any task-oriented site.

## Limitations

- Page-level only; it does not crawl through a flow or verify a form submit target against a live server.
- It does not assert that actions definitely cannot be automated — only that the
  structural pathway is weak or missing.
- Complex client-rendered interactivity is not executed.