# Public SmartRecruiters and Workable job-reading contracts

Research date: 2026-08-04

Scope: public, read-only job discovery and normalization. This note deliberately excludes candidate data, applications, job mutation, authenticated reporting, and other customer-only ATS data.

## Recommendation

Both providers can be added without credentials:

- SmartRecruiters has an unauthenticated public Posting API with separate list and detail calls. Page with `limit=100` and `offset`, then fetch each selected posting's detail to obtain the description and authoritative `postingUrl`/`applyUrl`.
- Workable has an unauthenticated account endpoint that returns all public jobs in one response. Use `?details=true` to include descriptions; there is no documented separate public detail call or public pagination contract.

Keep every request on a fixed first-party HTTPS origin. Treat all URLs returned in JSON as untrusted input and validate them before storing or displaying them.

## SmartRecruiters

### Public contract and authentication boundary

The public surface is the Posting API. It exposes only postings already made public and supports:

```text
GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings
GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings/{postingId}
```

SmartRecruiters' current authentication guide and Customer API overview explicitly classify the Posting API as public data available without authentication. The overview also distinguishes it from customer Job, Candidate, Application, User, Configuration, Interview, Message, and Reporting APIs, which use API key or OAuth access ([Authentication](https://developers.smartrecruiters.com/docs/authentication), [Customer API overview](https://developers.smartrecruiters.com/docs/customer-overview)).

There is one documentation inconsistency: the Posting API overview currently says the Posting API supports only API-key authentication, while the newer authentication guide says it requires no authentication ([Posting API overview](https://developers.smartrecruiters.com/docs/posting-api)). Live requests without `X-SmartToken` returned HTTP 200 for both list and detail on 2026-08-04, so the implementation should make no token a requirement. A future 401/403 should be handled as a per-board failure, not as a reason to introduce credential handling into this adapter.

Do not confuse the public Posting API with `GET /feed/publications`, application creation, screening questions, candidate status, or the internal Jobs API. Those are partner/customer workflows and require an `X-SmartToken`, API key, or OAuth scopes; they are non-goals for this feature ([authenticated publication feed](https://developers.smartrecruiters.com/reference/postingsjsonstream-1), [application questions authentication](https://developers.smartrecruiters.com/docs/get-application-screening-questions-and-privacy-policies)).

### Tenant and career URLs

`companyIdentifier` is the identifier after the slash in the default career-site URL `https://careers.smartrecruiters.com/{companyIdentifier}` ([Posting endpoints](https://developers.smartrecruiters.com/docs/endpoints)). Public job pages returned by the current API use:

```text
https://jobs.smartrecruiters.com/{companyIdentifier}/{postingId}-{slug}
```

Therefore the canonical internal board record should be:

```text
provider = smartrecruiters
tenant = lowercase companyIdentifier
career_url = https://careers.smartrecruiters.com/{companyIdentifier}
```

The career URL can redirect to a customer-owned career site, so collection must not follow that redirect to discover the API. Build the API URL directly from the validated identifier.

### Pagination, filters, and bounds

The list endpoint supports `q`, `limit`, `offset`, country/region/city/department/language and other filters. Responses are a `ListResult` with `limit`, `offset`, `totalFound`, and `content`; pagination advances `offset` until it reaches `totalFound` ([list and paging documentation](https://developers.smartrecruiters.com/docs/customer-overview), [Posting list reference](https://developers.smartrecruiters.com/reference/v1listpostings)).

- Documented maximum page size: 100.
- Documented total-result ceiling: not specified.
- Current general throttles: 10 requests/second and 8 concurrent requests ([Customer API overview](https://developers.smartrecruiters.com/docs/customer-overview)).
- Proposed local safety bound: request at most 100 postings per page, stop after 10 pages or 1,000 postings per tenant, and retain the repository's response-byte and timeout caps.

### Relevant response fields

List objects provide `id`, `uuid`, `name`, `jobAdId`, `company`, `releasedDate`, `location`, `department`, `function`, `typeOfEmployment`, `experienceLevel`, `customField`, and `ref`. The detail object adds `jobId`, `creator`, `applyUrl`, `postingUrl`, `referralUrl`, `jobAd.sections`, and `active`; `jobAd.sections` contains the description sections used to build the normalized JD ([Posting endpoints and objects](https://developers.smartrecruiters.com/docs/endpoints)). Compensation is optional in detail responses when the employer exposes it ([compensation changelog](https://developers.smartrecruiters.com/changelog/posting-api-expose-compensation)).

Suggested mapping:

| Normalized field | SmartRecruiters source |
| --- | --- |
| source_job_id | `id` (retain `uuid` in raw metadata) |
| title | `name` |
| company | `company.name` |
| published_at | `releasedDate` |
| location | `location.fullLocation`, or city/region/country; remote/hybrid flags |
| department/team | `department.label`, `function.label` |
| employment_type | `typeOfEmployment.label` |
| description | concatenate the text from `jobAd.sections` in response order |
| source_url | validated `postingUrl` |
| apply_url | validated `applyUrl` |

### URL safety and Common Crawl discovery

Accept a discovered tenant only from fixed HTTPS hosts and the first path segment:

- `https://careers.smartrecruiters.com/{tenant}`
- `https://jobs.smartrecruiters.com/{tenant}/{posting-id-or-slug}`
- optionally indexed API URLs matching `https://api.smartrecruiters.com/v1/companies/{tenant}/postings...`

Useful Common Crawl index patterns are consequently:

```text
careers.smartrecruiters.com/*
jobs.smartrecruiters.com/*/*
api.smartrecruiters.com/v1/companies/*/postings*
```

Only accept a conservative ASCII tenant token (letters, digits, `_` and `-`, with a local length bound), reject userinfo, ports, fragments and lookalike/subdomain hosts, and always construct the API request on exact host `api.smartrecruiters.com`. For returned URLs, accept HTTPS `postingUrl`/`applyUrl` only when the exact host is `jobs.smartrecruiters.com`, the first path segment equals the requested tenant, and the next segment begins with the same posting `id`. Drop `referralUrl` and arbitrary external redirects. Query parameters such as the observed `oga=true` may be retained only after the path identity check.

### Live public GET observation

On 2026-08-04, without any authorization header:

```text
GET https://api.smartrecruiters.com/v1/companies/smartrecruiters/postings?limit=2&offset=0
-> 200 application/json; limit=2, offset=0, totalFound=8, content count=2

GET https://api.smartrecruiters.com/v1/companies/smartrecruiters/postings/744000137613800
-> 200 application/json
-> postingUrl=https://jobs.smartrecruiters.com/smartrecruiters/744000137613800-senior-information-security-engineer
-> applyUrl=https://jobs.smartrecruiters.com/smartrecruiters/744000137613800-senior-information-security-engineer?oga=true
```

An unauthenticated request with `limit=101` was clamped to `limit=100`, consistent with the documented maximum. This is a point-in-time observation of the first-party API, not a substitute for the documented contract.

## Workable

### Public contract and authentication boundary

The public account endpoint is:

```text
GET https://www.workable.com/api/accounts/{account_subdomain}
GET https://www.workable.com/api/accounts/{account_subdomain}?details=true
```

It returns the account name/description and a `jobs` array containing public jobs. `details=true` adds each job's description ([public account endpoint reference](https://workable.readme.io/reference/jobs-1)). Workable's own careers-page help explicitly recommends this endpoint, plus the optional public locations and departments endpoints, as public alternatives ([careers-page API guide](https://help.workable.com/hc/en-us/articles/115012771647-Using-the-Workable-API-to-create-a-careers-page), [API troubleshooting](https://help.workable.com/hc/en-us/articles/4903195036183-Troubleshooting-API-issues)).

Do not use the superficially richer SPI endpoints for this adapter:

```text
GET https://{account_subdomain}.workable.com/spi/v3/jobs
GET https://{account_subdomain}.workable.com/spi/v3/jobs/{shortcode}
```

Those expose non-public job states and require `Authorization: Bearer ...` with at least the `r_jobs` scope. Creating candidates additionally requires `w_candidates`. They are customer/internal APIs and are non-goals ([Workable API token guide](https://workable.readme.io/reference/generate-an-access-token), [careers-page API guide](https://help.workable.com/hc/en-us/articles/115012771647-Using-the-Workable-API-to-create-a-careers-page)).

### Tenant and career URLs

The tenant is Workable's `account_subdomain`. Its default hosted career page is:

```text
https://apply.workable.com/{account_subdomain}/
```

Workable documents that customers may instead use a custom career domain, so the fixed hosted URL is the safe canonical board identity even if a company advertises another career URL ([career page options](https://help.workable.com/hc/en-us/articles/115012944968-Comparing-careers-page-options), [career page builder](https://help.workable.com/hc/en-us/articles/360052322194-Managing-the-Careers-page-site-builder)).

Canonical board record:

```text
provider = workable
tenant = lowercase account_subdomain
career_url = https://apply.workable.com/{account_subdomain}/
```

### Pagination and bounds

The public account endpoint documents no `limit`, `offset`, cursor, page, result ceiling, or separate public detail endpoint. It returns the entire `jobs` array; `details=true` supplies the description in that same response ([public account endpoint reference](https://workable.readme.io/reference/jobs-1)). Therefore:

- Official pagination/result upper bound: not specified.
- Proposed local safety bound: one request per tenant, maximum 4 MiB response, maximum 1,000 job objects, and the repository's normal timeout. Reject an oversized response rather than partially parsing it.
- Keyword filtering is local; Workable's help says the public/custom API provides raw data and the consumer implements search/filtering ([careers-page API guide](https://help.workable.com/hc/en-us/articles/115012771647-Using-the-Workable-API-to-create-a-careers-page)).

### Relevant response fields

Top-level fields are `name`, `description`, and `jobs`. Public job objects document `title`, `code`, `shortcode`, country/state/city, `department`, `telecommuting`, `published_on`, `url`, `application_url`, `shortlink`, `created_at`, optional `description`, `employment_type`, `industry`, `function`, `experience`, and `education`; current responses can also contain `locations` ([public account endpoint reference](https://workable.readme.io/reference/jobs-1)).

Suggested mapping:

| Normalized field | Workable source |
| --- | --- |
| source_job_id | `shortcode` (fallback `code` only if non-empty) |
| title | `title` |
| company | top-level `name` |
| published_at | `published_on` (fallback `created_at`) |
| location | `locations`, otherwise city/state/country and `telecommuting` |
| department/team | `department`, `function` |
| employment_type | `employment_type` |
| description | `description` from `details=true` |
| source_url | validated `url`/`shortlink` |
| apply_url | validated `application_url` |

The endpoint reference's prose appears to swap the meanings of `url` and `application_url`. Current first-party responses and the authenticated example in Workable's help show `url` as the job page and `application_url` as the application form. Use URL path shape, not the prose label alone, when normalizing ([public endpoint reference](https://workable.readme.io/reference/jobs-1), [careers-page API guide](https://help.workable.com/hc/en-us/articles/115012771647-Using-the-Workable-API-to-create-a-careers-page)).

### URL safety and Common Crawl discovery

Useful Common Crawl index patterns:

```text
apply.workable.com/*
apply.workable.com/*/j/*
apply.workable.com/j/*
```

Use only `https://apply.workable.com/{tenant}` pages to derive the tenant. A global `https://apply.workable.com/j/{shortcode}` job URL does not encode its tenant and must not create a board by itself. Do not infer tenants from arbitrary customer custom domains.

Build the JSON request only as `https://www.workable.com/api/accounts/{validated_tenant}?details=true`; reject userinfo, ports, fragments, encoded slashes and lookalike hosts. Treat response URLs as untrusted. For the current hosted format, accept exact HTTPS host `apply.workable.com` and require:

- source: `/j/{same-shortcode}` or the tenant-scoped hosted equivalent `/{tenant}/j/{same-shortcode}`,
- apply: `/j/{same-shortcode}/apply`,
- the URL shortcode must equal the JSON `shortcode` case-insensitively.

If a response contains a customer custom-domain URL, do not fetch it server-side; derive a fixed-host Workable link from the validated shortcode or omit the URL. Never follow arbitrary URLs during collection.

### Live public GET observation

On 2026-08-04, without any authorization header:

```text
GET https://www.workable.com/api/accounts/luxasia?details=true
-> 200 application/json; 192 public jobs; UTF-8 response size 1,257,409 bytes
-> first observed shortcode=5EC743DF75
-> url=https://apply.workable.com/j/5EC743DF75
-> application_url=https://apply.workable.com/j/5EC743DF75/apply
-> shortlink=https://apply.workable.com/j/5EC743DF75
```

The observed job object contained `application_url`, `city`, `code`, `country`, `created_at`, `department`, `description`, `education`, `employment_type`, `experience`, `function`, `industry`, `locations`, `published_on`, `shortcode`, `shortlink`, `state`, `telecommuting`, `title`, and `url`. This is point-in-time first-party behavior; adapters should tolerate missing optional fields.

## Explicit non-goals

- No API keys, OAuth tokens, cookies, or signed-in sessions.
- No application submission, candidate creation/status, questions, employee or recruiter data.
- No job creation, mutation, publication feeds, analytics, or internal/draft/confidential jobs.
- No crawling arbitrary company career pages, custom domains, returned redirects, attachments, or media.
- No attempt to guarantee completeness beyond each provider's public posting surface and the local safety caps.
- No compensation inference when a public response omits compensation.
