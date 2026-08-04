# Greenhouse and Lever job collection

## Goal

Turn a caller-approved Greenhouse or Lever career-site URL into a bounded,
read-only collection of public jobs, normalize those jobs into the existing
`NormalizedJob` contract, score the matched jobs with the existing evaluator,
and expose the result through the local ATS HTTP/UI flow.

This is the first vertical job-ingestion tracer bullet behind ATS detection. It
establishes one small collection interface with two real adapters so later ATS
vendors can be added behind the same seam.

## Existing behavior

- `job_sources.ats` identifies ATS vendors and safely reports evidence, but it
  never lists jobs.
- `NormalizedJob` is the source-independent job contract.
- `evaluate_normalized_job()` attaches the existing score and qualification
  decision without filtering the normalized record.
- The local ATS page can detect selected career sites and export a detection
  report, but it cannot retrieve or display jobs.

## Official upstream contracts

- Greenhouse Job Board data is public and its GET endpoints do not require
  authentication. Listing jobs uses
  `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs` and
  `content=true` includes descriptions, departments, and offices:
  https://developers.greenhouse.io/job-board.html
- Lever's public Postings API lists published jobs using
  `GET /v0/postings/{site}?skip=X&limit=Y`; the global and EU API hosts are
  `api.lever.co` and `api.eu.lever.co`. JSON fields include title, categories,
  descriptions, hosted/apply URLs, workplace type, and optional salary data:
  https://github.com/lever/postings-api

Only these documented public GET surfaces are in scope. Application submission
endpoints are explicitly excluded.

## Domain terms

- **Career URL**: caller-approved public Greenhouse/Lever job-board URL.
- **Tenant**: Greenhouse `board_token` or Lever `site`, extracted from a career
  URL after exact host and path validation.
- **Listing adapter**: vendor-specific implementation hidden behind the ATS job
  collection interface.
- **Matched job**: normalized public job whose title, description, location, or
  vendor categories contain the optional local keyword.
- **Complete collection**: the adapter reached the upstream end before the scan
  bound; a bounded truncation is returned as incomplete, never presented as a
  complete upstream snapshot.

## Deep-module interface

The external seam is one function and two immutable result types under
`job_sources.ats.listings`:

```python
collect_ats_jobs(
    career_url: str,
    *,
    company: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
    score: bool = True,
) -> AtsJobCollection
```

The interface guarantees:

- only exact supported HTTPS career hosts are accepted;
- `limit` is 1–50, keyword is optional and at most 100 characters, and company
  is optional and at most 200 characters;
- returned jobs are deduplicated by stable vendor-scoped `source_job_id`;
- when scoring is enabled, all returned jobs have `score` and `qualified`, and
  are ordered by descending score with stable upstream order for ties;
- errors never include the caller URL, tenant, query string, upstream body, or
  raw exception text.

The two listing adapters and bounded JSON transport are implementation details.
They are tested through the collection interface; network tests replace the
private transport seam with deterministic payloads.

## URL resolution and transport

Supported career hosts:

- Greenhouse: `boards.greenhouse.io`, `boards.eu.greenhouse.io`, and
  `job-boards.greenhouse.io`.
- Lever: `jobs.lever.co` and `jobs.eu.lever.co`.

URLs must use HTTPS, contain no user information or non-default port, and have a
single non-empty tenant as the first path segment. Tenant values are restricted
to 1–100 ASCII letters, digits, `_`, or `-`. Query and fragment values are
ignored and never forwarded.

Upstream requests use fixed vendor API hosts constructed from the validated
tenant, never a caller-controlled hostname. Transport limits:

- GET only; no credentials, cookies, application submission, or browser;
- 10-second timeout per request;
- same-origin redirects only, at most 2;
- JSON content type and successful HTTP status required;
- at most 4 MiB per response;
- Lever pages use `skip`/`limit` with 50 records per page;
- at most 200 upstream records are scanned per collection.

The official public API contracts authorize these GET requests; HTML scraping
and vendor application endpoints remain outside the module.

## Normalization

Both adapters populate the existing `NormalizedJob` fields. Required mapping:

- `source`: `greenhouse` or `lever`;
- `source_job_id`: `{tenant}:{vendor_job_id}`;
- `source_url`: vendor hosted public job URL;
- `official_apply_url`: Lever `applyUrl`; Greenhouse `absolute_url`;
- title, company, locations, description, employment/work mode, salary and
  public timing fields where supplied;
- `last_verified_at`: current UTC timestamp;
- `upstream_platform`: vendor name;
- `raw_data`: a documented allowlist of public scalar/category fields, never
  the full upstream object.

Greenhouse entity-encoded description HTML is converted to bounded plain text.
Lever uses its documented plain-text fields and list headings. Recruitment
channel is derived conservatively: internship indicators -> `internship`,
graduate/campus indicators -> `campus`, otherwise `social`.

Malformed records are skipped individually. A response with no valid jobs is a
successful empty collection if the upstream payload itself is valid. A changed
top-level payload shape is a collection failure.

## Local HTTP contract

Add `POST /job-sources/ats/jobs` to the existing local ATS router.

Request JSON:

```json
{
  "url": "https://jobs.lever.co/example",
  "company": "Example",
  "keyword": "python",
  "limit": 20
}
```

`company` and `keyword` may be `null`; keys are otherwise exact. Scoring is
always enabled for the Web route. The response is the JSON serialization of
`AtsJobCollection` and normalized jobs. The existing loopback dependency and
exact local-origin CORS policy apply. The route accepts at most one active Web
collection; another returns `409 collection_in_progress`.

Stable errors:

- `400 invalid_request`
- `400 unsupported_job_board`
- `403 origin_not_allowed`
- `409 collection_in_progress`
- `413 input_too_large`
- `415 unsupported_media_type`
- `502 collection_failed`

The body is streamed and rejected above 16 KiB. No error echoes input or
upstream data.

## UI behavior

- After ATS detection, Greenhouse and Lever rows offer “获取岗位”; unsupported
  vendors clearly show “暂未支持岗位获取”.
- The user may enter an optional keyword and choose a 1–50 result limit.
- One site is collected at a time. During collection, selection and collection
  controls are disabled to prevent duplicate or stale runs.
- Jobs show title, company, locations, employment/work mode, score,
  qualification, and a public apply link. The page never submits an
  application.
- Explicit empty, failure, and retry states are rendered. Changing detection
  selection clears any prior job result.
- Existing ATS detection report export remains unchanged.

## Non-goals

- Discovering career URLs or crawling the open Web.
- Workday, Moka, Beisen, Hotjob, SAP, or generic HTML adapters.
- Automatic persistence, scheduling, notifications, or candidate/project
  association.
- Applying to jobs, uploading resumes, login, CAPTCHA handling, or private ATS
  data.
- Renaming the Nowcoder research sink or changing database schema.
- Cross-company full-text search or claiming a truncated collection is complete.

## Acceptance criteria

1. Greenhouse and Lever fixture payloads normalize through the same public
   collection interface into deterministic `NormalizedJob` records.
2. Exact host/tenant validation, fixed API hosts, response/status/type/size
   limits, redirect bounds, Lever pagination, 200-record scan cap, record skip,
   dedupe, keyword filtering, score ordering, and safe failures have tests.
3. The local route enforces loopback/CORS/body/input/concurrency limits and
   returns stable, non-echoing errors.
4. The built UI can start a supported collection, render scored jobs, handle
   empty/error/retry states, and leaves unsupported vendors disabled.
5. No application POST, credential, persistence, scheduler, or public worker
   path is added.
6. Existing Python and ATS UI tests remain green; lint/build pass.
7. Browser QA demonstrates one deterministic local mocked/fixture-backed route
   or a safe supported public listing, plus error/retry and duplicate-run lock.
8. Independent Standards and Spec reviews pass after deterministic verification.

## Verification

- Red/green tests for the collection interface and both adapters.
- FastAPI route tests with mocked collection responses and concurrency controls.
- vinext build/render/source contract tests plus ESLint.
- Full Python unittest discovery and workflow verification.
- Manual local browser interaction recorded as task evidence.
