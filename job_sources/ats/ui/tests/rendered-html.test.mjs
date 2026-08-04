import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

import { isCollectableCareerUrl } from "../app/collectable-sites.ts";

const projectRoot = new URL("../", import.meta.url);

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the local ATS detection interface", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>企业招聘官网 ATS 检测<\/title>/i);
  assert.match(html, /已收录的企业招聘站/);
  assert.match(html, /检测选中网站/);
  assert.match(html, /本地实时检测/);
  assert.match(html, /需要启动本地 Python 服务/);
  assert.match(html, /获取并评分公开岗位/);
  assert.match(html, /自动发现 ATS 公司/);
  assert.match(html, /直接搜索公开岗位/);
  assert.match(html, /SmartRecruiters/);
  assert.match(html, /Workable/);
  assert.match(html, /检测到支持直采的 ATS 后/);
  assert.match(html, /Wikimedia Foundation/);
  assert.match(html, /Dun &amp; Bradstreet/);
  assert.match(html, /北森招聘/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape/);
});

test("uses the bounded local Python route without a public worker proxy", async () => {
  const [page, layout, collectableSites, packageJson, worker] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/collectable-sites.ts", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
    readFile(new URL("../worker/index.ts", import.meta.url), "utf8"),
  ]);

  assert.match(
    page,
    /http:\/\/127\.0\.0\.1:8010\/job-sources\/ats\/detect/,
  );
  assert.match(page, /fetch\(ATS_DETECTION_ENDPOINT/);
  assert.match(
    page,
    /http:\/\/127\.0\.0\.1:8010\/job-sources\/ats\/jobs/,
  );
  assert.match(page, /fetch\(ATS_JOBS_ENDPOINT/);
  assert.match(
    page,
    /http:\/\/127\.0\.0\.1:8010\/job-sources\/ats\/boards\/discover/,
  );
  assert.match(
    page,
    /http:\/\/127\.0\.0\.1:8010\/job-sources\/ats\/jobs\/search/,
  );
  assert.match(page, /fetch\(ATS_DISCOVERY_ENDPOINT/);
  assert.match(page, /fetch\(ATS_SEARCH_ENDPOINT/);
  assert.match(page, /keyword:\s*automaticKeyword\.trim\(\)/);
  assert.match(page, /max_boards:\s*automaticMaxBoards/);
  assert.match(page, /company:\s*target\.site\.company/);
  assert.match(page, /keyword:\s*jobKeyword\.trim\(\) \|\| null/);
  assert.match(page, /score/);
  assert.match(page, /collection\.jobs/);
  assert.match(page, /\.\.\.ATS_PROVIDERS/);
  assert.match(page, /ATS_PROVIDER_SET\.has\(value\)/);
  assert.match(
    collectableSites,
    /export const ATS_PROVIDERS = \[[\s\S]*?"beisen"[\s\S]*?"smartrecruiters"[\s\S]*?"workable"/,
  );
  assert.match(
    collectableSites,
    /export type AtsProvider = \(typeof ATS_PROVIDERS\)\[number\]/,
  );
  assert.match(collectableSites, /export const ATS_PROVIDER_SET/);
  assert.match(page, /ats:\s*AtsProvider/);
  assert.match(page, /https:\/\/beisen\.zhiye\.com\/jobs/);
  assert.match(page, /isCollectableCareerUrl\(detection\.ats, site\.url\)/);
  assert.match(page, /重新获取/);
  assert.match(page, /暂未支持岗位获取/);
  assert.match(page, /employment_type/);
  assert.match(page, /job\.qualified \? "合格" : "未达线"/);
  assert.match(
    page,
    /function toggleSite[\s\S]*?if \(isBusy\) return;/,
  );
  assert.match(
    page,
    /function toggleAll[\s\S]*?if \(isBusy\) return;/,
  );
  assert.match(page, /min=\{1\}/);
  assert.match(page, /max=\{50\}/);
  assert.match(page, /method:\s*"POST"/);
  assert.match(page, /JSON\.stringify\(\{\s*urls:/);
  assert.match(page, /const MAX_SITES = 20/);
  assert.match(page, /phase.*"error"/s);
  assert.match(page, /disabled=\{isBusy\}/);
  assert.match(page, /ats-report\.json/);
  assert.match(page, /导出 JSON/);
  assert.match(layout, /lang="zh-CN"/);
  assert.doesNotMatch(page, /frontend_demo|window\.setTimeout/);
  assert.doesNotMatch(worker, /job-sources\/ats\/detect/);
  assert.doesNotMatch(page, /_sites-preview|SkeletonPreview/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);

  await assert.rejects(
    access(new URL("../app/_sites-preview", import.meta.url)),
  );
  await assert.rejects(
    access(new URL("../.env", projectRoot)),
  );
});

test("matches the backend's supported public job-board URLs", () => {
  const accepted = [
    ["greenhouse", "https://job-boards.greenhouse.io/wikimedia"],
    ["greenhouse", "https://boards.eu.greenhouse.io/acme/jobs"],
    ["lever", "https://jobs.lever.co/dnb"],
    ["lever", "https://jobs.eu.lever.co/acme/openings"],
    ["ashby", "https://jobs.ashbyhq.com/linear"],
    [
      "smartrecruiters",
      "https://careers.smartrecruiters.com/smartrecruiters",
    ],
    ["smartrecruiters", "https://jobs.smartrecruiters.com/acme/123-role"],
    ["workable", "https://apply.workable.com/clearstory"],
    ["beisen", "https://beisen.zhiye.com/jobs"],
    ["beisen", "https://group.campus.zhiye.com/"],
  ];
  for (const [ats, url] of accepted) {
    assert.equal(isCollectableCareerUrl(ats, url), true, url);
  }

  const rejected = [
    ["greenhouse", "https://careers.example.com/wikimedia"],
    ["greenhouse", "https://boards.greenhouse.io/"],
    ["greenhouse", "https://@job-boards.greenhouse.io/acme"],
    ["lever", "https://jobs.lever.co.attacker.example/acme"],
    ["lever", "https://jobs.lever.co/not%20safe"],
    ["ashby", "https://jobs.ashbyhq.com/"],
    ["ashby", "https://jobs.ashbyhq.com.attacker.example/linear"],
    ["smartrecruiters", "https://careers.smartrecruiters.com/"],
    ["smartrecruiters", "https://jobs.smartrecruiters.com.attacker.test/acme/1"],
    ["workable", "https://apply.workable.com/j/B4E3BB8DE1"],
    ["workable", "https://apply.workable.com.attacker.test/clearstory"],
    ["beisen", "https://future.jereh.com/form"],
    ["beisen", "https://zhiye.com/jobs"],
    ["beisen", "https://bad_label.zhiye.com/jobs"],
    ["beisen", "https://user@beisen.zhiye.com/jobs"],
    ["beisen", "https://:@beisen.zhiye.com/jobs"],
    ["beisen", "https://北森.zhiye.com/jobs"],
    ["beisen", "https://beisen.zhiye.com:444/jobs"],
    ["beisen", "http://beisen.zhiye.com/jobs"],
  ];
  for (const [ats, url] of rejected) {
    assert.equal(isCollectableCareerUrl(ats, url), false, url);
  }
});
