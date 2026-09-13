# Brand AI Readiness Audit — Deterministic Website Evaluator for AI Agents

> Marketplace: `brand-ai-readiness-audit` v1.0.0 · Agent Skill Marketplace ·
> single entrypoint: `audit-orchestrator` (`"entrypoint": true` in `marketplace.json`)

## The Problem

The web's front door is changing. More and more users reach websites through **AI agents** —
answer engines, copilots, shopping assistants, and automations — that must *read* a site
directly, *judge* it, and *act* on it. A site can be fine-tuned for human search ranking and
still fail agents completely: content embedded in images instead of readable text, prices that
contradict each other across pages, checkout behind a surprise login, key information that only
exists after JavaScript runs.

An agent that cannot reliably find, read, understand, trust, and act on a website will quietly
drop it — no matter how high it ranks. Traditional SEO measures visibility and rankings, not
**executability**, and nothing audits for the agent experience. This marketplace closes that
gap. It answers one focused, evidence-backed question:

> Can an AI agent **FIND → READ → UNDERSTAND → TRUST → DECIDE → ARRIVE → CONTINUE THE TASK**
> on this website?

It answers deterministically, from public HTML alone — no browser, no trainer, no external AI
call — and reports every answer as structured, audit-grade evidence.

## What It Delivers

A provider-neutral, deterministic **Agent Skill Marketplace** that composes seven modular
audit skills behind one entrypoint, runs a bounded read-only crawl of the target site, and
emits **one structured JSON report** covering two dimensions:

| Dimension | Question it answers |
|---|---|
| Off-site — discoverability & answerability | Can an external, AI-driven client find, index, read, and trust this site's content? |
| On-site — engagement & task completion | Can the site orient the visitor and carry a task forward to completion? |

## How to Run This Marketplace (Agentic Harness)

This repository is a genuine Agent Skill Marketplace: the evaluator is given `marketplace.json`
and a target website, and is expected to **use the skills** it declares. There is no hidden
script — everything an agent needs is discoverable:

1. **Inventory the skills** — open `marketplace.json`. It declares **7 skills**, each with an
   `id`, a `path`, and — on exactly one of them (`audit-orchestrator`) — `"entrypoint": true`.

2. **Read the skill contracts** — each skill folder contains a `SKILL.md` that states what the
   skill consumes (`PageObservation`) and produces (`Finding`). Six of the seven are
   declarative specs; the entrypoint skill is the one that executes them.

3. **Invoke the single entrypoint** against the website — via its CLI:

   `python skills/audit-orchestrator/scripts/run_audit.py <website-url>`

   Optionally pass task context so findings are prioritized against the actual job:
   `... --intent "..." --objectives transaction_completion,... --details`

4. **Consume the one JSON report** — it goes to stdout (or a file via `-o`). Exit code `0`
   means the audit ran; `1` means it did not, so a harness can branch on it.

5. **Use the findings** — each finding carries an ID, title, severity, concrete evidence, and
   a suggested action. With task context it also carries affected objectives, task relevance,
   and a transparent priority reason.

The entrypoint runs the entire marketplace in one call — crawl, observation, all seven
specialist audits, cross-page layers, aggregation, and (optionally) objective mapping. No skill
is invoked in isolation; there is exactly one execution path.

## Marketplace Structure

```
marketplace.json                       # skill manifest (name, version, paths, entrypoint flag)
README.md                              # this document
requirements.txt                       # pinned dependencies
scripts/
  validate_marketplace.py              # structural validation of the marketplace itself
skills/
  audit-orchestrator/                  # ENTRYPOINT — the only skill with an executable
    SKILL.md
    scripts/
      run_audit.py                     # CLI entrypoint
      runtime/src/                     # the full engine
        orchestrator.py                # pipeline + internal audit registry
        crawler/                       # crawl_session, crawler, robots_sitemaps,
                                       # site_context, url_utils
        observation/                   # PageObservation model + PageSignals
        audits/                        # seven specialist audits + aggregation layers
  machine-answerability-audit/SKILL.md
  task-readiness-audit/SKILL.md
  crawl-render-audit/SKILL.md
  engagement-audit/SKILL.md
  evidence-consistency-audit/SKILL.md
  freshness-corroboration/SKILL.md     # note: no "-audit" suffix, matching marketplace.json
```

## Tech Stack

Python 3, standard-library-first — the pipeline is deterministic and dependency-light by
design:

| Layer | Technology |
|---|---|
| Language | Python 3 (stdlib-first; no framework, no async runtime) |
| HTTP client | `requests` — read-only GETs only |
| HTML parsing | `beautifulsoup4` — one deterministic parse per page |
| URLs / robots / sitemaps | stdlib: `urllib.parse`, `urllib.robotparser`, `xml.etree.ElementTree` |
| Deterministic IDs & facts | stdlib: `hashlib` (SHA-256 ID hardening), `decimal` (exact price normalization) |
| Reporting | stdlib: `json.dumps` (sorted keys, `ensure_ascii=False`, indent 2) |
| Deliberately absent | browser / JS engine, database, LLM, ML, analytics, external AI APIs |

Dependencies are pinned in `requirements.txt` (`requests`, `beautifulsoup4`, plus their
transitive pins); everything else is the Python standard library.

## Entry Point

**CLI:** `python skills/audit-orchestrator/scripts/run_audit.py <url>`
**Programmatic API:** `run_audit()` / `run_audit_full()` (exported from the bundled runtime)

- JSON report → stdout, or a file via `-o/--output`.
- Exit code `0` on successful audit, `1` otherwise.
- Flags: `--intent/-i`, `--description/-d`, `--objectives`, `--audits`, `--timeout`,
  `--user-agent`, `--no-robots`, `--details`, `--output/-o`, plus the positional target URL
  (or `--url`). See `--help` for full descriptions.

## End-to-End Pipeline

```
Target URL + optional task context (intent / description / objectives)
  → 1  Normalize and validate the URL/request
  → 2  Bounded, same-site, deterministic BFS crawl (robots-aware, sitemap discovery)
  → 3  SiteAuditContext built once from crawl results — no re-fetching, no re-parsing
  → 4  Collection-level layers: cross-page evidence + entity-identity analysis
  → 5  Seven specialist audits run against shared PageObservation objects (isolated errors)
  → 6  Aggregation: tag/source attribution → deduplication → ID disambiguation → stable sort
  → 7  Objective mapping + priority engine (run_audit_full)
  → 8  Unified structured JSON report
```

## How a Skill Works (Shared Contract)

Every audit is a pure, stateless function over one shared input/output contract:

- **Input:** a single `PageObservation` (specialist audits) or the full observation
  collection (cross-page layers).
- **Output:** a list of `Finding` objects (id, title, severity, evidence, suggested_action,
  priority, confidence, source_audit, classification, details).

The orchestrator keeps an **internal audit registry** (`_DEFAULT_AUDITS`) and resolves each
requested audit name against it, running audits with isolated per-audit error handling.
Adding a skill means registering a conforming callable in the registry plus a `SKILL.md` —
no pipeline or reporting changes required.

## Bounded Crawler

The crawler is read-only, strictly same-site over HTTP(S), and deterministic:

| Bound | Default |
|---|---|
| Max pages | 50 distinct URLs |
| Max depth | 3 link hops from the start URL |
| Per-request timeout | 10 s |
| Max response body | 5 MB |
| robots.txt | Checked before each page request (disable with `--no-robots`) |
| Sitemap discovery | Enabled: ≤ 5 sitemap files, depth 3, ≤ 1000 URLs |

- robots.txt is honored before each page request and **fails open** when unavailable.
- URLs beyond the budgets are recorded as unvisited with their reason; discovery sources are
  `start`, `link`, and `sitemap`.
- Canonicalization is deterministic: query parameters preserved, fragments dropped, unsafe
  schemes (`javascript:`, `mailto:`, …) rejected.
- Request budget: one robots.txt GET + one page GET per page; sitemap discovery adds one
  robots.txt GET + up to five sitemap GETs. Worst-case bound derived from the configured
  limits: ≈ **2 × 50 + 6 = 106 GETs per run** (a bound, not a measured benchmark).

## Shared Observation Layer

Each successfully fetched page is parsed exactly once into a `PageObservation` — a
structure-only, domain-neutral record that **captures facts and never judges quality**. It
includes, where present: requested/final URL, HTTP status and success/error, title, meta
description, canonical, H1/H2/H3, visible text, links, images, tables, JSON-LD, robots meta
and X-Robots-Tag, Open Graph and Twitter Card metadata, structural components, content type,
response headers, and derived structural signals.

Extraction is bounded: ≤ 200 components and ≤ 2000 characters of component text.

## The Seven Specialist Audits

Each specialist audit is a dedicated rule module. They are run per page observation; the two
always-on collection-level layers (cross-page evidence, entity identity) are described after.

### `machine_answerability` — FIND / READ / UNDERSTAND

The "can I extract it and understand it" audit. Inspects how information is represented:
visible text volume, image-vs-text dependence, structured (JSON-LD) data, and whether
structured facts have visible page context.

- `MA-IMG-001` — important content may be image-dependent (text buried in images).
- `MA-TXT-001` — a structured page has a low textual representation.
- `MA-CTX-001` — structured information lacks visible page context.

Fires on extraction/understanding risk — the classic "the machine can't read the page."

### `task_readiness` — DECIDE / RECOMMEND → ARRIVE

The "can the task actually get done" audit. Inspects navigation affordances, endpoints
(links/forms/buttons), multi-step flows, authentication barriers, and decision-support
evidence on transactional pages.

- `TR-INT-001` — a critical task action is non-navigable (no valid fallback link).
- `TR-FORM-001` — an advertised capability has no actionable endpoint or navigation support.
- `TR-PROC-001` — a multi-step workflow lacks structural guidance or step navigation.
- `TR-AUTH-001` — a mandatory authentication barrier has no navigable entry point.
- `TR-DATA-001` — a transactional page has a severe decision-evidence deficit.

Fires on "the task is blocked or the path to completion is missing."

### `crawl_render` — access / delivery / render risk

The "will an agent even reach and render the page" audit. Inspects fetch results, initial
HTML payloads, redirect behavior, and canonical declarations.

- `CR-001` — page is not reliably crawlable.
- `CR-002` — limited initial HTML may require client-side rendering.
- `CR-003` — problematic redirect destination / protocol downgrade (e.g. HTTPS → HTTP).
- `CR-004` — problematic canonical declaration.

Client-render issues are inferred heuristically from the raw HTML — there is no browser.

### `engagement` — CONTINUE THE TASK

The "does the page orient and continue the visitor" audit. Inspects structural affordances on
landing pages: orientation context, landing-context agreement, continuation cues, and
multi-step guidance. It is **structural** — it never measures real clicks, dwell time, or
conversion.

- `ENG-001` — generic landing-page orientation deficit.
- `ENG-002` — specific landing context mismatch.
- `ENG-003` — specific task-continuation deficit.
- `ENG-004` — multi-step workflow guidance deficit.

### `evidence_consistency` — EVIDENCE / TRUST

The "do the facts agree across the site" audit. Builds structured facts (price, availability,
entity/brand/name, attributes) with exact decimal price normalization and emits a finding
**only** for a genuine `CONFLICT` between representations.

- `EVID-PRC-001` — conflicting product price declarations.
- `EVID-AVT-001` — conflicting availability representations.
- `EVID-ENT-001` — conflicting entity/brand/name declarations.
- `EVID-ATT-001` — conflicting product attribute representations.

Uncertainty states (`AMBIGUOUS`, `INSUFFICIENT_EVIDENCE`, temporal) never fire on their own.

### `freshness_corroboration` — TRUST

The "is the site internally consistent and current" audit. Corroborates time-sensitive facts
across representations. It evaluates explicit on-site signals only — it does not verify live
ground truth externally.

- `FC-REP-001/002` — conflicting price / availability representations.
- `FC-ATTR-001` — conflicting product attribute representations.
- `FC-ENT-001/002` — conflicting entity identity / brand representations.
- `FC-FRSH-001` — limited freshness evidence for time-sensitive information.

### `t4_indexability` — INDEXABILITY / REACHABILITY

The "can external AI clients index it" audit. Inspects indexability directives (robots meta /
X-Robots-Tag), canonical declarations, and JSON-LD structured data.

- `T4-001/002` — page fetch failed / non-HTML response.
- `T4-003/004/005` — explicit `noindex` / explicit `index` / indexability unspecified.
- `T4-010/011` — robots meta / X-Robots-Tag directives present.
- `T4-020…024` — canonical tag missing, self-referencing, unresolvable, external, or an
  internal alternate URL.
- `T4-030…035` — JSON-LD presence, detected `@type` values, missing `@context`/`@type`,
  malformed JSON-LD blocks.

Sitemaps are crawler-side discovery only — T4 does not analyze sitemap issues, and it does
not analyze redirects generally (that is CR-003).

## Cross-Page Analysis (Always-On Layers)

Two collection-level layers run over the complete crawl on every orchestrated run (they are
per-run, not per-page, and are not listed in `audits_run`):

- **Deterministic site context** — a normalized internal-link graph (nodes, edges,
  incoming/outgoing relationships), external links, discovery sources, crawl statistics,
  bounded unvisited records, and representative-page selection: ≤ 10 pages, deterministic,
  homepage-first, then section/depth/connectivity signals — never random or ML-based.
- **Cross-page evidence** (`SiteEvidenceInventory`) — aggregates facts across pages; only a
  genuine value `CONFLICT` produces a contradiction finding (`EVID-001`). `AMBIGUOUS`,
  `INSUFFICIENT_EVIDENCE`, `HISTORICAL`, `EXPIRED`, and `FUTURE` are records about
  uncertainty, never defects on their own.
- **Entity identity** — deterministic pairwise comparison of identity signals (domain, phone,
  email domain, address, name). `CONFLICT` → `ENT-ID-001` (high); `AMBIGUOUS` → `ENT-ID-002`
  (medium). Weak evidence is never treated as proof, and relationship text (branch,
  subsidiary, franchise) can suppress false conflict classification.

## Finding ID Index

Every finding ID is stable and self-describing: `<AUDIT-PREFIX>-<ITEM>-<NNN>`. Single
occurrences keep the base ID verbatim (e.g. `TR-AUTH-001`); when the same rule fires more
than once in one report, the instance becomes `<RULE-ID>-<12-hex-sha256>` (e.g.
`T4-024-7d571a52cd37`), so IDs are report-unique and reproducible.

| Prefix | Audit / layer | Meaning of the item code |
|---|---|---|
| `MA-*` | machine_answerability | `IMG` image-dependent content · `TXT` low text · `CTX` structured data without visible context |
| `TR-*` | task_readiness | `INT` non-navigable critical action · `FORM` capability w/o endpoint · `PROC` weak multi-step flow · `AUTH` auth barrier w/o entry · `DATA` decision-evidence deficit |
| `CR-*` | crawl_render | `001` not reliably crawlable · `002` client-render risk · `003` bad redirect / protocol downgrade · `004` problematic canonical |
| `ENG-*` | engagement | `001` generic orientation · `002` landing mismatch · `003` continuation deficit · `004` workflow guidance deficit |
| `EVID-PRC/AVT/ENT/ATT-*` | evidence_consistency | price, availability, entity/brand/name, attribute contradictions (`CONFLICT` only) |
| `FC-*` | freshness_corroboration | `REP` price/availability representation conflicts · `ATTR` attribute conflicts · `ENT` entity/brand conflicts · `FRSH` limited freshness evidence |
| `T4-*` | t4_indexability | `001…005` fetch / indexability · `010…011` robots directives · `020…024` canonical · `030…035` JSON-LD |
| `EVID-001` | cross-page evidence | collection-level contradiction (only `CONFLICT` fires) |
| `ENT-ID-001/002` | entity identity | conflict (high) / ambiguity (medium) |

Findings carry a `classification` of `issue` or `observation`: an `observation` records a
neutral fact worth knowing (e.g. an explicit `index` directive or JSON-LD presence), while an
`issue` is an actionable defect. Severity governs `critical > high > medium > low`.

## Findings, Evidence, and Suggested Actions

The judge-friendly core of every finding:

```json
{
  "id": "TR-AUTH-001",
  "title": "Mandatory authentication barrier lacks navigable authentication entry point",
  "severity": "medium",
  "evidence": "Observed gated checkout/account endpoints with no visible public alternative.",
  "suggested_action": {
    "summary": "Expose a navigable (ideally public) path to the task.",
    "priority": 1
  }
}
```

- **`evidence`** is the concrete observed fact that triggered the rule; it survives
  aggregation and reporting verbatim — nothing is truncated or paraphrased.
- **`suggested_action`** is produced by the audit itself and preserved into the report: a
  `summary` plus a numeric `priority` (1 = highest).
- `severity` and `priority` are separate concepts: severity describes the seriousness of the
  finding; priority is the ordering signal — task relevance first when task context is
  supplied, then audit priority, then severity, then confidence.

Aggregation is conservative and deterministic: duplicate findings collapse only when rule ID
**and** normalized evidence match; distinct evidence is never silently merged; source-audit
attribution is preserved; colliding base IDs are deterministically disambiguated; ordering is
stable.

## Task-Aware Objectives

`AuditContext` accepts a task intent, a task description, and target objectives. Canonical
objectives: `transaction_completion`, `task_completion`, `price_accuracy`, `price_evaluation`,
`inventory_verification`, `entity_identity`, `information_sufficiency`, `content_discovery`,
`content_extraction`, `workflow_progression`, `authentication_access`.

Mapping is a deterministic rule-to-objective taxonomy over the finding ID, refined by
caller-supplied objective keywords and intent keywords.

- Without task context, findings remain `general` with an empty `affected_objectives` list —
  the system never invents semantic relationships.
- With task context, findings may be classified `direct_blocker`, `task_impairing`, or
  `general`, ranked with a transparent, evidence-based `priority_reason`.

## Output

The final report is deterministic JSON:

```json
{
  "site": "https://example.com/",
  "audited_at": null,
  "summary": { "total_findings": 3, "high": 0, "medium": 0, "low": 3 },
  "audits_run": [
    "machine_answerability", "task_readiness", "crawl_render",
    "engagement", "evidence_consistency", "freshness_corroboration",
    "t4_indexability"
  ],
  "findings": []
}
```

- `audited_at` is caller-supplied only — it stays `null` otherwise, keeping output
  deterministic.
- Serialization: sorted keys, `indent=2`, `ensure_ascii=False`, stable ordering.
- Optional `task_context`, `failure`, and `audit_errors` appear when relevant; failures are
  structured diagnostic blocks, never stack traces in the consumer contract.

## Safety / Read-Only Guarantees

Strictly read-only. It does **not**: modify the audited website, submit forms, authenticate
into private systems, run browser automation, execute JavaScript, call external AI/LLM APIs,
use analytics, write to external databases, or perform destructive actions. It is **not** a
full browser crawler, does not search the entire internet, and does not predict AI-provider
ranking or citation outcomes.

## Generalization

Provider-neutral and industry-neutral. It evaluates generic observable website signals — HTML
structure, visible text, headings, links, metadata, JSON-LD, canonical URLs, robots
directives, structural components, pricing/availability representations, navigation and
workflow affordances, and entity-identity signals. There are no per-client site whitelists;
the registrable-domain public-suffix logic is generic DNS/domain structure, not a
client-specific list.

## Limitations (Deliberate Scope)

- **No external or LLM verification** — freshness and correctness are based on explicit
  on-site evidence, not live ground truth.
- **Bounded crawl** — pages beyond 50 pages / depth 3 are not fetched (they are recorded as
  unvisited with reasons).
- **`engagement` is structural** — it assesses affordances, not behavioral analytics.
- **Objective mapping is conservative** — without task context, findings stay `general`
  rather than inventing semantic relationships.

These are scope boundaries, not defects: determinism, read-only behavior, and honest
evidence are the design goals.

## Validation

```text
python scripts/validate_marketplace.py
→ Result: PASS (7 skills, 0 failures, 0 warnings)
```

## Quick Start

```text
pip install -r requirements.txt
python skills/audit-orchestrator/scripts/run_audit.py https://example.com/
```

With task context (objective-aware prioritization):

```text
python skills/audit-orchestrator/scripts/run_audit.py https://shop.example.com/ \
  --intent "purchase running shoes" \
  --objectives transaction_completion,price_accuracy,inventory_verification \
  --details
```

Save to a file (harness-friendly):

```text
python skills/audit-orchestrator/scripts/run_audit.py https://example.com/ -o report.json
```