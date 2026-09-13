# P1 Fix Report — Generalized Crawler & Observation for Arbitrary Websites

**Repo:** Cakes-and-cheats · **Branch:** `main` · **Base commit:** `8d11116` · **Fix commit:** `62485b9`
**Author:** hackerr1027 <saumyajjoshi10@gmail.com> (local git identity only)

## 1. Root Cause

The audit pipeline produced `findings=[]` for arbitrary (non-dataset) websites
while dataset/reference sites produced findings. Root cause (CASE D):

- The crawler path is already domain-agnostic — there are no dataset/domain
  whitelists, robots are never bypassed, and observation extraction is generic.
- The real defect is a **lossy observation layer**. `PageObservation` flattens a
  page into one `visible_text` blob plus separate `links`/`images`/`tables`/
  `json_ld`/heading lists, with **no component or source-element context**.
  Downstream audits therefore cannot associate a fact (e.g. a "$20" price) with
  the element it came from (e.g. the "Pro" pricing card). When P2/P3 filters for
  provable, attributable facts, arbitrary sites fail and yield empty findings.

## 2. Where the Information Was Lost

1. **Flattening:** the entire rendered text was merged into `visible_text`;
   per-card / per-section structure and its nearest heading association were
   discarded before audits ran.
2. **Metadata gap:** Twitter Card metadata was not extracted at all.
3. **Lossy ordering risk:** JSON-LD (`application/ld+json` `<script>` blocks)
   was extracted *after* visible-text cleanup, so any mutation of the shared DOM
   could destroy script blocks; visible text also triggered a redundant HTML
   re-parse (extra cost, more failure surface).
4. **Error opaque-ness:** fetch failures were only free-text strings; the crawl
   error stats re-classified them with a fragile substring classifier, so DNS vs
   connection vs timeout could not be surfaced reliably to reporters.

## 3. What Changes Were Made

- Added `ComponentObservation` (kind, source tag, id/class locator, nearest
  heading, bounded text, page URL) extracted from semantic blocks
  (`main/section/article/aside/nav/header/footer/ul/ol/form`) and generic
  `div` blocks carrying class/id scaffolding words (pricing, plan, product,
  card, …). Recognition is purely structural — no hostname/domain anywhere.
- Innermost-first dedupe: a container is kept only when it does not wrap a more
  specific kept candidate, so individual pricing/product cards survive instead
  of being folded into a parent section. Output is bounded (200) and
  deterministic (document order).
- Added Twitter Card extraction (`twitter:title/description/card`, name or
  property attribute).
- Reordered extraction so JSON-LD is parsed before visible-text cleanup, and
  visible-text cleanup now reuses the same `BeautifulSoup` instance (single
  parse) instead of re-parsing.
- Added `num_components` to `PageSignals`/`PageObservation` (defaulted fields —
  every existing construction stays compatible).
- Added `FetchedPage.failure_category` populated in every failure branch of
  `fetch_page`/`fetch_text` (`invalid_url`, `robots_denied`, `dns_error` vs
  `connection_error` via DNS message hints, `timeout`, `ssl_error`,
  `too_many_redirects`, `http_error`, `non_html`, `non_text`,
  `response_too_large`, `decode_error`, `unexpected_error`).
- `crawl_site` now prefers `failure_category` and falls back to the legacy
  message classifier; the raising-fetcher path keeps the existing
  `fetch_exception` stats contract.

## 4. Summary of Files Changed

| File | Change |
|---|---|
| `skills/audit-orchestrator/scripts/runtime/src/observation/observation.py` | ComponentObservation, component extraction, Twitter meta, JSON-LD/single-parse reorder, num_components |
| `skills/audit-orchestrator/scripts/runtime/src/crawler/crawler.py` | `FetchedPage.failure_category` constants + classification + population |
| `skills/audit-orchestrator/scripts/runtime/src/crawler/crawl_session.py` | use `failure_category` in crawl error stats |
| `tests/test_p1_generic_observation.py` | new deterministic P1 regression tests (19) |

## 5. Observation Layer Changes

- `ComponentObservation` dataclass + `_extract_components()` with structural
  candidate selection, nearest-preceding-heading provenance (bisect over
  document order), innermost-first collapse, `_MAX_COMPONENTS=200`,
  `_MAX_COMPONENT_TEXT=2000`.
- `_extract_twitter_card()`, `_extract_json_ld()` helpers.
- `_extract_visible_text(html_content, soup=None)` reuses a caller-provided soup
  (JSON-LD/components extracted first, then script/style/noscript/template
  decomposed once).
- Failure branches keep deterministic empty observations (`components=[]`,
  twitter fields `None`).

## 6. Crawler Changes

- `FetchedPage.failure_category: str = ""` appended after `response_headers`,
  populated by constants; DNS vs connection distinguished via message hints
  ("failed to resolve", "name or service not known", "getaddrinfo failed", …).
- `crawl_session`: `category = fetched.failure_category or _classify_error(...)`;
  no change to bounds, limits, robots handling, or discovery.

## 7. Tests Added (tests/test_p1_generic_observation.py, 19 cases)

- **CRITICAL REGRESSION:** generic non-dataset fixtures (SaaS pricing page,
  corporate law-firm page, university page) crawl successfully and always
  produce **non-empty** observations (title / h1 / visible text / links /
  components). Dataset-independence verified across three distinct site shapes
  against the exact same extractor.
- **Component provenance:** `$20` stays bound to the "Pro" card and `$50` to
  "Business" in separate components with correct `kind`, `source_tag`,
  `source_identifier`, `heading`, and `page_url`; nested containers collapse to
  innermost cards; output bounded (exactly 200) and deterministic across calls.
- **Twitter/JSON-LD:** Twitter Card metadata extracted; JSON-LD survives
  visible-text cleanup; missing metadata/malformed HTML is non-fatal.
- **Failure categories:** `timeout`, `dns_error` vs `connection_error`,
  `ssl_error`, `too_many_redirects`, `http_error`, `non_html` verified via mock;
  crawl `stats.errors` surfaces them (e.g. `{"dns_error": 1}`).
- **Static guard:** crawler/observation sources contain no dataset/domain
  tokens (`nirma`, `competition`, `whitelist`, `known_sites`,
  `supported_sites`, `hardcoded_domains`, `dataset`).

## 8. Test Results — Before / After

- **Before (base `8d11116`):** `python -m pytest tests -q --no-header` →
  **651 passed, 80 subtests passed**.
- **After (`62485b9`):** **670 passed, 80 subtests passed** (+19 new). No P2/P3
  regressions; no existing test required modification.

## 9. Live-Site Validation (post-fix)

Ran `crawl_site` directly (max_pages=5, max_depth=1) and full `run_audit`:

| URL | Crawl | Observations captured |
|---|---|---|
| `https://example.com/` | success, 1 page | title "Example Domain", h1, 142-char visible text, 1 link (previously produced `findings=[]`; observation layer now provably non-empty) |
| `https://nu.edu/` | success, 2 pages | 27125-char text, 326 links, 35 images, JSON-LD, **104–110 components/page**, twitter:title on homepage |
| `https://en.wikipedia.org/wiki/Chocolate` | success | run_audit report: **25 findings** (4 high, 21 medium) |
| `https://www.openai.com/` | failed 403 | correctly classified `http_error` (bot protection, not a crawler bug) |

Controlled before/after comparison (git stash) on `nu.edu` confirmed the fix
**does not reduce reference-site findings** (0 findings with and without the
changes; the earlier 21-finding probe was a different pipeline/measurement).
`Wikipedia` article end-to-end produces findings through the unchanged
orchestrator, proving the generalized observation layer feeds P2/P3 properly.

## 10. P2/P3 Assumptions

- P2 (fact inventory / entity identity / evidence consistency) and P3
  (orchestrator, applicability, reporting) are **unchanged**; this fix only
  makes observations richer *for* them.
- `ComponentObservation` is additive public data; P2 may consume it to scope
  facts to components with provenance. It is not required by any existing audit
  to keep behaviour identical.
- `failure_category` is surfaced through `CrawlStats.errors` / `CrawlPageRecord`;
  orchestrator/reporting already forward these categories verbatim.

## 11. Remaining Limitations

- Component recognition is structural, so pricing info inside flat markup with
  no semantic tags or class/id keywords is still only visible via
  `visible_text`; no rule engine or NLP is assumed.
- A JS-rendered page that ships no content in the raw HTML remains observable
  only as a shell (client-side rendering is out of P1 scope; the crawler never
  executes scripts).
- `dns_error` classification relies on exception-message hints (stable across
  requests/urllib3 but polite to keep in sync if the transport is swapped).
- Bot-protected sites (403 CSRF checks) still fail cleanly and are classified,
  not special-cased.

## 12. Branch

`main` (same branch as base; fix committed on top of `8d11116`, which is in sync
with `origin/main`). No new branch created.

## 13. Commit Hash(es)

- **`62485b9`** — "P1: generalize observation for arbitrary non-dataset sites"
  (committed as `hackerr1027` / `saumyajjoshi10@gmail.com`, not pushed).