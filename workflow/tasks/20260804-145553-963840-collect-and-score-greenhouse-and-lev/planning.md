# Planning Record

- status: `ready`
- grilling_status: `confirmed`
- multi_session: `true`
- spec_ref: `workflow/tasks/20260804-145553-963840-collect-and-score-greenhouse-and-lev/spec.md`

## Unresolved decisions

- None

## Decisions

- None

## Acceptance criteria

- [ ] Full Python/UI verification and independent Standards/Spec reviews pass.
- [ ] Greenhouse and Lever public job payloads normalize, filter, deduplicate, and score through one bounded interface.
- [ ] No application submission, credentials, persistence, scheduling, public proxy, or unsupported ATS adapter is introduced.
- [ ] The local UI retrieves one supported site at a time and renders scored jobs with empty, error, retry, and locked running states.
- [ ] The loopback-only HTTP route enforces stable privacy-safe errors, exact local CORS, input/body bounds, and one active collection.

## Tracer-bullet tickets

### T1: Normalize Greenhouse and Lever public jobs

- blocked_by: none
- [ ] One bounded collection interface resolves exact vendor URLs, fetches official public GET endpoints, normalizes, filters, deduplicates, and scores fixture jobs with safe errors.

### T2: Expose bounded local job collection route

- blocked_by: T1
- [ ] Loopback-only POST route enforces exact CORS, 16 KiB body, validated inputs, one active collection, stable safe errors, and canonical JSON output.

### T3: Add scored jobs to local ATS UI

- blocked_by: T2
- [ ] Supported detected sites can collect and render scored jobs with limit, keyword, empty/error/retry and locked running states; unsupported vendors remain disabled.
