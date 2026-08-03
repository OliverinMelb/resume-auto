# Issue tracker

- kind: `local`
- location: Local Markdown under `.scratch/<feature>/issues/`.
- publication_policy: `explicit`
- prs_as_request_surface: `false`

## Operations

Use `.scratch/<feature>/spec.md` for a standalone spec and one file per ticket under `.scratch/<feature>/issues/<ticket-id>.md`. Each ticket records status, acceptance criteria, `Blocked by`, and dated comment sections. List/frontier operations scan those files; claim changes status to `in-progress`; resolve records the outcome and sets status to `closed`. Codex Workflow evidence remains under `workflow/tasks/<task-id>/`.

The configured adapter must support fetch/read, list/query, create, update, comment, and close operations. For local Markdown these are filesystem reads and bounded file edits; for remote trackers they are CLI/API operations.

Distinguish issues from pull/merge requests; PRs/MRs are not a request surface unless `prs_as_request_surface` is deliberately changed. A spec is one canonical artifact; tickets are tracer-bullet vertical slices with explicit status and blocking edges.

## Wayfinding operations

Create one map issue and child decision tickets. Query the frontier as open children with no unresolved blockers. Claim one decision ticket before work, append evidence/comments, resolve at most one non-research decision per session, then update the map and newly visible frontier. Never treat the map itself as the implementation spec.

A Workflow invocation authorizes local planning artifacts only. Remote issue creation or modification always requires explicit user authorization.
