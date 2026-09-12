---
name: freshness-corroboration
description: Evaluates whether the facts on a page are internally consistent and whether available freshness signals give an agent reason to treat time-sensitive facts as current. Driver of the TRUST stage.
---

# Freshness & Corroboration Audit

## Purpose

Assesses two dimensions that build **trusted** answers:

1. **Corroboration** — do the page's representations (structured data, visible text,
   tables, headings) agree with each other?
2. **Freshness** — where facts are time-sensitive, is there explicit evidence that they
   are current?

## Problem Detected

Internal contradictions between structured data and visible content, ambiguous entity or
brand identity, and time-sensitive facts carrying only stale timestamps (limited recency
evidence).

## Inputs

A single `PageObservation` (visible text, tables, JSON-LD, headings). Consumes observation only.

## Output / Finding Contract

Returns a list of `Finding` objects with `id`, `title`, `severity`, `evidence`,
`suggested_action`, `priority`, `confidence`, and `details`.

## Detection Philosophy

- **Single-page internal corroboration**: consistency evaluated within the observed page.
- **Evidence before inference**: reports only deterministic, demonstrable discrepancies.
- **Conservative severity**: normal minimal pages and historical articles are not penalized.
  An old timestamp is treated as *limited freshness evidence*, never as definitive proof of staleness.

## Implemented Rules

| id | Category |
|----|----------|
| `FC-REP-001` | Conflicting visible vs. structured price representations |
| `FC-REP-002` | Conflicting visible vs. structured availability representations |
| `FC-ATTR-001` | Contradictory product attribute specifications (structured vs. table) |
| `FC-ENT-001` | Conflicting entity identity signals (headline vs. structured name) |
| `FC-ENT-002` | Conflicting brand representations (visible vs. structured) |
| `FC-FRSH-001` | Limited freshness evidence for time-sensitive content |

## Example Finding Categories

- A price shown in visible text that contradicts the JSON-LD price.
- Product specifications in a spec table that contradict structured attributes.
- A heading naming one product while structured data declares another name or brand.
- Transactional pages carrying timestamps more than three years old with no newer signal.

## Generalization

Provider-neutral and industry-neutral. It works on generic consistency signals (price,
availability, attributes, entity name, brand, dates) that exist across shopping, services,
directory, publishing, and information sites.

## Limitations

- Freshness is about *explicit evidence*, not ground truth: it cannot verify today's
  actual price or stock against a live source.
- Attribute comparison uses deterministic normalization; heavily paraphrased or
  alternative-unit conflicts may not be flagged.
- The default freshness reference year is present-day aligned (configurable at the
  API boundary), keeping behavior deterministic within a fixed reference.