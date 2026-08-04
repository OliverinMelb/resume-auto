"use client";

import { useMemo, useState } from "react";

import {
  ATS_PROVIDERS,
  ATS_PROVIDER_SET,
  type AtsProvider,
  isCollectableCareerUrl,
} from "./collectable-sites";

type DetectionStatus = "confirmed" | "candidate" | "unknown" | "blocked";

const ATS_NAMES = [
  "json_ld",
  ...ATS_PROVIDERS,
  "moka",
  "hotjob",
  "workday",
  "sap_successfactors",
  "unknown",
] as const;
type AtsName = (typeof ATS_NAMES)[number];
const ATS_NAME_SET = new Set<string>(ATS_NAMES);

type CareerSite = {
  id: string;
  company: string;
  url: string;
};

type AtsDetection = {
  ats: AtsName;
  confidence: number;
  evidence: string[];
  version_hint: string | null;
  supports_json_ld: boolean;
  requires_browser: boolean;
  blocked_reason: string | null;
};

type AtsBatchItem = {
  member_id: string;
  detection: AtsDetection;
};

type AtsBatchReport = {
  items: AtsBatchItem[];
  clusters: unknown[];
};

type ResultSite = {
  site: CareerSite;
  detection: AtsDetection;
};

type DetectionRunState =
  | { phase: "idle" }
  | { phase: "running" }
  | { phase: "error" }
  | {
      phase: "complete";
      report: AtsBatchReport;
      sites: CareerSite[];
    };

type AtsJob = {
  source: string;
  source_job_id: string;
  source_url: string;
  official_apply_url: string | null;
  title: string;
  company: string | null;
  locations: string[];
  recruitment_channel: string;
  employment_type: string | null;
  work_mode: string | null;
  score: number | null;
  qualified: boolean | null;
};

type AtsJobCollection = {
  ats: AtsProvider;
  jobs: AtsJob[];
  discovered_count: number;
  scanned_count: number;
  matched_count: number;
  complete: boolean;
  stop_reason: string;
};

type JobRunState =
  | { phase: "idle" }
  | { phase: "running" }
  | { phase: "error" }
  | { phase: "complete"; collection: AtsJobCollection };

type AtsBoard = {
  provider: AtsProvider;
  tenant: string;
  career_url: string;
  company: string | null;
};

type AtsBoardCatalog = {
  boards: AtsBoard[];
  crawl: string;
  discovered_at: string;
  cached: boolean;
};

type AtsSearchFailure = {
  provider: string;
  tenant: string;
  reason: string;
};

type AtsJobSearchReport = {
  jobs: AtsJob[];
  failures: AtsSearchFailure[];
  searched_boards: AtsBoard[];
  searched_count: number;
  succeeded_count: number;
  failed_count: number;
  discovered_count: number;
  matched_count: number;
};

type CatalogRunState =
  | { phase: "idle" }
  | { phase: "running" }
  | { phase: "error" }
  | { phase: "complete"; catalog: AtsBoardCatalog };

type AutomaticSearchState =
  | { phase: "idle" }
  | { phase: "running" }
  | { phase: "error" }
  | { phase: "complete"; report: AtsJobSearchReport };

const ATS_DETECTION_ENDPOINT =
  "http://127.0.0.1:8010/job-sources/ats/detect";
const ATS_JOBS_ENDPOINT = "http://127.0.0.1:8010/job-sources/ats/jobs";
const ATS_DISCOVERY_ENDPOINT =
  "http://127.0.0.1:8010/job-sources/ats/boards/discover";
const ATS_SEARCH_ENDPOINT =
  "http://127.0.0.1:8010/job-sources/ats/jobs/search";
const MAX_SITES = 20;

const presetSites: CareerSite[] = [
  {
    id: "wikimedia",
    company: "Wikimedia Foundation",
    url: "https://job-boards.greenhouse.io/wikimedia",
  },
  {
    id: "dnb",
    company: "Dun & Bradstreet",
    url: "https://jobs.lever.co/dnb",
  },
  {
    id: "beisen",
    company: "北森招聘",
    url: "https://beisen.zhiye.com/jobs",
  },
  {
    id: "tesla",
    company: "Tesla South Korea",
    url: "https://hire-r1.mokahr.com/social-recruitment/tesla/100000166/",
  },
  {
    id: "fotile",
    company: "方太集团",
    url: "https://fotile.zhiye.com/social",
  },
  {
    id: "cifi",
    company: "旭辉集团",
    url: "https://www.hotjob.cn/wt/cifi/web/index",
  },
  {
    id: "workday",
    company: "Workday",
    url: "https://workday.wd5.myworkdayjobs.com/Workday",
  },
  {
    id: "sap",
    company: "SAP",
    url: "https://jobs.sap.com/",
  },
];

function reviewStatus(detection: AtsDetection): DetectionStatus {
  if (detection.blocked_reason) return "blocked";
  if (detection.ats === "unknown" || detection.confidence < 0.5) {
    return "unknown";
  }
  if (detection.confidence >= 0.9) return "confirmed";
  return "candidate";
}

function isAtsName(value: unknown): value is AtsName {
  return typeof value === "string" && ATS_NAME_SET.has(value);
}

function isCollectableAts(
  value: AtsName,
): value is AtsProvider {
  return ATS_PROVIDER_SET.has(value);
}

function isCollectableSite(site: CareerSite, detection: AtsDetection): boolean {
  return (
    isCollectableAts(detection.ats) &&
    isCollectableCareerUrl(detection.ats, site.url)
  );
}

function isBatchReport(value: unknown, expectedItems: number): value is AtsBatchReport {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<AtsBatchReport>;
  if (
    !Array.isArray(candidate.items) ||
    candidate.items.length !== expectedItems ||
    !Array.isArray(candidate.clusters)
  ) {
    return false;
  }
  return candidate.items.every((item) => {
    const detection = item?.detection;
    return (
      typeof item?.member_id === "string" &&
      Boolean(detection) &&
      isAtsName(detection.ats) &&
      typeof detection.confidence === "number" &&
      Array.isArray(detection.evidence) &&
      detection.evidence.every((entry) => typeof entry === "string") &&
      typeof detection.supports_json_ld === "boolean" &&
      typeof detection.requires_browser === "boolean" &&
      (detection.version_hint === null ||
        typeof detection.version_hint === "string") &&
      (detection.blocked_reason === null ||
        typeof detection.blocked_reason === "string")
    );
  });
}

function isAtsJob(value: unknown): value is AtsJob {
  if (!value || typeof value !== "object") return false;
  const job = value as Partial<AtsJob>;
  return (
    typeof job.source === "string" &&
    typeof job.source_job_id === "string" &&
    typeof job.source_url === "string" &&
    typeof job.title === "string" &&
    Array.isArray(job.locations) &&
    job.locations.every((location) => typeof location === "string") &&
    (job.score === null || typeof job.score === "number") &&
    (job.qualified === null || typeof job.qualified === "boolean")
  );
}

function isJobCollection(value: unknown): value is AtsJobCollection {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<AtsJobCollection>;
  return (
    Boolean(candidate.ats && isCollectableAts(candidate.ats as AtsName)) &&
    Array.isArray(candidate.jobs) &&
    candidate.jobs.every(isAtsJob) &&
    typeof candidate.discovered_count === "number" &&
    typeof candidate.scanned_count === "number" &&
    typeof candidate.matched_count === "number" &&
    typeof candidate.complete === "boolean" &&
    typeof candidate.stop_reason === "string"
  );
}

function isBoardCatalog(value: unknown): value is AtsBoardCatalog {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<AtsBoardCatalog>;
  return (
    typeof candidate.crawl === "string" &&
    typeof candidate.discovered_at === "string" &&
    typeof candidate.cached === "boolean" &&
    Array.isArray(candidate.boards) &&
    candidate.boards.every(
      (board) =>
        typeof board?.provider === "string" &&
        isCollectableAts(board.provider as AtsName) &&
        typeof board.tenant === "string" &&
        typeof board.career_url === "string" &&
        (board.company === null || typeof board.company === "string"),
    )
  );
}

function isJobSearchReport(value: unknown): value is AtsJobSearchReport {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<AtsJobSearchReport>;
  return (
    Array.isArray(candidate.jobs) &&
    candidate.jobs.every(isAtsJob) &&
    Array.isArray(candidate.failures) &&
    candidate.failures.every(
      (failure) =>
        typeof failure?.provider === "string" &&
        typeof failure.tenant === "string" &&
        failure.reason === "collection_failed",
    ) &&
    Array.isArray(candidate.searched_boards) &&
    candidate.searched_boards.every(
      (board) =>
        typeof board?.provider === "string" &&
        isCollectableAts(board.provider as AtsName) &&
        typeof board.tenant === "string" &&
        typeof board.career_url === "string",
    ) &&
    typeof candidate.searched_count === "number" &&
    typeof candidate.succeeded_count === "number" &&
    typeof candidate.failed_count === "number" &&
    typeof candidate.discovered_count === "number" &&
    typeof candidate.matched_count === "number"
  );
}

const statusLabels: Record<DetectionStatus, string> = {
  confirmed: "已确认",
  candidate: "待复核",
  unknown: "未知",
  blocked: "已阻断",
};

export default function Home() {
  const [sites, setSites] = useState(presetSites);
  const [selectedIds, setSelectedIds] = useState(
    () => new Set(presetSites.map((site) => site.id)),
  );
  const [run, setRun] = useState<DetectionRunState>({ phase: "idle" });
  const [customUrl, setCustomUrl] = useState("");
  const [showCustom, setShowCustom] = useState(false);
  const [jobRun, setJobRun] = useState<JobRunState>({ phase: "idle" });
  const [jobKeyword, setJobKeyword] = useState("");
  const [jobLimit, setJobLimit] = useState(20);
  const [jobSiteId, setJobSiteId] = useState("");
  const [catalogRun, setCatalogRun] = useState<CatalogRunState>({
    phase: "idle",
  });
  const [automaticRun, setAutomaticRun] = useState<AutomaticSearchState>({
    phase: "idle",
  });
  const [automaticKeyword, setAutomaticKeyword] = useState("前端");
  const [automaticLimit, setAutomaticLimit] = useState(20);
  const [automaticMaxBoards, setAutomaticMaxBoards] = useState(20);
  const phase = run.phase;
  const isBusy =
    phase === "running" ||
    jobRun.phase === "running" ||
    catalogRun.phase === "running" ||
    automaticRun.phase === "running";

  const selectedSites = useMemo(
    () => sites.filter((site) => selectedIds.has(site.id)),
    [selectedIds, sites],
  );
  const resultSites = useMemo<ResultSite[]>(() => {
    if (run.phase !== "complete") return [];
    return run.report.items.flatMap((item, index) => {
      const site = run.sites[index];
      return site ? [{ site, detection: item.detection }] : [];
    });
  }, [run]);
  const supportedJobSites = useMemo(
    () =>
      resultSites.filter(({ site, detection }) =>
        isCollectableSite(site, detection),
      ),
    [resultSites],
  );
  const selectedJobSite =
    supportedJobSites.find(({ site }) => site.id === jobSiteId) ??
    supportedJobSites[0];

  const clusterCounts = useMemo(() => {
    const counts: Record<DetectionStatus, number> = {
      confirmed: 0,
      candidate: 0,
      unknown: 0,
      blocked: 0,
    };
    resultSites.forEach(({ detection }) => {
      counts[reviewStatus(detection)] += 1;
    });
    return counts;
  }, [resultSites]);

  function resetResults() {
    setRun({ phase: "idle" });
    setJobRun({ phase: "idle" });
  }

  function toggleSite(id: string) {
    if (isBusy) return;
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    resetResults();
  }

  function toggleAll() {
    if (isBusy) return;
    setSelectedIds(
      selectedIds.size === sites.length
        ? new Set()
        : new Set(sites.map((site) => site.id)),
    );
    resetResults();
  }

  async function discoverAutomaticBoards() {
    if (isBusy) return;
    setCatalogRun({ phase: "running" });
    try {
      const response = await fetch(ATS_DISCOVERY_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limit: 50, refresh: true }),
      });
      if (!response.ok) throw new Error("request_failed");
      const payload: unknown = await response.json();
      if (!isBoardCatalog(payload)) throw new Error("invalid_response");
      setCatalogRun({ phase: "complete", catalog: payload });
    } catch {
      setCatalogRun({ phase: "error" });
    }
  }

  async function runAutomaticSearch() {
    if (
      isBusy ||
      !automaticKeyword.trim() ||
      automaticLimit < 1 ||
      automaticLimit > 50 ||
      automaticMaxBoards < 1 ||
      automaticMaxBoards > 20
    ) {
      return;
    }
    setAutomaticRun({ phase: "running" });
    try {
      const response = await fetch(ATS_SEARCH_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          keyword: automaticKeyword.trim(),
          limit: automaticLimit,
          max_boards: automaticMaxBoards,
          refresh: false,
        }),
      });
      if (!response.ok) throw new Error("request_failed");
      const payload: unknown = await response.json();
      if (!isJobSearchReport(payload)) throw new Error("invalid_response");
      setAutomaticRun({ phase: "complete", report: payload });
    } catch {
      setAutomaticRun({ phase: "error" });
    }
  }

  async function runDetection() {
    if (!selectedSites.length || isBusy) return;
    const requestedSites = selectedSites;
    setRun({ phase: "running" });
    setJobRun({ phase: "idle" });
    try {
      const response = await fetch(ATS_DETECTION_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls: requestedSites.map((site) => site.url) }),
      });
      if (!response.ok) throw new Error("request_failed");
      const payload: unknown = await response.json();
      if (!isBatchReport(payload, requestedSites.length)) {
        throw new Error("invalid_response");
      }
      setRun({ phase: "complete", report: payload, sites: requestedSites });
    } catch {
      setRun({ phase: "error" });
    }
  }

  async function runJobCollection() {
    const target = selectedJobSite;
    if (!target || jobRun.phase === "running" || jobLimit < 1 || jobLimit > 50) {
      return;
    }
    setJobRun({ phase: "running" });
    try {
      const response = await fetch(ATS_JOBS_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: target.site.url,
          company: target.site.company,
          keyword: jobKeyword.trim() || null,
          limit: jobLimit,
        }),
      });
      if (!response.ok) throw new Error("request_failed");
      const payload: unknown = await response.json();
      if (!isJobCollection(payload)) throw new Error("invalid_response");
      setJobRun({ phase: "complete", collection: payload });
    } catch {
      setJobRun({ phase: "error" });
    }
  }

  function addCustomSite() {
    const value = customUrl.trim();
    if (
      isBusy ||
      sites.length >= MAX_SITES ||
      !/^https?:\/\/[^.\s]+\.[^\s]+/i.test(value)
    ) {
      return;
    }
    const id = `custom-${Date.now()}`;
    const site: CareerSite = {
      id,
      company: "自定义招聘官网",
      url: value,
    };
    setSites((current) => [...current, site]);
    setSelectedIds((current) => new Set([...current, id]));
    setCustomUrl("");
    setShowCustom(false);
    resetResults();
  }

  function exportReport() {
    if (run.phase !== "complete") return;
    const blob = new Blob([JSON.stringify(run.report, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "ats-report.json";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return (
    <main className="page-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">ATS</span>
          <div>
            <h1>企业招聘官网 ATS 检测</h1>
            <p>识别招聘系统、证据和访问限制</p>
          </div>
        </div>
        <span className="demo-badge">本地实时检测</span>
      </header>

      <section className="notice" aria-label="本地服务说明">
        <strong>需要启动本地 Python 服务。</strong>
        <span>
          点击检测会请求 127.0.0.1:8010，并按 robots 规则访问所选招聘官网。
        </span>
      </section>

      <section className="panel automatic-panel" id="automatic-ats-search">
        <div className="section-heading">
          <div>
            <span className="eyebrow">00 · 无需填写 URL</span>
            <h2>自动发现 ATS 公司</h2>
          </div>
          {catalogRun.phase === "complete" && (
            <span className="complete-message">
              {catalogRun.catalog.boards.length} 家 · {catalogRun.catalog.crawl}
              {catalogRun.catalog.cached ? " · 本地缓存" : " · 刚刚刷新"}
            </span>
          )}
        </div>

        <p className="automatic-copy">
          公开索引只负责发现租户，岗位内容直接读取 Greenhouse、Lever、Ashby、
          SmartRecruiters、Workable 和北森公开接口。
        </p>

        <div className="automatic-controls">
          <label>
            岗位关键词
            <input
              disabled={isBusy}
              maxLength={100}
              onChange={(event) => setAutomaticKeyword(event.target.value)}
              placeholder="前端、Java、产品经理…"
              value={automaticKeyword}
            />
          </label>
          <label>
            查询公司数
            <input
              disabled={isBusy}
              max={20}
              min={1}
              onChange={(event) => setAutomaticMaxBoards(Number(event.target.value))}
              type="number"
              value={automaticMaxBoards}
            />
          </label>
          <label>
            返回岗位数
            <input
              disabled={isBusy}
              max={50}
              min={1}
              onChange={(event) => setAutomaticLimit(Number(event.target.value))}
              type="number"
              value={automaticLimit}
            />
          </label>
          <button
            className="secondary-button"
            disabled={isBusy}
            onClick={discoverAutomaticBoards}
            type="button"
          >
            {catalogRun.phase === "running" ? "正在发现…" : "刷新公司目录"}
          </button>
          <button
            className="primary-button"
            disabled={
              isBusy ||
              !automaticKeyword.trim() ||
              automaticLimit < 1 ||
              automaticLimit > 50 ||
              automaticMaxBoards < 1 ||
              automaticMaxBoards > 20
            }
            onClick={runAutomaticSearch}
            type="button"
          >
            {automaticRun.phase === "running" ? "正在直采…" : "直接搜索公开岗位"}
          </button>
        </div>

        {catalogRun.phase === "error" && (
          <div className="inline-error">公司目录发现失败，请稍后重试。</div>
        )}

        {automaticRun.phase === "complete" && (
          <div className="automatic-summary">
            搜索 {automaticRun.report.searched_count} 家，成功 {automaticRun.report.succeeded_count}
            家，失败 {automaticRun.report.failed_count} 家；匹配
            {automaticRun.report.matched_count} 个，展示 {automaticRun.report.jobs.length} 个。
          </div>
        )}

        {automaticRun.phase === "complete" && automaticRun.report.jobs.length ? (
          <div className="table-wrap jobs-table">
            <table>
              <thead>
                <tr>
                  <th>公司 / 岗位</th>
                  <th>来源</th>
                  <th>地点</th>
                  <th>评分</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {automaticRun.report.jobs.map((job) => (
                  <tr key={`${job.source}-${job.source_job_id}`}>
                    <td>
                      <strong>{job.title}</strong>
                      <small>{job.company ?? "未知公司"}</small>
                    </td>
                    <td><code>{job.source}</code></td>
                    <td>{job.locations.join("、") || "未注明"}</td>
                    <td>
                      <span className={`score-pill ${job.qualified ? "qualified" : ""}`}>
                        {job.score === null ? "—" : job.score.toFixed(1)}
                      </span>
                    </td>
                    <td>
                      <a
                        className="job-link"
                        href={job.official_apply_url ?? job.source_url}
                        rel="noreferrer"
                        target="_blank"
                      >
                        查看岗位
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : automaticRun.phase !== "idle" ? (
          <div className={`job-empty ${automaticRun.phase}`}>
            <strong>
              {automaticRun.phase === "running"
                ? "正在从各公司 ATS 直接读取岗位"
                : automaticRun.phase === "error"
                  ? "自动搜索失败"
                  : "没有找到匹配岗位"}
            </strong>
            <p>发现服务只提供租户地址，JD 始终来自公司的公开 ATS 接口。</p>
          </div>
        ) : null}
      </section>

      <section className="panel source-panel">
        <div className="section-heading">
          <div>
            <span className="eyebrow">01 · 选择检测范围</span>
            <h2>已收录的企业招聘站</h2>
          </div>
          <button
            className="text-button"
            disabled={isBusy}
            type="button"
            onClick={toggleAll}
          >
            {selectedIds.size === sites.length ? "取消全选" : "全部选择"}
          </button>
        </div>

        <div className="site-list">
          {sites.map((site) => (
            <label
              className={`site-option ${
                selectedIds.has(site.id) ? "is-selected" : ""
              }`}
              key={site.id}
            >
              <input
                checked={selectedIds.has(site.id)}
                disabled={isBusy}
                onChange={() => toggleSite(site.id)}
                type="checkbox"
              />
              <span className="company-name">{site.company}</span>
              <span className="site-host">{new URL(site.url).hostname}</span>
              <span className="ats-preview">待检测</span>
            </label>
          ))}
        </div>

        <div className="actions">
          <button
            className="primary-button"
            disabled={!selectedIds.size || isBusy}
            onClick={runDetection}
            type="button"
          >
            {phase === "running"
              ? "正在检测…"
              : phase === "error"
                ? `重试检测（${selectedIds.size}）`
                : `检测选中网站（${selectedIds.size}）`}
          </button>
          <button
            className="secondary-button"
            disabled={isBusy || sites.length >= MAX_SITES}
            onClick={() => setShowCustom((value) => !value)}
            type="button"
          >
            添加其他官网 URL
          </button>
          {phase === "complete" && (
            <span className="complete-message">
              真实检测完成，共 {resultSites.length} 项
            </span>
          )}
        </div>

        {showCustom && (
          <div className="custom-url-row">
            <label htmlFor="custom-url">其他企业招聘官网</label>
            <div>
              <input
                id="custom-url"
                disabled={isBusy}
                onChange={(event) => setCustomUrl(event.target.value)}
                placeholder="https://careers.example.com"
                type="url"
                value={customUrl}
              />
              <button
                disabled={isBusy || sites.length >= MAX_SITES}
                onClick={addCustomSite}
                type="button"
              >
                添加
              </button>
            </div>
            <small>
              检测会调用本地 Python 服务并遵守 robots；当前 {sites.length} / {MAX_SITES}。
            </small>
          </div>
        )}
      </section>

      <section className="summary-grid" aria-label="检测摘要">
        {(
          [
            ["confirmed", "已确认"],
            ["candidate", "待复核"],
            ["unknown", "未知"],
            ["blocked", "已阻断"],
          ] as const
        ).map(([status, label]) => (
          <div className={`summary-card ${status}`} key={status}>
            <span>{label}</span>
            <strong>{clusterCounts[status]}</strong>
          </div>
        ))}
      </section>

      <section className="panel result-panel">
        <div className="section-heading">
          <div>
            <span className="eyebrow">02 · 查看结果</span>
            <h2>检测结果与证据</h2>
          </div>
          <button
            className="secondary-button"
            disabled={run.phase !== "complete"}
            onClick={exportReport}
            type="button"
          >
            导出 JSON
          </button>
        </div>

        {phase !== "complete" ? (
          <div className={`empty-state ${phase}`}>
            <strong>
              {phase === "running"
                ? "正在执行真实检测"
                : phase === "error"
                  ? "本地检测失败"
                  : "尚未开始检测"}
            </strong>
            <p>
              {phase === "running"
                ? "正在检查 robots、识别 HTML 特征并整理结果。"
                : phase === "error"
                  ? "请确认 Python 服务已在 127.0.0.1:8010 启动，然后重试。"
                  : "选择公司后点击“检测选中网站”。"}
            </p>
            {phase === "running" && <span className="progress-bar" />}
            {phase === "error" && (
              <button
                className="secondary-button retry-button"
                onClick={runDetection}
                type="button"
              >
                重试检测
              </button>
            )}
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>企业</th>
                  <th>ATS</th>
                  <th>置信度</th>
                  <th>状态</th>
                  <th>关键证据</th>
                  <th>浏览器</th>
                  <th>岗位获取</th>
                </tr>
              </thead>
              <tbody>
                {resultSites.map(({ site, detection }, index) => {
                  const status = reviewStatus(detection);
                  return (
                    <tr key={`${site.id}-${index}`}>
                      <td>
                        <strong>{site.company}</strong>
                        <small>{new URL(site.url).hostname}</small>
                      </td>
                      <td>
                        <code>{detection.ats}</code>
                      </td>
                      <td>
                        <div className="confidence">
                          <strong>{detection.confidence.toFixed(2)}</strong>
                          <span>
                            <i
                              style={{ width: `${detection.confidence * 100}%` }}
                            />
                          </span>
                        </div>
                      </td>
                      <td>
                        <span className={`status-pill ${status}`}>
                          {statusLabels[status]}
                        </span>
                      </td>
                      <td>
                        {detection.evidence.length ? (
                          <div className="evidence-list">
                            {detection.evidence.slice(0, 2).map((item) => (
                              <code key={item}>{item}</code>
                            ))}
                          </div>
                        ) : (
                          <span className="muted">暂无证据</span>
                        )}
                      </td>
                      <td>{detection.requires_browser ? "需要" : "不需要"}</td>
                      <td>
                        {isCollectableSite(site, detection) ? (
                          <button
                            className="table-action"
                            disabled={jobRun.phase === "running"}
                            onClick={() => {
                              setJobSiteId(site.id);
                              setJobRun({ phase: "idle" });
                            }}
                            type="button"
                          >
                            获取岗位
                          </button>
                        ) : (
                          <span className="muted">暂未支持岗位获取</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel jobs-panel" id="ats-job-panel">
        <div className="section-heading">
          <div>
            <span className="eyebrow">03 · 查找岗位</span>
            <h2>获取并评分公开岗位</h2>
          </div>
          {jobRun.phase === "complete" && (
            <span className="complete-message">
              匹配 {jobRun.collection.matched_count} 个，展示 {jobRun.collection.jobs.length} 个
            </span>
          )}
        </div>

        <div className="job-controls">
          <label>
            招聘站
            <select
              disabled={!supportedJobSites.length || jobRun.phase === "running"}
              onChange={(event) => {
                setJobSiteId(event.target.value);
                setJobRun({ phase: "idle" });
              }}
              value={selectedJobSite?.site.id ?? ""}
            >
              {!supportedJobSites.length && <option value="">等待检测结果</option>}
              {supportedJobSites.map(({ site, detection }) => (
                <option key={site.id} value={site.id}>
                  {site.company} · {detection.ats}
                </option>
              ))}
            </select>
          </label>
          <label>
            关键词（可选）
            <input
              disabled={jobRun.phase === "running"}
              maxLength={100}
              onChange={(event) => setJobKeyword(event.target.value)}
              placeholder="Python、数据、产品…"
              value={jobKeyword}
            />
          </label>
          <label>
            返回数量
            <input
              disabled={jobRun.phase === "running"}
              max={50}
              min={1}
              onChange={(event) => setJobLimit(Number(event.target.value))}
              type="number"
              value={jobLimit}
            />
          </label>
          <button
            className="primary-button"
            disabled={
              !selectedJobSite ||
              jobRun.phase === "running" ||
              jobLimit < 1 ||
              jobLimit > 50
            }
            onClick={runJobCollection}
            type="button"
          >
            {jobRun.phase === "running"
              ? "正在获取…"
              : jobRun.phase === "complete" || jobRun.phase === "error"
                ? "重新获取"
                : "获取岗位"}
          </button>
        </div>

        {jobRun.phase === "complete" && jobRun.collection.jobs.length ? (
          <div className="table-wrap jobs-table">
            <table>
              <thead>
                <tr>
                  <th>岗位</th>
                  <th>地点</th>
                  <th>用工 / 模式</th>
                  <th>评分</th>
                  <th>判断</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {jobRun.collection.jobs.map((job) => (
                  <tr key={job.source_job_id}>
                    <td>
                      <strong>{job.title}</strong>
                      <small>{job.company ?? selectedJobSite?.site.company}</small>
                    </td>
                    <td>{job.locations.join("、") || "未注明"}</td>
                    <td>
                      {job.employment_type ?? job.recruitment_channel}
                      {job.work_mode ? ` · ${job.work_mode}` : ""}
                    </td>
                    <td>
                      <span className={`score-pill ${job.qualified ? "qualified" : ""}`}>
                        {job.score === null ? "—" : job.score.toFixed(1)}
                      </span>
                    </td>
                    <td>{job.qualified ? "合格" : "未达线"}</td>
                    <td>
                      <a
                        className="job-link"
                        href={job.official_apply_url ?? job.source_url}
                        rel="noreferrer"
                        target="_blank"
                      >
                        查看岗位
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className={`job-empty ${jobRun.phase}`}>
            <strong>
              {jobRun.phase === "running"
                ? "正在读取公开岗位并计算匹配分"
                : jobRun.phase === "error"
                  ? "岗位获取失败"
                  : jobRun.phase === "complete"
                    ? "没有匹配的岗位"
                    : "检测到支持直采的 ATS 后可获取公开岗位。"}
            </strong>
            <p>
              {jobRun.phase === "error"
                ? "确认本地服务可用后，点击“重新获取”。"
                : "只读取公开列表，不登录，也不会自动投递。"}
            </p>
          </div>
        )}
      </section>

      <footer className="safety-note">
        <strong>检测边界</strong>
        <span>robots 优先</span>
        <span>仅同源重定向</span>
        <span>不登录、不绕过验证码</span>
        <span>只读取公开岗位、不自动投递</span>
      </footer>
    </main>
  );
}
