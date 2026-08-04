# Batch ATS Career-Site Discovery and Job Aggregation

## Goal

Turn the standalone ATS tool into a useful multi-company job finder. A user can
paste a batch of company homepage URLs, enter one optional job keyword, and run
one bounded operation that discovers recruiting pages, identifies ATS vendors,
collects every supported public board, deduplicates jobs, and returns one ranked
job list.

This slice remains standalone. It does not require a candidate, research report,
database, login, or scheduled task.

## Existing behavior

- The detector accepts explicit URLs and safely fetches one page using robots,
  public-IP validation, DNS pinning, same-origin redirect limits, timeouts, and
  response-size caps.
- The batch detector accepts explicit URLs but does not follow career links.
- Direct public job collection supports Greenhouse, Lever, and Beisen
  `*.zhiye.com` boards, one board per call.
- Moka, Hotjob, Workday, and SuccessFactors are detection-only.
- The web page requires sites to be added individually and requires the user to
  choose one supported site before collecting jobs.

## Product boundary

### Input

The first version accepts 1-20 newline-separated company homepage or known
career URLs in one submission. A line may be either:

```text
https://company.example
Company name | https://company.example
```

The company name is a display override. The URL is the trusted user-provided
seed, not an automatically accepted network destination.

An optional single keyword uses the existing source-independent job matching
behavior. Per-board results remain bounded to 1-50 jobs, with a global output
cap of 200.

Company-name-only internet search is not part of this slice. It would require a
separately chosen search provider or browser-search contract. Bulk URL paste and
file-friendly newline input remove the current one-at-a-time workflow without
introducing a paid or scraped search dependency.

### Output

One report contains:

- ordered seed results and stable member IDs;
- safely reported discovered career/ATS URLs;
- ATS detection, supported/unsupported status, and safe failure reason per
  discovered site;
- collection counts per supported board;
- one cross-board list of normalized, deduplicated, scored jobs;
- total seed/page/board/job counts and a completeness flag.

No data is persisted. The web page can export the report as JSON.

## Module design

### Safe HTML seam

Deepen `job_sources.ats.http` by extracting the existing robots/SSRF/redirect/
size-controlled page loading into one reusable interface. `detect_ats_url(...)`
must call the same implementation, preserving all existing observable behavior.

The returned page snapshot contains only the final approved URL, decoded public
HTML, status, and safe headers. Failures expose stable blocked reasons rather
than transport exceptions or response bodies.

Discovery must not use a second generic HTTP client or inherit environment proxy
settings.

### Discovery interface

Add one caller interface in `job_sources.ats.discovery`:

```python
discover_ats_jobs(
    seeds,
    *,
    keyword=None,
    per_board_limit=20,
    global_limit=200,
    max_workers=4,
) -> AtsDiscoveryReport
```

The module hides crawling, candidate ranking, ATS detection, board
canonicalization, collection, aggregation, and safe failure isolation.

### Bounded discovery rules

For every seed:

1. Fetch the seed page through the safe HTML seam.
2. Detect ATS signatures on the seed itself.
3. Extract anchor URLs and visible anchor text.
4. Prioritize direct supported ATS links and links containing bounded career
   terms such as `career`, `jobs`, `join`, `recruit`, `hiring`, `招聘`, `加入我们`,
   `校招`, or `社招`.
5. Fetch at most three additional candidate pages for a seed.
6. Parse those pages once for direct ATS links, but do not recursively crawl
   beyond that depth.

Only these links may be followed:

- same-origin HTTP(S) links;
- direct links whose host is a known ATS host/suffix; or
- a directly linked public HTTP(S) career candidate whose text/path contains a
  career term.

Every followed URL still passes the safe HTML seam. Candidate URLs are
defragmented, credentials are rejected, non-default ports are rejected, and
query strings are not exposed in the report.

Global bounds:

- at most 20 seeds;
- at most 4 fetched HTML pages per seed including the seed;
- at most 60 unique candidate URLs overall;
- 4 concurrent workers by default, maximum 8;
- existing per-request timeouts and byte caps remain in force.

### Job-board collection

- Canonicalize repeated Greenhouse/Lever tenant links and repeated Beisen host
  links so a board is collected once.
- Collect only Greenhouse, Lever, and validated `*.zhiye.com` Beisen boards.
- Report Moka, Hotjob, Workday, SuccessFactors, JSON-LD, unknown, and unsupported
  custom domains without trying unsupported job collection.
- One board failure must not fail other seeds or boards.
- Deduplicate the final list by `(source, source_job_id)` and keep the first
  occurrence for stable ordering.
- Sort scored jobs by descending score with stable discovery order for ties;
  apply the global output limit after cross-board aggregation.
- Report incomplete status when discovery or a provider scan hits a configured
  cap.

## Local HTTP interface

Add:

```text
POST /job-sources/ats/discover-jobs
```

Request fields:

- `sites`: 1-20 objects containing exact `company` and `url` fields;
- `keyword`: string or null, at most 100 characters;
- `per_board_limit`: integer 1-50;
- `global_limit`: integer 1-200.

The route retains the current loopback-only and local-origin restrictions,
bounded JSON body handling, safe error messages, and one-operation lock. It does
not proxy arbitrary response bodies.

## Web UI

Add a primary “批量自动发现岗位” panel:

- multiline input for `Company | URL` or URL-only lines;
- optional keyword and result limits;
- one button for discovery plus collection;
- progress, partial-failure, empty, retry, and complete states;
- discovered site summary grouped by company;
- one aggregated job table using the existing score/link presentation;
- JSON export.

Keep the current manual detection and one-board tools available as diagnostic
fallbacks.

## Non-goals

- Company-name-only search-engine lookup.
- Crawling the open web or enumerating ATS tenants.
- Database persistence, schedules, research projects, candidates, or resumes.
- Login, registration, job application, CAPTCHA bypass, or browser automation.
- Adding collectors for Moka, Hotjob, Workday, or SuccessFactors.
- Merging V2EX or other Git branches in this task.

## Acceptance criteria

1. A batch of company URLs can be submitted once and returns a single aggregated
   job report without manually pasting discovered ATS board URLs.
2. Direct ATS links and shallow career links are discovered in deterministic
   order within explicit per-seed and global crawl bounds.
3. All page reads reuse the existing robots/SSRF/DNS-pin/redirect/timeout/size
   behavior; unsafe or blocked candidates never reach collection.
4. Repeated tenant links are collected once; jobs are deduplicated across boards,
   scored, stably ranked, and globally limited.
5. Unsupported ATS sites and per-site failures are visible but isolated; other
   companies still return jobs.
6. The local-only HTTP route validates exact bounded request shapes and never
   leaks rejected URLs, upstream bodies, or exception details in errors.
7. The web page supports bulk lines, aggregated results, retry, partial failure,
   and export while retaining the existing diagnostic workflows.
8. Existing ATS detection, CLI, Greenhouse, Lever, Beisen, web, and UI behavior
   remains passing.

## Verification

- TDD tests at the `discover_ats_jobs(...)` and local HTTP interfaces.
- Transport regression tests proving discovery shares the existing safe HTML
  implementation.
- Pure parsing tests for deterministic link ranking, canonicalization, duplicate
  boards, bounds, failures, aggregation, and stable ranking.
- Full Python test suite.
- UI lint, production build, rendered tests, and executable bulk-input tests.
- Small read-only live smoke using public company/career pages without login or
  form submission.
- Independent Standards and Spec review.
