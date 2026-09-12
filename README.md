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
Audit Request (URL + optional task context)
  ↓
Audit Orchestrator (entrypoint)
  ↓
Fetch              one read-only HTTP GET (respects robots.txt)
  ↓
Observe            one deterministic HTML parse (no network)
  ↓
Specialist Audits  (run against the shared observation)
  ├─ machine-answerability-audit      FIND / READ / UNDERSTAND
  ├─ task-readiness-audit             DECIDE / ARRIVE
  ├─ crawl-render-audit               FIND / READ / TRUST
  ├─ engagement-audit                 CONTINUE THE TASK
  ├─ evidence-consistency-audit       EVIDENCE / TRUST
  └─ freshness-corroboration          TRUST
  ↓
Finding Aggregation conservative deduplication & source attribution
  ↓
Objective Mapping  maps findings to task objectives & relevance
  ↓
Priority Engine    task-aware prioritization & transparent reasoning
  ↓
Final Report       unified structured JSON report
```

## Skill Decomposition

| Skill | Stage | Responsibility |
|-------|-------|----------------|
| `audit-orchestrator` | entrypoint | Composition, aggregation, objective mapping, priority engine, reporting |
| `machine-answerability-audit` | FIND / READ / UNDERSTAND | Can key information be extracted and understood? |
| `task-readiness-audit` | DECIDE / ARRIVE | Can the task actually be executed? |
| `crawl-render-audit` | FIND / READ / TRUST | Reachability, protocol delivery, and client-side render accessibility |
| `engagement-audit` | CONTINUE THE TASK | Does the page orient and continue the referred visitor? |
| `evidence-consistency-audit` | EVIDENCE / TRUST | Shared evidence/provenance classification (MATCH / CONFLICT / AMBIGUOUS) behind trustworthy facts |
| `freshness-corroboration` | TRUST | Are representations consistent and time-sensitive facts current? |

Each audit skill is a thin, self-contained specification consuming the same `PageObservation`
and emitting `Finding` objects. Skills do not depend on one another.

## Entrypoint Composition

The marketplace has exactly **one designated entrypoint**: `audit-orchestrator`. It:

- Receives the audit request with optional task context (`task_intent`, `task_description`, `target_objectives`).
- Fetches each URL once (read-only, respecting `robots.txt`) and observes it once.
- Runs all six specialist audits against that single shared observation (or an explicitly requested subset).
- Conservatively deduplicates findings and attributes each to its source audit.
- Executes **Objective Mapping** to link findings to affected user/task objectives.
- Executes the **Priority Engine** to provide task-aware ranking and actionable priority reasons.
- Emits the unified final report.

New audits plug in by registering a conforming callable in the orchestrator registry — no pipeline
or reporting changes required.

## Final Report Concept

The final report is a deterministic projection of structured data:

- `site`, `audited_at` (caller-supplied only)
- `summary` with `total_findings` and per-severity counts (high / medium / low, plus
  critical when present)
- `findings` with id, title, severity, **full evidence**, priority, confidence, source audit,
  `suggested_action` (summary + priority), and structured `details`
- `task_context`, `affected_objectives`, `task_relevance`, and `priority_reason` when task context is provided
- structured `failure` / `audit_errors` when the pipeline or an individual audit fails

Evidence is never truncated or collapsed into an opaque string. Severity and priority are
the audits' own values — the report never invents them.

## How the Skills Work Together

1. **Fetch & Observe**: Crawling respects `robots.txt` and produces one shared, deterministic observation.
2. **Specialist Evaluation**: All six specialist audits evaluate different capabilities of the page using the shared observation.
3. **Aggregation**: Findings are combined without loss and deduplicated conservatively.
4. **Objective Mapping**: Findings are mapped to canonical objectives and task-specific intents.
5. **Priority Engine**: Findings are prioritized by task relevance (direct blockers first) and severity with transparent rationales.
6. **Unified Report**: The final report is emitted as the single, JSON-serializable contract for downstream consumers.

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
- CLI Entrypoint: `python skills/audit-orchestrator/scripts/run_audit.py <URL>`
- Bundled Runtime: `skills/audit-orchestrator/scripts/runtime/src/`
- Report serialization: `AuditReport.as_final_dict()` / `report_to_json(...)`