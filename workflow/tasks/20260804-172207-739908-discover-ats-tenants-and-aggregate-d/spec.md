# ATS Tenant Discovery and Direct Job Search

## Goal

Let a local user discover public ATS tenants without pasting company career URLs,
then search normalized jobs by calling each tenant's public ATS endpoint directly.
Reuse the existing `collect_ats_jobs(...)` seam and `NormalizedJob` contract.

## Existing behavior

- `collect_ats_jobs(...)` directly collects Greenhouse, Lever, and Beisen jobs
  from one caller-supplied public career URL.
- The local HTTP/UI flow detects ATS sites and collects one site's jobs.
- Collection already bounds response size, redirects, per-site scans, result
  counts, filtering, scoring, and error disclosure.
- There is no tenant catalog, public-index discovery, cross-tenant search, or
  Ashby listing adapter.

## Proposed behavior

### Tenant discovery

Add one public discovery interface that queries only Common Crawl's fixed index
host. It obtains the latest crawl identifier, performs bounded URL-only queries
for supported ATS host patterns, and converts result URLs into canonical boards:

- Greenhouse: `boards.greenhouse.io`, `job-boards.greenhouse.io`, and EU host;
- Lever: global and EU hosted job sites;
- Ashby: `jobs.ashbyhq.com`;
- Beisen: valid subdomains of `zhiye.com`.

Discovery never downloads WARC records or company/career page bodies. Invalid,
non-HTTPS, nested job-detail, duplicate, and unsupported URLs are discarded or
canonicalized. One discovery run returns at most 50 unique boards.

The discovered catalog is cached in `.runtime/ats-boards.json`, an existing
ignored runtime directory. A non-expired catalog is reused for 24 hours unless
the caller requests refresh. Invalid cache content fails closed and can be
replaced by a fresh discovery.

### Direct ATS collection

Keep `collect_ats_jobs(...)` as the single-site interface. Add Ashby support via
its public posting endpoint and normalize listed public jobs into the existing
`NormalizedJob` contract.

Add one batch search interface accepting a board collection and keyword. It:

- searches at most 20 unique boards with at most four workers;
- delegates every site to `collect_ats_jobs(...)`;
- isolates per-board fetch or validation failures;
- deduplicates jobs by canonical apply/source identity;
- ranks deterministically by existing score and stable tie-breakers;
- returns aggregate counts, jobs, searched boards, and safe failure codes.

### Local HTTP and UI

Add local-only JSON routes to discover boards and search catalog jobs. Reuse the
existing origin, body-size, lock, and CORS policy. Extend the existing local ATS
page with a small "discover companies" and cross-company keyword-search flow;
retain the manual detector and single-site diagnostics.

## Public interfaces and test seams

1. Tenant-discovery seam: Common Crawl URL records in, canonical deduplicated
   ATS boards out; fixed-host transport is injected for deterministic tests.
2. Batch-search seam: ATS board collection plus keyword in, aggregate normalized
   job report out; single-site collector is injected for deterministic tests.
3. Existing single-site seam: `collect_ats_jobs(...)`, extended with Ashby.
4. Local HTTP seam: bounded request/response behavior for discovery and search.

Tests assert behavior only through these seams, not private parsing helpers.

## Constraints

- No paid search or job-data API.
- No API keys, login, cookies, applications, resumes, or browser automation.
- No WARC/body download and no unbounded Common Crawl pagination.
- No arbitrary outbound discovery URL; only fixed Common Crawl HTTPS hosts and
  existing validated ATS collection hosts.
- No PostgreSQL, embeddings, RAG, scheduler, background daemon, or cross-branch
  integration in this slice.
- Preserve existing dirty work and existing manual ATS behavior.

## Non-goals

- Complete coverage of every ATS tenant or every company.
- Crawling company homepages or career pages for discovery.
- Workable, SmartRecruiters, Workday, Moka, Hotjob, or SAP collection adapters.
- Claiming that Common Crawl results are complete or current.
- Searching more than the bounded local MVP limits in one request.

## Acceptance criteria

1. URL-index records for all four supported ATS families produce canonical,
   deduplicated board records without downloading page content.
2. Discovery uses only fixed Common Crawl HTTPS endpoints, enforces response and
   result limits, caches results for 24 hours, and survives corrupt cache data.
3. Ashby public postings normalize into `NormalizedJob`; unlisted or malformed
   records do not leak into results.
4. Cross-tenant search directly reuses `collect_ats_jobs(...)`, searches at most
   20 boards/four workers, isolates site failures, deduplicates jobs, and returns
   stable ordering and aggregate counts.
5. Local routes and UI support discovery followed by keyword search without
   removing manual detection and single-site collection.
6. Existing ATS Python tests pass; new discovery/search/Ashby/web tests pass;
   ATS UI tests, lint, and production build pass.

## Verification

- Run focused red/green Python tests after each vertical slice.
- Run the complete ATS Python test set.
- Run ATS UI unit tests, lint, and production build.
- Run `codex-workflow verify`, then independent standards/spec review.
