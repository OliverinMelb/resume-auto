# Beisen Public Job Collection Spec

## Goal

Extend the existing `collect_ats_jobs(...)` seam and ATS web page so a user can
paste a public Beisen recruitment URL hosted below `zhiye.com`, collect its open
jobs, normalize them into `NormalizedJob`, optionally score them, and inspect the
results beside the existing Greenhouse and Lever results.

## Existing behavior

- ATS detection recognizes Beisen from `beisen.com` resources and corroborated
  `*.zhiye.com` hostnames.
- Direct job collection supports only Greenhouse and Lever.
- `POST /job-sources/ats/jobs` and the web page already consume the provider-
  independent `AtsJobCollection` shape.
- The collector validates provider-controlled hosts before any network request,
  caps scanning at 200 jobs, limits each response to 4 MiB, and exposes only safe
  public errors.

## Verified upstream behavior

Observed on 2026-08-04 against public Beisen-powered career sites:

- `https://beisen.zhiye.com` and `https://nfyy.zhiye.com` publish
  `robots.txt` with `Allow: /`.
- `POST /api/Jobad/GetJobAdPageList` succeeds without a login, cookie, token, or
  API key.
- The JSON request supports zero-based `PageIndex`, `PageSize`, `KeyWords`,
  `SpecialType`, and `DisplayFields`.
- The JSON response exposes `Code`, `Count`, and `Data`; each job can include
  `JobAdId`, `JobAdName`, `Duty`, `Require`, `Org`, `Category`, `CategoryId`,
  `LocNames`, `Salary`, `Kind`, `PostDate`, `EndTime`, and other public listing
  fields.
- `CategoryId` values 1, 2, and 3 correspond to social, campus, and internship
  routes. Public detail URLs use
  `/{social|campus|intern}/detail?jobAdId=<id>`.
- A page size of 50 works and distinct `PageIndex` values return distinct pages.

The evidence and reproducible requests are recorded in `research-beisen.md`.

## Proposed behavior

### External seam

Keep the existing callable and HTTP interfaces unchanged:

```python
collect_ats_jobs(
    career_url,
    *,
    company=None,
    keyword=None,
    limit=20,
    score=True,
) -> AtsJobCollection
```

Add `beisen` to `AtsListingName` and to the UI's collectable ATS set. Existing
callers do not need provider-specific arguments.

### Supported URLs

For this slice, accept only HTTPS hosts strictly below `zhiye.com`, with no user
info and no non-default port. Examples: `beisen.zhiye.com` and
`nfyy.zhiye.com`.

Do not collect from arbitrary custom domains such as `future.jereh.com` yet.
Although some are Beisen-powered, posting to a caller-supplied arbitrary origin
would widen the server-side request surface and require a separate SSRF-safe
custom-domain discovery design. Detection may still identify those pages.

### Provider module

Keep `collect_ats_jobs(...)` as the external seam. Put Beisen-specific URL
validation, POST transport, pagination, payload validation, route construction,
and normalization in `job_sources/ats/listings/beisen.py`. The existing fixed-
host Greenhouse/Lever GET transport must not be weakened or generalized to
support arbitrary origins.

The Beisen adapter will:

1. Derive the fixed same-origin endpoint
   `/api/Jobad/GetJobAdPageList` from the validated `*.zhiye.com` hostname.
2. POST JSON pages of 50 jobs, starting at page 0, stopping at the upstream end
   or after scanning 200 jobs.
3. Request the public display fields needed for location, organization,
   category, employment type, salary, and dates.
4. Use a 10-second request timeout and a 4 MiB response limit per page.
5. Reject redirects, non-200 responses, non-JSON content, malformed JSON,
   `Code != 200`, invalid `Count`, or non-list `Data` with
   `JobListingFetchError("collection_failed")`.
6. Send no credentials, cookies, login requests, application requests, or
   browser automation.

### Normalization

For each valid record:

- `source` and `upstream_platform`: `beisen`.
- Stable source ID: validated hostname plus `JobAdId`.
- Title: `JobAdName`.
- Company: caller override when provided; otherwise the first hostname label.
- Locations: ordered, deduplicated strings from `LocNames`.
- Recruitment channel: exact `CategoryId` mapping (1 social, 2 campus,
  3 internship), then the existing conservative text heuristic for unknown
  categories.
- Employment type: normalized `Kind` when present.
- Description: public duty and requirement text combined for scoring;
  responsibilities and requirements also remain separately available.
- Salary: `Salary` when present.
- Published and expiry dates: validated ISO timestamps; known empty/sentinel
  dates become `None`.
- Public detail/apply URL: same validated origin and channel route.
- Raw data: only bounded public listing metadata needed for audit and filters;
  no session or applicant data.

Malformed individual jobs are skipped consistently with the current collector.
Duplicate source IDs are removed before keyword filtering and limiting.

### Counts and completion

- `discovered_count`: validated upstream `Count`.
- `scanned_count`: number of records considered, capped at 200.
- `matched_count`: normalized, deduplicated records matching the optional
  keyword before the caller's result limit.
- `complete`: true only when the upstream end is reached and the advertised
  count does not exceed the scan cap.
- `stop_reason`: `upstream_end` or `scan_limit` using the current contract.

### Web UI

- Treat detected Beisen `*.zhiye.com` sites as collectable.
- Extend the response type to include `beisen`.
- Add one public Beisen preset so the path can be exercised without hand-
  entering a URL.
- Reuse the existing job table, filters, loading, empty, and error states.

## Non-goals

- Arbitrary Beisen custom-domain collection.
- Login, registration, resume submission, application status, referrals, or
  anti-bot bypass.
- Moka, Hotjob, Workday, or SuccessFactors collection.
- Persistence, scheduling, or database schema changes.
- A broad refactor of the existing Greenhouse and Lever implementations.

## Acceptance criteria

1. Valid `https://<tenant>.zhiye.com/...` URLs collect public Beisen jobs through
   the unchanged Python and HTTP seams; invalid schemes, credentials, ports,
   apex `zhiye.com`, and unrelated/custom domains are rejected before I/O.
2. Pagination reads distinct zero-based pages of 50, stops at the upstream end
   or 200 scanned records, and reports counts/completion accurately.
3. Beisen records normalize stable IDs, public detail URLs, category channels,
   locations, duty/requirements, salary, employment type, and dates without
   leaking non-public data.
4. Transport is bounded to the validated same-origin endpoint, POST only, no
   credentials, 10-second timeout, 4 MiB maximum JSON response, no redirects,
   and safe error messages.
5. Malformed payload shapes and individual records are handled consistently
   with Greenhouse/Lever behavior; scoring and keyword filtering remain source-
   independent.
6. The web page enables collection for detected Beisen `*.zhiye.com` sites and
   renders results with the existing UI states.
7. Existing ATS detection, Greenhouse, Lever, CLI, web, and UI tests remain
   passing.

## Verification

- Focused TDD tests for URL validation, request contract, pagination, response
  caps/errors, normalization, channels, sentinels, deduplication, filtering,
  scoring, and scan completion.
- Full Python test suite.
- UI render test, lint, and production build.
- Read-only live smoke against at least two public `*.zhiye.com` tenants with a
  small page size; do not login or submit forms.
- Final standards and spec review after all checks pass.
