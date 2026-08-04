# Planning Record

- status: `ready`
- grilling_status: `confirmed`
- multi_session: `false`
- spec_ref: `workflow/tasks/20260804-141132-055407-connect-local-ats-ui-to-python-detec/spec.md`

## Unresolved decisions

- None

## Decisions

- None

## Acceptance criteria

- [ ] Exact local browser origins can submit 1-20 explicit URLs and receive the canonical privacy-safe report.
- [ ] Existing Python and UI tests plus new HTTP/UI contracts pass; lint has no errors.
- [ ] No public worker route, persistence, discovery, browser automation, or unrelated FastAPI behavior is added.
- [ ] Remote origins, oversized or invalid requests, concurrent runs, and unexpected failures are rejected safely before leaking request data.
- [ ] The UI uses real returned detections for display, counts, retry, and exact report export, with no simulation path.

## Tracer-bullet tickets

- None
