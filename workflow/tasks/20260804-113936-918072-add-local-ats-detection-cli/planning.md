# Planning Record

- status: `ready`
- grilling_status: `confirmed`
- multi_session: `false`
- spec_ref: `workflow/tasks/20260804-113936-918072-add-local-ats-detection-cli/spec.md`

## Unresolved decisions

- None

## Decisions

- None

## Acceptance criteria

- [ ] Diagnostics and reports do not expose URL, query, token, body, path, or rejected argument values.
- [ ] Existing ATS tests pass and the frontend remains unchanged.
- [ ] Exit codes are 0 for a produced report including all-blocked, 1 for execution failure, and 2 for arguments or input.
- [ ] Explicit UTF-8 file or stdin input produces the existing JSON or JSONL report.
- [ ] Input is bounded to 100 URLs, 8192 bytes per line, 128 KiB total, and 1-8 workers.

## Tracer-bullet tickets

- None
