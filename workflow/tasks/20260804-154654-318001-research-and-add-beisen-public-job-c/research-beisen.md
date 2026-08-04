# Beisen public career-site research

- Observed: 2026-08-04 (Asia/Shanghai)
- Scope: public, unauthenticated recruitment pages only
- Excluded: login, applicant data, application submission, CAPTCHA bypass, and
  non-public employer interfaces

## Conclusion

Beisen's current public recruitment portal exposes a usable unauthenticated JSON
listing endpoint on customer-controlled subdomains of `zhiye.com`:

```text
POST https://<tenant>.zhiye.com/api/Jobad/GetJobAdPageList
```

The endpoint is called by the public portal itself, supports zero-based paging,
and returns enough data to normalize and score jobs without opening every detail
page. It does not require an API token, login, or cookie in the sites tested.

This is a public portal implementation contract, not a documented developer API.
It may change, so the adapter should validate every response and fail safely.

## Primary-source sites

The following live customer pages identify themselves as `Powered by Beisen` or
load Beisen-owned portal assets:

1. [Beisen recruitment](https://beisen.zhiye.com/jobs) — vendor's own recruitment
   portal. The live list endpoint advertised 170 jobs during the check.
2. [Southern Medical University Nanfang Hospital](https://nfyy.zhiye.com/jobs) —
   public hospital recruitment portal. The live endpoint advertised 249 jobs.
3. [Pantum](https://pantum.zhiye.com/jobs) — public customer portal, visibly
   marked `Powered by Beisen`.
4. [Shenzhen State Microelectronics](https://szssmec1.zhiye.com/jobs) — public
   customer portal, visibly marked `Powered by Beisen`.
5. [Jereh recruitment](https://future.jereh.com/form) — Beisen-powered custom
   domain. Its HTML exposes tenant ID 101615 and loads Beisen `bstatics.com`
   portal bundles.

Beisen's official product page describes the hosted recruitment portal as part
of its campus recruitment solution:
[Beisen campus recruitment solution](https://www.beisen.com/solution/campus/).

## Runtime evidence

### Page configuration

The public HTML for `beisen.zhiye.com`, `nfyy.zhiye.com`, and the Jereh custom
domain embeds `BSGlobal` configuration. Observed public fields include:

- `tenantInfo.Id`, `tenantInfo.Name`, and `tenantInfo.Alias`
- portal and page IDs
- page definitions for `jobs` and job detail views
- public Beisen static bundle URLs under `*.bstatics.com`

The current PC portal bundle loads named chunks such as `pc-job-list` and
`pc-job-detail`. The public bundle contains these request paths:

- `/api/Jobad/GetJobAdPageList`
- `/api/Jobad/GetJobAdSearchConditions`
- `/api/JobAd/GetJobAdInfo`

Primary runtime bundle inspected:

```text
https://acdn.bstatics.com/ux/ux-recruitment-portal-2022/release/dist/1993-40abacdf4f41dafb768d.chk.js
```

The bundle's request wrapper uses JSON POST for the page-list endpoint and JSON
GET for detail. The list component constructs a request with `PageIndex`,
`PageSize`, `KeyWords`, `SpecialType`, `PortalId`, and `DisplayFields`.

### List request

A minimal public request that returned jobs on both verified tenants:

```powershell
$body = @{
  PageIndex = 0
  PageSize = 2
  KeyWords = ""
  SpecialType = 0
  DisplayFields = @(
    "Org", "Category", "LocId", "Salary", "Kind", "HeadCount",
    "Station", "PostDate", "EndTime", "Degree", "YearsOfWorking",
    "DetailAddress"
  )
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "https://beisen.zhiye.com/api/Jobad/GetJobAdPageList" `
  -ContentType "application/json" `
  -Headers @{ "X-Requested-With" = "xmlhttprequest"; langType = "zh_CN" } `
  -Body $body
```

No token, API key, login, or cookie is present in this request.

Observed response envelope:

```json
{
  "Code": 200,
  "Message": "operation success",
  "Count": 170,
  "Data": [
    {
      "JobAdId": 430044249,
      "JobAdName": "...",
      "Duty": "...",
      "Require": "...",
      "CategoryId": "1",
      "LocNames": ["北京市"],
      "PostDate": "2025-10-13T14:57:17"
    }
  ]
}
```

Only field names and a shortened public example are retained here; no applicant
or account data was accessed.

### Pagination

- `PageIndex` is zero-based.
- `PageIndex=0, PageSize=2` and `PageIndex=1, PageSize=2` returned distinct IDs.
- `PageSize=50` returned 50 records on `beisen.zhiye.com`.
- `Count` is the advertised total (170 for Beisen and 249 for Nanfang Hospital
  at observation time).

The adapter can therefore read pages of 50 and stop after a short page, reaching
`Count`, or its own 200-record scan cap.

### Public fields

With `DisplayFields` requested, the page-list response can expose:

- identity: `Id`, `JobAdId`, `JobAdName`
- content: `Duty`, `Require`
- organization and channel: `Org`, `Category`, `CategoryId`
- location: `LocNames`, `DetailAddress`, `Station`
- employment: `Kind`, `HeadCount`, `Degree`, `YearsOfWorking`
- compensation and dates: `Salary`, `PostDate`, `EndTime`, `ChangeDate`

The list response already contains `Duty` and `Require`; an N+1 detail fetch is
not necessary for this adapter.

### Detail routes and channel mapping

The current public portal bundle defines business types 1, 2, and 3 as social,
campus, and internship. Verified public routes include:

```text
https://beisen.zhiye.com/social/detail?jobAdId=430044249
https://beisen.zhiye.com/campus/detail?jobAdId=430043452
```

Both returned public HTML without authentication. The list's `CategoryId` can be
used to construct the route, with a conservative text fallback for unknown
categories.

## Robots and access constraints

At observation time, both checked tenant hosts returned:

```text
User-agent: *
Allow: /
```

from `/robots.txt`.

No CAPTCHA or login was required for the small read-only listing requests.
Some customer pages can display load or traffic warnings; the adapter must not
try to bypass those states. It should use bounded requests and surface a safe
temporary failure.

## Domain variants and security decision

Two public hosting shapes exist:

1. Vendor-controlled tenant hosts such as `beisen.zhiye.com`.
2. Customer custom domains such as `future.jereh.com` that load the same Beisen
   portal implementation.

The first adapter should support only validated HTTPS subdomains of
`zhiye.com`. Deriving and POSTing to the same origin is then constrained to a
vendor-controlled suffix.

Accepting an arbitrary custom domain and POSTing to it would turn the server into
a more general caller-directed request mechanism. Safely supporting that shape
would require a separate SSRF design (DNS/IP checks, redirect policy, positive
Beisen attestation, and rebinding considerations). Custom domains should remain
detection-only in this slice.

## Adapter recommendation

- Preserve `collect_ats_jobs(...)` as the caller interface.
- Put Beisen URL validation, JSON POST transport, pagination, response
  validation, and normalization in a dedicated provider module.
- Use pages of 50 with the existing 200-record scan cap.
- Use no credentials and do not invoke login/application endpoints.
- Enforce HTTPS, default port, `*.zhiye.com`, fixed endpoint path, no redirects,
  10-second timeout, and 4 MiB maximum response per page.
- Validate `Code == 200`, non-negative integer `Count`, list `Data`, and each
  required job field. Skip malformed individual records; reject malformed
  envelopes with the existing safe `collection_failed` error.

## Unresolved questions

- Beisen does not appear to publish this portal endpoint as a versioned external
  developer contract, so backward compatibility is not guaranteed.
- Some tenants may customize field availability or categories beyond the common
  1/2/3 mapping; fixtures should cover missing optional fields and unknown
  categories.
- Custom-domain support is deliberately deferred until it has its own SSRF-safe
  discovery and transport design.
