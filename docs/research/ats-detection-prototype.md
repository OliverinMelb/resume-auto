# ATS detection research and prototype boundary

## Goal

Given a caller-approved corporate careers URL, fetch that one public page
under a fail-closed robots and network-safety policy, then identify a candidate
ATS without crawling, discovering endpoints, or fetching page resources.

The first-stage result is:

```text
ats
confidence
evidence
version_hint
supports_json_ld
requires_browser
blocked_reason
```

The signature detector remains a pure function. A separate bounded HTTP seam
owns robots checks, HTTP limits, redirects, and content-size limits. The
prototype never starts a browser.

## Public seam

```python
detect_ats(
    url: str,
    html: str,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> AtsDetection
```

`confidence` is a float in `[0.0, 1.0]`. Evidence is a deterministic tuple of
short tokens such as `hostname:jobs.lever.co` and
`json_ld:@type=JobPosting`; it must never contain a copied page body, cookie,
token, query string, or discovered endpoint.

## Evidence and precedence

The detector uses only public, fixture-visible evidence:

1. A recognized vendor-managed careers hostname is strong evidence.
2. A vendor resource hostname or product marker in HTML is supporting
   evidence. On a custom company domain it is capped at low confidence.
3. Valid `application/ld+json` containing `@type=JobPosting` identifies
   `json_ld` only when no more specific ATS has evidence. A vendor match keeps
   precedence and reports `supports_json_ld=true`.
4. Company names, CSS appearance, a plain product word, and guessed private API
   paths are never sufficient evidence.
5. Conflicting vendor evidence, unrecognized custom-domain versions, and weak
   one-word hints return `unknown` or a low-confidence candidate.

Confidence bands used by the fixture prototype:

- `0.90–1.00`: recognized vendor-managed hostname;
- `0.70–0.80`: a documented shared portal hostname corroborated by a
  matching vendor-owned resource hostname;
- `0.50–0.65`: resource/product evidence on a custom domain;
- below `0.50`: unknown, conflicting, weak, or absent evidence.

`version_hint` is only a coarse public surface marker explicitly present in the
page, such as `career_site_builder` or `legacy`; it is not a claim about a
tenant's internal software version.

The 2026-07-30 live sample matrix is recorded in
`docs/research/ats-live-site-research-2026-07-30.md`. Live Greenhouse, Lever,
Moka, Beisen, Hotjob, Workday, and SAP samples did not expose the synthetic
generator meta used by some hermetic fixtures. Build hashes, Git commits,
dependency versions, shard names, and tenant paths therefore remain excluded
from `version_hint`.

## Browser and blocking policy

`requires_browser=true` means only that the HTTP snapshot is an explicit
JavaScript shell and a later, separately approved stage may need public-page
rendering. It does not launch Playwright and does not override restrictions.

The detector reports observed access restrictions through `blocked_reason`:
HTTP 401/403/429, `noindex`, login gates, and CAPTCHA/verification pages.
Detection may still report a vendor from an exact vendor hostname, but callers
must stop rather than bypass the restriction.

`blocked_reason` is not a classification-failure field. A normal `unknown`
result, conflicting custom-domain signals, and an explicit JavaScript shell
leave it as `None`; the confidence and evidence fields explain those outcomes.
`invalid_url` is the one input-validation block. When a restriction is present
on a custom domain, body/resource evidence is ignored. Only an exact recognized
vendor hostname may retain an ATS candidate alongside the restriction.

## Fixture matrix

Fixed local fixtures cover:

- generic JSON-LD `JobPosting`;
- Greenhouse, Lever, Moka, Beisen, Hotjob/WinTalent, Workday, and SAP
  SuccessFactors;
- vendor plus JSON-LD precedence;
- custom-domain resource evidence with capped confidence;
- conflicting/weak evidence;
- malformed JSON-LD;
- noindex, login, CAPTCHA, HTTP restrictions, and JavaScript shells.

These fixtures are synthetic signatures, not captured customer pages. They
make no coverage claim across tenant customizations or ATS versions.

## Batch clustering prototype

The next pure seam groups existing detection results without fetching or
re-reading any page:

```python
cluster_ats_detections(
    detections: Mapping[str, AtsDetection],
) -> tuple[AtsCluster, ...]
```

Mapping keys are caller-owned opaque member IDs, not URLs that the clusterer
will open or inspect. This keeps membership auditable without copying URL query
strings, tokens, or credentials into aggregate evidence.

Each cluster exposes:

```text
review_status
ats
version_hint
members
min_confidence
max_confidence
evidence
supports_json_ld
requires_browser
blocked_reasons
```

The cluster key is `(review_status, ats, version_hint)`. Review status is
derived from the existing result, in this order:

1. `blocked` when `blocked_reason` is present;
2. `unknown` when `ats=unknown` or confidence is below `0.50`;
3. `confirmed` when confidence is at least `0.90`;
4. `candidate` otherwise.

This deliberately prevents low-confidence custom domains from joining a
confirmed vendor cluster, and prevents blocked pages from being treated as
actionable even when their vendor hostname is known. Different public
`version_hint` values form different clusters.

Members and cluster ordering are deterministic and independent of mapping
insertion order. Confidence is aggregated as a min/max range; evidence and
blocked reasons are de-duplicated; JSON-LD/browser flags use `any`. Empty input
returns an empty tuple.

## Controlled batch detection and reporting

The batch seam accepts only the explicit URL iterable supplied by its caller:

```python
detect_ats_urls(
    urls: Iterable[str],
    *,
    policy: AtsHttpPolicy = AtsHttpPolicy(),
    max_workers: int = 4,
) -> AtsBatchReport
```

The interface is deliberately bounded:

- at most 100 input positions per call;
- `max_workers` must be between 1 and 8;
- exact duplicate URL strings are detected once, while every input position
  remains present in the ordered result;
- the existing per-URL HTTP policy remains authoritative for robots, network
  safety, redirects, response limits, and blocking;
- no URL discovery, retry crawler, browser fallback, persistence, or job
  extraction is added.

`AtsBatchReport.items` stays in caller iteration order. Each item receives a
deterministic opaque member ID such as `item-000001`; the original URL is not
stored in the item or passed to the clusterer. Callers correlate results by
input position. This prevents query strings or credentials in an approved URL
from leaking into cluster membership or serialized reports. Duplicate inputs
receive distinct member IDs and therefore remain auditable even though their
HTTP detection is reused.

The same report can be serialized without filesystem side effects:

```python
serialize_ats_batch_report(report, format="json")
serialize_ats_batch_report(report, format="jsonl")
```

JSON contains ordered `items` and `clusters`. JSONL emits all `detection`
records in input order followed by all `cluster` records in deterministic
cluster order; each line uses `record_type` plus `payload`. Both formats are
UTF-8-safe, compact, deterministic strings and add no original URL or
transport details. Hostnames may still appear as intentional detector
evidence, for example `hostname:jobs.lever.co`.

## Standalone web prototype

`job_sources/ats/ui/` is a local-only frontend prototype with no router,
database, or Python integration. It does not call `detect_ats_url`, fetch a
career site, or imply that displayed results are live.

The primary interaction avoids manual URL entry: users select from the seven
public career-site samples already documented in this research, then run a
short simulated detection to inspect ATS names, confidence, evidence, review
status, browser needs, and summary counts. Adding another URL is an optional
secondary action and stores it only in the current page state. JSON export is
generated entirely in the browser and marks the report as
`mode=frontend_demo`.

The page visibly labels itself as an unconnected frontend demo. Its safety
footer repeats the prototype boundary: robots first, same-origin redirects,
no login/CAPTCHA bypass, and no job crawling.

## Controlled HTTP detection seam

The HTTP-first seam completes the original URL-to-result flow:

```python
detect_ats_url(
    url: str,
    *,
    policy: AtsHttpPolicy = AtsHttpPolicy(),
) -> AtsDetection
```

It requests only:

1. the selected URL's same-origin `/robots.txt`;
2. the selected URL itself when the robots policy allows it;
3. at most three same-origin redirects from either request.

There is no link discovery, sitemap traversal, search, page resource fetching,
cookie jar, login, browser rendering, or call to an endpoint found in HTML.

`AtsHttpPolicy` exposes bounded operational values only: timeout, maximum HTML
bytes, maximum robots bytes, redirect limit, and a public crawler user-agent.
The transport implementation remains hidden inside the module.

Before any request, the seam accepts only HTTP(S), rejects URL credentials,
localhost/private/link-local/reserved/multicast destinations, non-standard
ports, and DNS answers that are not globally routable. Redirects must keep the
same scheme, hostname, and effective port. All accepted DNS addresses are
retained, and the transport connects directly to one of those validated IPs;
HTTPS SNI and certificate verification continue to use the original hostname.
It does not inherit environment proxy settings or ask a resolver again during
connection, closing the resolver-to-connect DNS rebinding window in this
prototype. This remains defense in depth for a local research tool and is not
wired to an untrusted public server interface.

Robots behavior is fail-closed:

- `200`: parse for the configured user-agent and selected target path;
- `404`/`410`: treat as no published robots rules;
- `401`/`403` or an explicit disallow: `robots_disallowed`;
- timeout, DNS/transport failure, `429`, or `5xx`: `robots_unavailable`.

When the selected page redirects, the destination path is checked against the
same parsed robots policy before the redirect is followed. A disallowed
redirect target returns `robots_disallowed` without requesting that path.

The page response must be `text/html` or `application/xhtml+xml` and fit the
configured byte limit. Page restrictions and failures map to deterministic
`blocked_reason` values, including:

```text
unsafe_host
unsafe_redirect
too_many_redirects
robots_disallowed
robots_unavailable
http_unavailable
http_<status>
rate_limited
unsupported_content_type
response_too_large
decode_error
```

Successful HTML is passed unchanged to the existing pure `detect_ats` seam.
The existing noindex/login/CAPTCHA handling remains authoritative, and
`requires_browser=true` is still only a report—this stage never launches one.

On 2026-07-30, a controlled live smoke used this exact seam for the two public
samples already listed in the research matrix:

- `https://job-boards.greenhouse.io/wikimedia` returned `greenhouse`,
  confidence `0.97`, hostname/resource evidence, and no block;
- `https://jobs.lever.co/dnb` returned `lever`, confidence `0.97`,
  hostname/resource evidence, and no block.

Each run was limited to that host's robots file and selected page. No page
resource or API endpoint was fetched. These are time-sensitive smoke results,
not hermetic assertions; the fixed fixtures remain the unit-test source.

## Explicit non-goals

- No ATS job crawler or adapter.
- No internet scan, link traversal, sitemap discovery, or search-engine use.
- No calls to page-discovered, private, authenticated, or write APIs.
- No Playwright, login, CAPTCHA handling, robots/noindex bypass, or credential
  use.
- No changes to shared contracts, routers, the application entry point,
  database code, or other job sources.
