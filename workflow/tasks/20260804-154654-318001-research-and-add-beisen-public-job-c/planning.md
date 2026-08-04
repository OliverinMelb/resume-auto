# Planning Record

- status: `ready`
- grilling_status: `confirmed`
- multi_session: `false`
- spec_ref: `workflow/tasks/20260804-154654-318001-research-and-add-beisen-public-job-c/spec.md`

## Unresolved decisions

- None

## Decisions

- Keep collect_ats_jobs as the external seam and isolate Beisen POST transport and normalization in a provider module.
- Read zero-based pages of 50 up to the existing 200-record scan cap; use the list payload without extra detail requests.
- Support only validated HTTPS subdomains of zhiye.com in the first Beisen collector; custom domains remain detection-only to avoid widening SSRF exposure.

## Acceptance criteria

- [ ] Beisen pagination, counts, scan cap, deduplication, keyword filtering, scoring, and completion semantics match the existing collection contract.
- [ ] Beisen transport is POST-only to the validated same origin with no credentials, 10-second timeout, 4 MiB response cap, no redirects, and safe errors.
- [ ] Normalized Beisen jobs expose stable IDs, public route URLs, channels, locations, duty/requirements, salary, employment type, and safe date handling.
- [ ] The web UI enables Beisen collection and existing ATS, Greenhouse, Lever, CLI, web, and UI tests remain passing.
- [ ] Validated zhiye.com tenant URLs collect through the unchanged Python and HTTP seams; unsafe or unrelated URLs are rejected before I/O.

## Tracer-bullet tickets

- None
