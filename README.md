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
Audit Orchestrator  run_audit() / run_audit_full()  (single entrypoint)
  ↓
Crawl               bounded, same-site BFS (page + depth limits, robots.txt,
                    sitemap discovery) — one read-only HTTP GET per page
  ↓
Observe             one deterministic HTML parse per crawled page (no network)
  ↓
PageObservation collection (one per page: text, structure, links, metadata)
  ↓
Specialist Audits   seven default audits, run per-page observation
  ├─ machine_answerability      FIND / READ / UNDERSTAND
  ├─ task_readiness             DECIDE / ARRIVE
  ├─ crawl_render               FIND / READ / TRUST
  ├─ engagement                 CONTINUE THE TASK
  ├─ evidence_consistency       EVIDENCE / TRUST
  ├─ freshness_corroboration    TRUST
  └─ t4_indexability            INDEXABILITY / REACHABILITY
  ↓
Multi-page layers   always run on the complete observation collection
  ├─ p2_evidence                cross-page evidence contradictions
  └─ entity_identity            entity identity (MATCH / CONFLICT / AMBIGUITY)
  ↓
Finding Aggregation conservative deduplication + deterministic ID hardening
  ↓
Objective Mapping   maps findings to task objectives & relevance
  ↓
Priority Engine     task-aware prioritization & transparent reasoning
  ↓
Final Report        unified structured JSON report
```

## Default Audit Set

`run_audit` and `run_audit_full` default to **seven specialist audits**
(`_DEFAULT_AUDITS` in the orchestrator registry). Each audit consumes a single
`PageObservation` and emits `Finding` objects; findings are tagged with their
source audit before aggregation.

| Registry name | Stage | Responsibility |
|---------------|-------|----------------|
| `machine_answerability` | FIND / READ / UNDERSTAND | Can key information be extracted and understood? |
| `task_readiness` | DECIDE / ARRIVE | Can the task actually be executed? |
| `crawl_render` | FIND / READ / TRUST | Reachability, protocol delivery, and client-side render accessibility |
| `engagement` | CONTINUE THE TASK | Does the page orient and continue the referred visitor? |
| `evidence_consistency` | EVIDENCE / TRUST | Same-context evidence/provenance classification (MATCH / CONFLICT / HISTORICAL / AMBIGUOUS / INSUFFICIENT_EVIDENCE) behind trustworthy facts |
| `freshness_corroboration` | TRUST | Are representations consistent and time-sensitive facts current? |
| `t4_indexability` | INDEXABILITY | Technical delivery barriers (redirects, robots/X-Robots-Tag, canonical conflicts, XML sitemap issues) |

Explicit audit selection (via `AuditRequest.audits` or `AuditOptions.audits`) overrides the default set.

In addition, two **always-on, multi-page analysis layers** run on the complete
observation collection of every orchestrated run. They are not per-page specialist
audits and are not listed in `audits_run`:

- **`p2_evidence`** — cross-page evidence aggregation. Only a genuine value contradiction
  (`CONFLICT`) produces a finding; uncertain or missing evidence never does.
- **`entity_identity`** — deterministic, multi-signal entity-identity analysis across the
  crawled pages.

## Skill Decomposition

The marketplace packages the audits as reusable Agent Skills (see `marketplace.json`).
Each audit skill is a thin, self-contained specification consuming the shared
`PageObservation` and emitting `Finding` objects. Skills do not depend on one another.
New audits plug in by registering a conforming callable in the orchestrator registry —
no pipeline or reporting changes required.

| Skill | Responsibility |
|-------|----------------|
| `audit-orchestrator` (entrypoint) | Composition, aggregation, objective mapping, priority engine, reporting |
| `machine-answerability-audit` | FIND / READ / UNDERSTAND — can key information be extracted and understood? |
| `task-readiness-audit` | DECIDE / ARRIVE — can the task actually be executed? |
| `crawl-render-audit` | FIND / READ / TRUST — reachability, protocol delivery, and client-side render accessibility |
| `engagement-audit` | CONTINUE THE TASK — orientation and task-continuation affordances |
| `evidence-consistency-audit` | EVIDENCE / TRUST — shared evidence/provenance classification behind trustworthy facts |
| `freshness-corroboration` | TRUST — representation consistency and currency of time-sensitive facts |

## Entrypoint Composition

The marketplace has exactly **one designated entrypoint**: the `audit-orchestrator`
skill, implemented by `run_audit() / run_audit_full()` in the bundled runtime and the
`run_audit.py` CLI. It:

- Receives the audit request with optional task context (`task_intent`, `task_description`, `target_objectives`).
- Runs a **bounded, same-site crawl** from the target URL (page limit, depth limit,
  per-request timeout, robots.txt, sitemap discovery) and observes each fetched page once.
- Runs the seven default specialist audits per crawled page observation (or an explicitly requested subset).
- Runs the two always-on multi-page layers (P2 evidence, entity identity) on the full observation collection.
- Conservatively deduplicates findings and attributes each to its source audit.
- Executes **Objective Mapping** to link findings to affected user/task objectives.
- Executes the **Priority Engine** to provide task-aware ranking and actionable priority reasons.
- Emits the unified final report.

## Bounded Crawling / Site-Aware Behavior

The system is **not** a single-page audit and **not** an unlimited or full-web crawler.
`crawl_site` performs a deterministic, breadth-first crawl of a target site:

- **Same-site scope only**: only http(s) URLs sharing the site's host are followed;
  external and unsupported schemes are ignored.
- **Hard bounds**: `max_pages` (default 50 distinct URLs) and `max_depth` (default 3
  link hops from the start URL). Discovery never exceeds these budgets.
- **robots.txt**: robots.txt is fetched and honored before requests (enable/disable
  via `AuditOptions.check_robots`).
- **Sitemap discovery**: robots.txt `Sitemap:` records and the conventional
  `/sitemap.xml` seed the crawl at depth 1, still within the same page/depth budget.
- **Per-request timeout** and a single read-only HTTP GET per page; no browser
  automation, no JavaScript execution.
- **Structured failures**: fetch failures are categorized per page
  (`invalid_url`, `robots_denied`, `dns_error`, `connection_error`, `timeout`,
  `ssl_error`, `too_many_redirects`, `http_error`, `non_html`, `response_too_large`,
  `decode_error`, …). A blocked or bot-protected site fails cleanly and is reported —
  the tool makes no claim that arbitrary websites are always crawlable.

Each successful page produces one `PageObservation`; the crawl result is composed
into a structural `SiteAuditContext` (internal-link graph, per-URL discovery source,
representative-page selection) that multi-page layers consume without re-fetching
or re-parsing.

## Observation Model

`observe_page` extracts deterministic, domain-neutral facts from the fetched HTML:

- **Page facts**: final URL, status code, title, headings (h1/h2/h3), visible text,
  links, images, tables, JSON-LD script blocks, content-type, response headers.
- **Robots directives**: `<meta name="robots">` and `X-Robots-Tag` header values
  (raw + normalized tokens).
- **Social metadata**: Open Graph (`og:title`, `og:description`, `og:type`, `og:url`)
  and Twitter Card (`twitter:title`, `twitter:description`, `twitter:card`).
- **Content components** (`ComponentObservation`): structural blocks (`main`,
  `article`, `section`, `nav`, `header`, `footer`, `form`, lists, pricing/product
  card-like `div`s) with provenance — component kind, source tag, `id`/`class`
  locator, nearest heading, bounded cleaned text, and the page URL they came from.
- **Quantitative signals** (`PageSignals`): presence/count summaries (title, meta
  description, canonical, links, images, tables, headings, components).

Observation is a record of what is on the page; it never judges quality. All
extraction is structural — no hostname, industry, CMS, or platform is special-cased.

## Entity Identity (P3-2)

`entity_identity` analyzes identity signals (domain, phone, email domain, address,
name, category/sameAs) extracted from the crawled pages. It is **deterministic and
offline** — it never uses an external web search, an LLM, or any third-party service.

Classification semantics (match the implementation):

- **MATCH** → no entity-identity finding.
- **CONFLICT** → `ENT-ID-001` (high) — contradicting hard signals with no
  explanatory relationship.
- **AMBIGUOUS** → `ENT-ID-002` (medium) — split evidence, potential ambiguity.
- **INSUFFICIENT_EVIDENCE** → no entity-identity finding.

Name alone is never sufficient for a MATCH; the layer is deliberately conservative
and false-positive-safe for subsidiaries, branches, and harmless formatting variants.

## Evidence Philosophy

The marketplace treats **uncertainty as evidence about the evidence**, not as a
defect. Missing, unknown, or ambiguous information is **not** automatically a
problem:

- Evidence states `MATCH`, `AMBIGUOUS`, `INSUFFICIENT_EVIDENCE`, and the temporal
  states (`HISTORICAL`, `EXPIRED`, `FUTURE`) describe uncertainty about a signal and
  **never** produce a finding by themselves.
- **Only a genuine value contradiction** (`CONFLICT`) produces a finding. For
  cross-page evidence, the P2 layer emits `EVID-001`; for same-page
  cross-representation conflicts, `evidence_consistency` emits
  `EVID-PRC-001` / `EVID-AVT-001` / `EVID-ENT-001` / `EVID-ATT-001`.
- Legitimate variants (sale vs. original prices, price ranges, "from" prices,
  different currencies) are resolved as `AMBIGUOUS`, never fabricated as conflicts.

## Evidence Aggregation and Attribution

Evidence is carried on every finding as the `evidence` string plus structured
`details`, and both survive aggregation and reporting unchanged (nothing is
truncated, paraphrased, or collapsed into an opaque string). The shared provenance
primitive (`EvidenceUnit` / `FactInventory` / `SiteEvidenceInventory`) records
for each fact: entity, attribute, normalized value, raw text, source URL, source
type, confidence, and temporal qualifier. There is no separate "citation" system
and no external source verification — attribution is limited to what the local
audit observed.

## Finding Aggregation (Finding IDs)

Aggregation (`aggregate_findings`) runs after all audits in three deterministic steps:

1. **Deduplicate**: two findings collapse only when the rule id AND the evidence
   (up to whitespace) are identical.
2. **Harden IDs**: every finding receives a deterministic, report-unique ID
   (P3-4). A rule ID that occurs once keeps its original form; when the same rule
   fires more than once in one report, each instance becomes `<RULE-ID>-<12-hex
   digest>` (e.g. `T4-024-7d571a52cd37`), where the digest is a SHA-256 over the
   rule id and the finding's own canonical evidence. IDs never depend on
   generation order, `id()`, Python hash randomization, time, or randomness, and
   they are stable across separate processes.
3. **Sort**: by audit-supplied `priority` (ascending) then severity (critical →
   low).

Single-instance rule conventions are preserved verbatim, including
`ENT-ID-001`, `ENT-ID-002`, `EVID-001`, and all `T4-*` / `MA-*` / `TR-*` /
`FC-*` / `CR-*` / `ENG-*` rule IDs.

## Objective Mapping

`map_findings_to_objectives` links findings to canonical objectives
(`transaction_completion`, `price_accuracy`, `price_evaluation`,
`inventory_verification`, `entity_identity`, `information_sufficiency`, …)
using a deterministic rule-to-objective taxonomy plus the caller-supplied
`target_objectives` and `task_intent` keywords. When no task context is
supplied, findings remain **general** with an empty `affected_objectives` list.
Objective mapping only interprets relevance; it never creates or re-scopes
findings.

## Priority and Severity

Severity and priority are the audits' own values — the pipeline never invents,
rewrites, or reinterprets them. The orchestrator orders findings by
`priority` then `severity`. When task context is present, the Priority Engine
ranks findings by task relevance (direct blocker → task-impairing → general),
then audit priority, severity, confidence, and finally a stable ID tie-break,
with a transparent, evidence-based `priority_reason`.

## Recommendations

The only recommendation artifact is the per-finding `suggested_action`
(a `summary` string plus a `priority`), produced by the audits themselves and
preserved verbatim into the report. There is no separate recommendation engine.

## Final Report Concept

The final report is a deterministic projection of structured data:

- `site`, `audited_at` (caller-supplied only — never auto-generated)
- `summary` with `total_findings` and per-severity counts (high / medium / low,
  plus critical when present)
- `findings` with id, title, severity, **full evidence**, priority, confidence,
  source audit, `suggested_action` (summary + priority), and structured `details`
- `task_context`, `affected_objectives`, `task_relevance`, and `priority_reason`
  when task context is provided
- structured `failure` / `audit_errors` when the pipeline or an individual audit fails

`AuditReport.as_final_dict()` / `report_to_json()` emit sorted keys and no
wall-clock data. Findings/evidence/objectives/priority/severity reach the final
report exactly as the aggregation and downstream stages produced them.

## How the Skills Work Together

1. **Crawl & Observe**: a bounded, same-site crawl (robots.txt, sitemap discovery,
   page/depth limits) produces one deterministic observation per page.
2. **Specialist Evaluation**: the seven default specialist audits evaluate each
   page observation; the two multi-page layers evaluate the full collection.
3. **Aggregation**: findings are combined without loss, deduplicated conservatively,
   and given deterministic, report-unique IDs.
4. **Objective Mapping**: findings are mapped to canonical objectives and
   task-specific intents.
5. **Priority Engine**: findings are prioritized by task relevance with transparent
   rationales, using audit-supplied values.
6. **Unified Report**: the final report is emitted as the single, JSON-serializable
   contract for downstream consumers.

## Generalization Across Industries

All signals are domain-neutral structural facts: headings, visible text, links, images,
tables, structured data, URLs, and dates. Nothing is hard-coded to a particular industry,
website, CMS, or e-commerce platform. The same skills evaluate shopping, services,
publishing, health, education, government, and any other site.

## Determinism / Read-Only / Provider Neutrality

The pipeline is deterministic for identical inputs and read-only with respect to
audited sites:

- Zero LLM calls, zero external AI/ML APIs, zero analytics platforms.
- No browser automation, no database, no persistence.
- Dependencies are limited to `beautifulsoup4` (HTML parsing) and `requests`
  (read-only HTTP GETs during the bounded crawl).
- No writes to audited sites are ever made.

Same URL + same HTML → same findings → same report. Because external network
responses (redirects, CDN variation, bot protection) can naturally vary, exact
reproducibility is guaranteed for identical inputs, not for the live web.

## Evaluation / Runtime Philosophy

- **Evidence before inference**: a finding exists only when concrete observed facts support it.
- **Conservative**: prefer no finding over a speculative one; never pretend an audit succeeded
  when it failed.
- **Structured failures**: concise diagnostic blocks, never stack traces in the consumer contract.
- **Provider-neutral and user-respecting**: read-only at all times, no writes to audited sites.

## Entry Points

- Manifest: `marketplace.json` (single entrypoint = `audit-orchestrator`)
- Orchestrator skill: `skills/audit-orchestrator/SKILL.md`
- CLI Entrypoint: `python skills/audit-orchestrator/scripts/run_audit.py <URL>` — see
  `--help` for `--intent`, `--description`, `--objectives`, `--audits`, `--timeout`,
  `--user-agent`, `--no-robots`, `--details`, `--output`
- Bundled Runtime: `skills/audit-orchestrator/scripts/runtime/src/`
- Report serialization: `AuditReport.as_final_dict()` / `report_to_json(...)`