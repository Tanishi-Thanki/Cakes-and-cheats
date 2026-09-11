# Machine Answerability Audit Skills — Marketplace

A provider-neutral, deterministic **agent-skill marketplace** for evaluating whether an AI
agent can effectively complete a task on any given website. The marketplace composes modular
audit skills through a single orchestrator entrypoint and produces one structured JSON report.

## What This Marketplace Does

It answers, with evidence, a focused question:

> Can an automated agent **FIND → READ → UNDERSTAND → TRUST → DECIDE/RECOMMEND → ARRIVE →
> CONTINUE THE TASK** on this page?

Each audit skill addresses part of that journey, and the orchestrator composes their findings
into a single evidence-backed report. The output is structured JSON — designed to be consumed
by an agent, an evaluation harness, or a marketplace runtime — never a dashboard or PDF.

## Why This Is Different From Traditional SEO Auditing

This is **not**:

- SEO scoring or keyword auditing
- Page-speed / performance auditing
- Generic website linting or HTML validation
- A "health score" that measures how a site looks to a search engine

Traditional SEO tools measure discoverability and ranking signals. This marketplace measures
**task executability by an AI agent**: whether the information can be extracted, understood,
trusted, acted upon, and continued. The currency is evidence and structure, not rankings or
keywords.

## High-Level Architecture

```
URL / AuditRequest
  ↓
Crawler            one read-only HTTP GET
  ↓
Observation        one deterministic HTML parse (no network)
  ↓
Audit Orchestrator (entrypoint)
  ├─ machine-answerability-audit      FIND / READ / UNDERSTAND
  ├─ task-readiness-audit             DECIDE / ARRIVE
  ├─ engagement-audit                 CONTINUE THE TASK
  └─ freshness-corroboration          TRUST
  ↓
Aggregation        conservative dedup + priority ordering (source-tagged)
  ↓
Reporting         final structured JSON report
```

## Skill Decomposition

| Skill | Stage | Responsibility |
|-------|-------|----------------|
| `audit-orchestrator` | entrypoint | Composition, aggregation, priority ordering, reporting |
| `machine-answerability-audit` | FIND / READ / UNDERSTAND | Can key information be extracted and understood? |
| `task-readiness-audit` | DECIDE / ARRIVE | Can the task actually be executed? |
| `engagement-audit` | CONTINUE THE TASK | Does the page orient and continue the referred visitor? |
| `freshness-corroboration` | TRUST | Are representations consistent and time-sensitive facts current? |
| `crawl-render-audit` | FIND / READ / TRUST | Reachability, protocol delivery, and client-side render accessibility |

Each audit skill is a thin, self-contained specification consuming the same `PageObservation`
and emitting `Finding` objects. Skills do not depend on one another.

## Entrypoint Composition

The marketplace has exactly **one entrypoint**: `audit-orchestrator`. It:

- Fetches each URL once (read-only) and observes it once.
- Runs every registered audit against that shared observation.
- Source-tags, conservatively deduplicates, and orders findings by audit-supplied priority/severity.
- Emits the final report.

New audits plug in by registering a conforming callable in the orchestrator — no pipeline
or reporting changes required. The skill list therefore reflects what is actually registered.

## Final Report Concept

The final report is a deterministic projection of existing structured data:

- `site`, `audited_at` (caller-supplied only)
- `summary` with `total_findings` and per-severity counts (high / medium / low, plus
  critical when present)
- `findings` with title, severity, **full evidence**, priority, confidence, source audit,
  `suggested_action`, and structured `details`
- structured `failure` / `audit_errors` when the pipeline or an audit fails

Evidence is never truncated or collapsed into an opaque string. Severity and priority are
the audits' own values — the report never invents them.

## How the Skills Work Together

1. Crawl and observation produce one shared, deterministic view of the page.
2. Each audit evaluates a different capability of that page for an automated agent.
3. Findings are combined without loss; duplicates are collapsed conservatively.
4. Priority ordering surfaces the most important findings first.
5. The report is the single, JSON-serializable contract downstream consumers use.

## Generalization Across Industries

All signals are domain-neutral structural facts: headings, visible text, links, images,
tables, structured data, URLs, and dates. Nothing is hard-coded to a particular industry,
website, CMS, or e-commerce platform. The same skills evaluate shopping, services,
publishing, health, education, government, and any other site.

## No External AI / API Dependency

The entire pipeline is deterministic and offline:

- Zero LLM calls, zero external AI/ML APIs, zero analytics platforms.
- No browser automation, no database, no persistence.
- Dependencies are limited to `beautifulsoup4` (HTML parsing) and `requests` (one read-only GET).

Same URL + same HTML → same findings → same report.

## Evaluation / Runtime Philosophy

- **Evidence before inference**: a finding exists only when concrete observed facts support it.
- **Conservative**: prefer no finding over a speculative one; never pretend an audit succeeded
  when it failed.
- **Structured failures**: concise diagnostic blocks, never stack traces in the consumer contract.
- **Provider-neutral and user-respecting**: read-only at all times, no writes to audited sites.

## Entry Points

- Manifest: `marketplace.json` (single entrypoint = `audit-orchestrator`)
- Orchestrator skill: `skills/audit-orchestrator/SKILL.md`
- Python entry point: `src.orchestrator.run_audit_full` → `(AuditResult, AuditReport)`
- Report serialization: `AuditReport.as_final_dict()` / `report_to_json(...)`