# Connect the local ATS UI to Python detection

## Goal

Replace the ATS frontend's simulated results with real calls to the existing bounded Python batch detector while keeping the feature local-only, privacy-safe, and unsuitable as a public scanning endpoint.

## Existing behavior

- The Python module accepts explicit caller-approved URLs, enforces robots and SSRF protections, and serializes reports without input URLs or transport details.
- The CLI provides a local research entry point.
- `job_sources/ats/ui/` is a standalone vinext frontend demo. It uses preset results, a timer, and browser-generated `mode=frontend_demo` exports.
- The main FastAPI app already aggregates job-source routers and has a loopback-only dependency used by other local job-source routes.

## HTTP interface

```text
POST http://127.0.0.1:8010/job-sources/ats/detect
Content-Type: application/json

{"urls":["https://careers.example.com"]}
```

- The request has exactly one field, `urls`.
- It accepts 1-20 strings. Leading and trailing whitespace is removed; an empty result is invalid.
- Each physical URL is at most 8,192 UTF-8 bytes and all URL values together are at most 128 KiB.
- The complete request body is read incrementally and rejected above 132 KiB.
- The server fixes batch concurrency at four workers; the caller cannot change policy, output format, timeouts, redirects, or byte limits.
- Only one web batch may run at once. A concurrent request returns `409 detection_in_progress` without starting another batch.
- The success body is the existing canonical JSON report and therefore contains member IDs, detections, and clusters but no input URLs, query strings, tokens, bodies, or transport details.

## Local browser access

- The router retains the existing loopback-client dependency.
- Requests without an `Origin` header remain available to local non-browser clients.
- Browser requests are accepted only from `http://localhost:3000`, `http://127.0.0.1:3000`, or `http://[::1]:3000`.
- The router handles its own exact CORS preflight and response headers. It does not enable application-wide CORS.
- Any other browser origin is rejected before detection.
- No route is added to the vinext/Cloudflare worker. A deployed copy of the page cannot use the local detector because its origin is not allowed.

## Safe errors

- `400 invalid_request`: malformed UTF-8/JSON, wrong shape, wrong value type/count, empty value, or unknown field.
- `403 origin_not_allowed`: browser origin is not one of the exact local origins.
- `409 detection_in_progress`: another web batch holds the single-run lock.
- `413 input_too_large`: body or URL byte bounds are exceeded.
- `415 unsupported_media_type`: request is not JSON.
- `502 detection_failed`: the batch module fails unexpectedly.
- Error responses and logs never echo request values, URLs, paths, query strings, tokens, response bodies, or exception messages.

## Frontend behavior

- The page continues to keep the selected company names and URLs only in browser state.
- Clicking detection sends the ordered selected URLs to the local FastAPI interface and displays returned detections by input position.
- The running state prevents duplicate clicks. Failures return to an explicit retryable error state without showing raw server details.
- Summary counts and table rows derive from the returned report, not preset ATS values.
- Export downloads the exact returned privacy-safe report as `ats-report.json`.
- Labels and README identify the page as local live detection requiring the Python backend; all claims of simulation and `frontend_demo` are removed.
- The page permits at most 20 total sites, matching the server limit.

## Test seams

1. FastAPI HTTP seam: registered router requests cover local-origin success, safe response shape, no-origin local clients, exact preflight, rejected origins, bounded/invalid inputs, busy state, and safe unexpected failures. The detector dependency is replaced only at the batch-module seam so tests never access the internet.
2. Built UI seam: vinext build/render tests verify the local-live labels and absence of demo mode; source-level contract checks verify the exact loopback route, POST request, canonical report export, error state, and 20-site limit.
3. Manual local browser seam: start both local servers and verify selection, real blocked result, error/retry state, summary, and export behavior through the rendered page.

## Non-goals

- No public/serverless ATS endpoint, deployment, authentication, persistence, scheduler, URL discovery, sitemap/search traversal, link following beyond the existing same-origin policy, browser automation, login, cookies, CAPTCHA bypass, or job crawling.
- No changes to detector vendor rules, HTTP policy, CLI behavior, the rest of the FastAPI interface, or the main server bind address.
- No forwarding route inside the vinext worker and no application-wide CORS middleware.

## Acceptance criteria

1. A permitted local page can submit 1-20 explicit URLs and receive the canonical privacy-safe report from the real batch detector.
2. Remote clients and non-local browser origins cannot invoke detection; exact local preflight succeeds.
3. Request sizes, counts, concurrency, and fixed worker policy are bounded before network work, and every error is stable and does not echo request data.
4. The page has no simulated detection or preset result path; display, counts, and export use the returned report.
5. Existing 53 Python ATS tests and UI tests continue to pass; new HTTP/UI contract tests pass; lint has no errors.
6. No public worker route, persistence, discovery, login, browser automation, or unrelated UI/backend change is introduced.

## Verification

- Run existing and new Python ATS tests with the project `.venv`.
- Run UI build/render tests and lint.
- Exercise the local page with both services through the browser.
- Run workflow verification, then separate Standards and Spec reviews.
