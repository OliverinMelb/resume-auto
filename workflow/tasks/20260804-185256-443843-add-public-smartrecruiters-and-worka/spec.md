# Public SmartRecruiters and Workable adapters

## Goal

Extend the existing zero-credential ATS discovery and direct-job collection flow
with SmartRecruiters and Workable, while preserving `collect_ats_jobs(...)`,
`AtsBoard`, `NormalizedJob`, the local search route, and the current UI workflow.

## Existing behavior

- Common Crawl URL records become canonical Greenhouse, Lever, Ashby, or Beisen
  boards and are cached for 24 hours.
- `collect_ats_jobs(...)` validates one supported career URL, calls only a fixed
  first-party public job endpoint, normalizes at most 200 jobs, and returns at
  most 50.
- Cross-board search handles at most 20 boards with four workers and isolates
  per-board failures.
- The local page can discover boards, search jobs, or manually inspect one
  supported career URL.

## Proposed behavior

### Shared provider contract

Add `smartrecruiters` and `workable` to the canonical `AtsProvider` type and to
the detector/UI supported-provider types. The existing public seams and report
shapes remain unchanged.

### SmartRecruiters

- Accept only exact HTTPS career hosts `careers.smartrecruiters.com` and
  `jobs.smartrecruiters.com`, with one conservative ASCII tenant in the first
  path segment.
- Construct requests only on exact origin `https://api.smartrecruiters.com`.
- Call the public Posting list endpoint without credentials. Pass the normalized
  keyword through the documented `q` parameter when present and request at most
  100 list records.
- Fetch details for at most 50 postings per collection so descriptions and
  authoritative posting/apply URLs are available without unbounded fan-out.
- A malformed detail is dropped without failing valid siblings. A transport or
  invalid top-level list response fails that board safely.
- Accept returned posting/apply URLs only on exact HTTPS host
  `jobs.smartrecruiters.com`, with the requested tenant and a path segment that
  starts with the same posting ID.
- Normalize title, employer, location/remote state, employment type,
  department/function, publication time, concatenated public description
  sections, source/apply URLs, and safe public metadata.

### Workable

- Accept only exact HTTPS host `apply.workable.com`, with a conservative ASCII
  tenant in the first path segment. A global `/j/{shortcode}` URL cannot discover
  a tenant.
- Construct one unauthenticated request only to
  `https://www.workable.com/api/accounts/{tenant}?details=true`.
- Keep the existing 4 MiB response cap, scan at most 200 returned job objects,
  and return at most 50. Workable exposes no public pagination contract; an
  oversized response fails safely rather than switching to authenticated SPI.
- Accept returned source/apply URLs only on exact HTTPS host
  `apply.workable.com`, with the same shortcode in `/j/{shortcode}` and
  `/j/{shortcode}/apply`; tenant-scoped hosted variants are also allowed.
- Normalize the public account name, title, shortcode, locations/remote state,
  department/function, employment type, publication date, description, and safe
  public metadata.

### Discovery and UI

- Add bounded Common Crawl URL-index patterns for
  `careers.smartrecruiters.com`, `jobs.smartrecruiters.com`, and tenant-scoped
  `apply.workable.com` pages.
- Convert detail URLs into one canonical board per provider/tenant:
  `https://careers.smartrecruiters.com/{tenant}` and
  `https://apply.workable.com/{tenant}`.
- Extend the current automatic and manual UI guards; do not add new routes or a
  second search implementation.

## Constraints and non-goals

- No API keys, OAuth, cookies, login, applications, candidates, draft/internal
  jobs, or authenticated SmartRecruiters Customer/Partner APIs.
- No Workable `/spi/v3` endpoint, customer custom-domain crawl, or application
  submission.
- No arbitrary outbound URLs, returned-link fetches, browser automation, WARC
  downloads, scheduler, database, embeddings, or new provider framework.
- Preserve all pre-existing dirty work and current provider behavior.

## Acceptance criteria

1. Public URL-index records produce canonical, deduplicated SmartRecruiters and
   Workable boards while rejecting global Workable job URLs and lookalike hosts.
2. Both providers are detected and accepted by the existing backend/UI URL
   policy without weakening HTTPS, userinfo, port, or tenant checks.
3. SmartRecruiters public list/detail payloads and Workable public account
   payloads normalize into `NormalizedJob`, including descriptions and validated
   source/apply links; malformed siblings are excluded.
4. Collection remains bounded, credential-free, and delegates unchanged through
   `collect_ats_jobs(...)` and cross-board search.
5. Existing Python and UI behavior remains green; new provider tests, complete
   Python tests, UI build/tests/lint, workflow verification, and independent
   Standards/Spec review pass.

## Verification

- Use TDD through the public discovery, detection, collection, search, and local
  UI seams.
- Run focused ATS Python tests after each provider slice.
- Run `python -m unittest discover`, UI `npm test`, and UI `npm run lint`.
- Run `workflow.ps1 verify`, independent Standards/Spec review, and a bounded
  live GET smoke test against one public tenant per provider.

## Research basis

See [research-public-ats.md](research-public-ats.md) for official documentation,
authentication boundaries, field mappings, URL rules, and live public endpoint
observations.
