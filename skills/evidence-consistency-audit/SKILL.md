---
name: evidence-consistency-audit
description: Extracts and preserves provider-neutral evidence facts (price, availability, name, brand, dates, attributes) into a shared FactInventory, classifies each evidence group across representations as MATCH/CONFLICT/HISTORICAL/AMBIGUOUS/INSUFFICIENT_EVIDENCE, and reports only meaningful cross-representation conflicts. Consumes the shared PageObservation; emits Finding objects.
---

# Evidence & Consistency Audit

> **STATUS: AVAILABLE — IMPLEMENTED**
>
> Implemented in the bundled runtime (`skills/audit-orchestrator/scripts/runtime/src/audits/evidence_consistency.py`) as
> `audit_evidence_consistency(obs) -> list[Finding]` and registered in the
> orchestrator under the stable name `evidence_consistency` as part of the
> default seven-audit pipeline (callers may also explicitly select a subset via
> `AuditOptions.audits` / `AuditRequest.audits`).

## Purpose

Answers the question: *"Do the different representations of a fact on this page
agree, and when they disagree, is that a real problem for automated
consumers?"*

It builds the shared, provider-neutral **evidence / provenance layer**: every
fact on the page (prices, availability, names, brands, dates, attributes) is
captured as an `EvidenceUnit` with full provenance (raw text, source URL,
source type, confidence, qualifier, date) and grouped into a `FactInventory`.
Consistency classification then decides whether any disagreement is a genuine
conflict worth reporting.

It is **not** a re-implementation of the existing validity audits: Machine
Answerability (MA-REP-001/002) and Freshness & Corroboration (FC-REP-001/002,
FC-ATTR-001) already detect price/availability/attribute inconsistencies on a
single page. This audit intentionally stays out of their lane (see
Restricted Scope below).

## Problem Detected

- Structured data and visible text disagree about a **price** on the same page
  (e.g. JSON-LD `$949` vs visible `$999`), so an automated consumer cannot
  trust which amount is correct.
- Structured data and visible text disagree about **availability** (e.g.
  JSON-LD `InStock` vs visible "out of stock").
- **Identity** signals disagree — JSON-LD name vs H1/title/brand label claim
  different products.
- **Other attributes** (weight, storage, colour, size, material, model, ...)
  disagree across JSON-LD and table/visible representations.

## Inputs

A single `PageObservation` (URL, final URL, status code, success flag, title,
H1, visible text, JSON-LD blocks, tables, links). Consumes observation
only — **no re-fetch, no network request, no browser automation**. Sleep, sale,
range, and historical variants are preserved as contextual qualifiers, never
invented.

## Output / Finding Contract

Returns a list of `Finding` objects conforming to the shared contract
(`id`, `title`, `severity`, `evidence`, `suggested_action`, `priority`,
`confidence`, `details`). Every finding carries structured `details` with the
full provenance of the conflicting units (`entity`, `attribute`,
`classification`, `evidence_units`).

## Detection Philosophy

- **Evidence before inference**: only observed facts become units; only
  same-context disagreements become findings.
- **Conservative**: uncertainty resolves to `AMBIGUOUS` or
  `INSUFFICIENT_EVIDENCE`, never to a fabricated CONFLICT. Legitimate variants
  (sale/original/"from" prices, price ranges, different currencies) are never
  contradictions.
- **Temporal honesty**: `HISTORICAL` is only concluded across different source
  URLs with a genuinely older dated value.
- **Read-only, deterministic, provider-neutral**, standard library only.
- Price normalization is exact (decimal-accurate, currency-aware) — distinct
  by design from the existing audits' integer-part comparison, which are left
  untouched to preserve their behavior and tests.

## Implemented Rules

| id | Category | Severity |
|----|----------|----------|
| `EVID-PRC-001` | Same-context price conflict across representations | high |
| `EVID-AVT-001` | Same-context availability conflict across representations | high |
| `EVID-ENT-001` | Conflicting entity identity (name/brand) signals | medium |
| `EVID-ATT-001` | Conflicting product attribute representations | medium |

Rule guardrails:

- **Only CONFLICT produces a finding.** `MATCH`, `AMBIGUOUS`, `HISTORICAL`, and
  `INSUFFICIENT_EVIDENCE` never generate findings.
- Fewer than two evidence units, or values that cannot be normalized, resolve to
  `INSUFFICIENT_EVIDENCE`.
- Sale / original / "from" / range-qualified prices resolve to `AMBIGUOUS`.
- Prices in different currencies (or without currency context) resolve to
  `AMBIGUOUS`.
- A scalar price vs a price range (`mixed value kinds`) resolves to `AMBIGUOUS`.
- Identity (name/brand) comparisons use significant-token overlap, so
  "Pegasus 40 Performance Running Shoes" vs "Pegasus 40" is a MATCH, not a
  conflict.

## Relationship to Task Readiness

Ownership is deliberately split:

- **Evidence & Consistency** owns evidence extraction, preservation,
  normalization, classification, and the reusable `FactInventory` primitive.
- **Task Readiness** owns sufficiency and keeps its unchanged contract
  `audit_task_readiness(obs) -> list[Finding]`.
- `FactInventory` is a shared, provider-neutral provenance layer that other
  audits (e.g. Task Readiness) may consume in a documented follow-up. This
  integration connects the audit to the orchestrator, aggregation, objective
  mapping, priority engine, and final report; it does not silently redraw
  audit boundaries.

## Restricted Scope

- Does **not** re-implement Machine Answerability (MA-REP-001/002) or Freshness
  & Corroboration (FC-REP-001/002, FC-ATTR-001) checks. Converging those audits
  onto this caretaker logic is a documented follow-up, not a silent divergence.
- Single-page extraction assigns every unit to one resolved primary entity
  (JSON-LD `name`, else `H1`, else `title`); cross-page merging groups units by
  entity label so different products are never compared.
- No LLM, no external API, no browser, no database, no writes to the audited
  site.

## Example Finding Categories

- JSON-LD `Offer` price `949 USD` while visible text shows "$999" for the same
  product.
- JSON-LD availability `InStock` while visible text states "currently
  unavailable".
- JSON-LD product name "Beta Footwear Ultra" while the H1 reads "Acme Running
  Shoes".
- JSON-LD `weight` "300 g" while the specification table lists "280 grams".

## Generalization

Industry-neutral: price, availability, identity, and attribute consistency are
cross-cutting concerns for every provider. No website, industry, or provider
is hard-coded.

## Limitations

- Operates on a single `PageObservation`; cross-page comparison is prepared by
  the `FactInventory.merge` API and provenance design, not automated here.
- Missing evidence is silence (`INSUFFICIENT_EVIDENCE`), not a finding: the
  audit does not claim negative facts it did not observe.
- Exact decimal price normalization is deliberately stricter than the existing
  audits' integer-part comparison; the audits are kept behavior-identical.