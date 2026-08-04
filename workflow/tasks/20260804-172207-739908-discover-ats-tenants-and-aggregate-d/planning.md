# Planning Record

- status: `ready`
- grilling_status: `confirmed`
- multi_session: `false`
- spec_ref: `workflow/tasks/20260804-172207-739908-discover-ats-tenants-and-aggregate-d/spec.md`

## Unresolved decisions

- None

## Decisions

- Bound one cross-tenant search to 20 boards and four workers with isolated safe failures.
- Expose discovery and cross-tenant search in the existing local HTTP/UI while retaining manual diagnostics.
- Keep collect_ats_jobs as the direct collection seam and add Ashby as a fourth adapter.
- Persist the free discovery catalog under ignored .runtime storage and reuse it for 24 hours unless refresh is requested.
- Use Common Crawl only as a bounded URL index to discover ATS tenants; never fetch WARC or company page bodies.

## Acceptance criteria

- [ ] Ashby listed public jobs normalize through the existing NormalizedJob contract.
- [ ] Batch search directly delegates to existing collectors, deduplicates jobs, and returns stable ranking and aggregate counts.
- [ ] Fixed-host discovery is size-limited, cacheable, and tolerant of corrupt cache data.
- [ ] The local page supports zero-URL tenant discovery followed by keyword search without regressing manual flows.
- [ ] URL-index records produce at most 50 canonical deduplicated Greenhouse, Lever, Ashby, and Beisen boards.

## Tracer-bullet tickets

- None
