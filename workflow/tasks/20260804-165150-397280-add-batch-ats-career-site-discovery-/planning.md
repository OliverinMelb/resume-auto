# Planning Record

- status: `discovering`
- grilling_status: `pending`
- multi_session: `false`
- spec_ref: `workflow/tasks/20260804-165150-397280-add-batch-ats-career-site-discovery-/spec.md`

## Unresolved decisions

- None

## Decisions

- Crawl at most three additional career candidates per seed and aggregate only supported Greenhouse, Lever, and Beisen boards while reporting unsupported ATS detections.
- Deepen the existing safe HTTP module and reuse its robots, SSRF, DNS-pinning, redirect, timeout, and size behavior for every discovered page.
- Keep the feature standalone and in-memory; no candidate, research-project, persistence, schedule, or cross-branch merge.
- Use bulk company homepage/career URLs as free deterministic seeds; company-name-only search is deferred until a search provider is deliberately chosen.

## Acceptance criteria

- [ ] Discovery cannot fetch a page through an unvalidated generic HTTP path.
- [ ] Duplicate boards are collected once; site failures are isolated; jobs are deduplicated and stably ranked.
- [ ] One batch submission returns one aggregated cross-company job report.
- [ ] The local HTTP route and web UI expose bounded bulk discovery, partial failures, retry, aggregate results, and JSON export without removing manual diagnostics.
- [ ] Users can paste up to 20 Company | URL or URL-only lines instead of adding ATS URLs one at a time.

## Tracer-bullet tickets

- None
