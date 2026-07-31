"use client";

import { useMemo, useState } from "react";

type DetectionStatus = "confirmed" | "candidate" | "unknown" | "blocked";

type CareerSite = {
  id: string;
  company: string;
  url: string;
  ats: string;
  confidence: number;
  evidence: string[];
  versionHint: string | null;
  supportsJsonLd: boolean;
  requiresBrowser: boolean;
  blockedReason: string | null;
};

const presetSites: CareerSite[] = [
  {
    id: "wikimedia",
    company: "Wikimedia Foundation",
    url: "https://job-boards.greenhouse.io/wikimedia",
    ats: "greenhouse",
    confidence: 0.97,
    evidence: [
      "hostname:job-boards.greenhouse.io",
      "resource_host:job-boards.cdn.greenhouse.io",
    ],
    versionHint: null,
    supportsJsonLd: false,
    requiresBrowser: false,
    blockedReason: null,
  },
  {
    id: "dnb",
    company: "Dun & Bradstreet",
    url: "https://jobs.lever.co/dnb",
    ats: "lever",
    confidence: 0.97,
    evidence: ["hostname:jobs.lever.co", "resource_host:www.lever.co"],
    versionHint: null,
    supportsJsonLd: false,
    requiresBrowser: false,
    blockedReason: null,
  },
  {
    id: "tesla",
    company: "Tesla South Korea",
    url: "https://hire-r1.mokahr.com/social-recruitment/tesla/100000166/",
    ats: "moka",
    confidence: 0.97,
    evidence: [
      "hostname:hire-r1.mokahr.com",
      "resource_host:hire-r1-ats.mokahr.com",
    ],
    versionHint: null,
    supportsJsonLd: false,
    requiresBrowser: false,
    blockedReason: null,
  },
  {
    id: "fotile",
    company: "方太集团",
    url: "https://fotile.zhiye.com/social",
    ats: "beisen",
    confidence: 0.75,
    evidence: [
      "candidate_hostname:fotile.zhiye.com",
      "resource_host:stc.beisen.com",
    ],
    versionHint: null,
    supportsJsonLd: false,
    requiresBrowser: false,
    blockedReason: null,
  },
  {
    id: "cifi",
    company: "旭辉集团",
    url: "https://www.hotjob.cn/wt/cifi/web/index",
    ats: "hotjob",
    confidence: 0.97,
    evidence: ["hostname:www.hotjob.cn", "resource_host:hztp.hotjob.cn"],
    versionHint: null,
    supportsJsonLd: false,
    requiresBrowser: false,
    blockedReason: null,
  },
  {
    id: "workday",
    company: "Workday",
    url: "https://workday.wd5.myworkdayjobs.com/Workday",
    ats: "workday",
    confidence: 0.97,
    evidence: [
      "hostname:workday.wd5.myworkdayjobs.com",
      "resource_host:wd5.myworkdaycdn.com",
    ],
    versionHint: null,
    supportsJsonLd: false,
    requiresBrowser: true,
    blockedReason: null,
  },
  {
    id: "sap",
    company: "SAP",
    url: "https://jobs.sap.com/",
    ats: "sap_successfactors",
    confidence: 0.65,
    evidence: [
      "resource_host:rmkcdn.successfactors.com",
      "resource_host:performancemanager5.successfactors.eu",
    ],
    versionHint: null,
    supportsJsonLd: false,
    requiresBrowser: true,
    blockedReason: null,
  },
];

function reviewStatus(site: CareerSite): DetectionStatus {
  if (site.blockedReason) return "blocked";
  if (site.ats === "unknown" || site.confidence < 0.5) return "unknown";
  if (site.confidence >= 0.9) return "confirmed";
  return "candidate";
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
  const [phase, setPhase] = useState<"idle" | "running" | "complete">("idle");
  const [customUrl, setCustomUrl] = useState("");
  const [showCustom, setShowCustom] = useState(false);

  const selectedSites = useMemo(
    () => sites.filter((site) => selectedIds.has(site.id)),
    [selectedIds, sites],
  );
  const resultSites = phase === "complete" ? selectedSites : [];

  const clusterCounts = useMemo(() => {
    const counts: Record<DetectionStatus, number> = {
      confirmed: 0,
      candidate: 0,
      unknown: 0,
      blocked: 0,
    };
    resultSites.forEach((site) => {
      counts[reviewStatus(site)] += 1;
    });
    return counts;
  }, [resultSites]);

  function toggleSite(id: string) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    setPhase("idle");
  }

  function toggleAll() {
    setSelectedIds(
      selectedIds.size === sites.length
        ? new Set()
        : new Set(sites.map((site) => site.id)),
    );
    setPhase("idle");
  }

  function runDetection() {
    if (!selectedIds.size || phase === "running") return;
    setPhase("running");
    window.setTimeout(() => setPhase("complete"), 900);
  }

  function addCustomSite() {
    const value = customUrl.trim();
    if (!/^https?:\/\/[^.\s]+\.[^\s]+/i.test(value)) return;
    const id = `custom-${sites.length + 1}`;
    const site: CareerSite = {
      id,
      company: "自定义招聘官网",
      url: value,
      ats: "unknown",
      confidence: 0,
      evidence: [],
      versionHint: null,
      supportsJsonLd: false,
      requiresBrowser: false,
      blockedReason: null,
    };
    setSites((current) => [...current, site]);
    setSelectedIds((current) => new Set([...current, id]));
    setCustomUrl("");
    setShowCustom(false);
    setPhase("idle");
  }

  function exportReport() {
    const report = {
      mode: "frontend_demo",
      items: resultSites.map((site, index) => ({
        member_id: `item-${String(index + 1).padStart(6, "0")}`,
        detection: {
          ats: site.ats,
          confidence: site.confidence,
          evidence: site.evidence,
          version_hint: site.versionHint,
          supports_json_ld: site.supportsJsonLd,
          requires_browser: site.requiresBrowser,
          blocked_reason: site.blockedReason,
        },
      })),
    };
    const blob = new Blob([JSON.stringify(report, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "ats-demo-report.json";
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
        <span className="demo-badge">前端演示模式 · 未连接 Python</span>
      </header>

      <section className="notice" aria-label="演示说明">
        <strong>当前页面使用已调研样本演示操作。</strong>
        <span>
          点击检测不会访问互联网；后续接入 Python 模块后才会执行 robots
          检查与真实检测。
        </span>
      </section>

      <section className="panel source-panel">
        <div className="section-heading">
          <div>
            <span className="eyebrow">01 · 选择检测范围</span>
            <h2>已收录的企业招聘站</h2>
          </div>
          <button className="text-button" type="button" onClick={toggleAll}>
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
                onChange={() => toggleSite(site.id)}
                type="checkbox"
              />
              <span className="company-name">{site.company}</span>
              <span className="site-host">
                {new URL(site.url).hostname}
              </span>
              <span className="ats-preview">{site.ats}</span>
            </label>
          ))}
        </div>

        <div className="actions">
          <button
            className="primary-button"
            disabled={!selectedIds.size || phase === "running"}
            onClick={runDetection}
            type="button"
          >
            {phase === "running"
              ? "正在检测…"
              : `检测选中网站（${selectedIds.size}）`}
          </button>
          <button
            className="secondary-button"
            onClick={() => setShowCustom((value) => !value)}
            type="button"
          >
            添加其他官网 URL
          </button>
          {phase === "complete" && (
            <span className="complete-message">
              演示检测完成，共 {resultSites.length} 项
            </span>
          )}
        </div>

        {showCustom && (
          <div className="custom-url-row">
            <label htmlFor="custom-url">其他企业招聘官网</label>
            <div>
              <input
                id="custom-url"
                onChange={(event) => setCustomUrl(event.target.value)}
                placeholder="https://careers.example.com"
                type="url"
                value={customUrl}
              />
              <button onClick={addCustomSite} type="button">
                添加
              </button>
            </div>
            <small>仅添加到当前浏览器演示列表，不会发起网络请求。</small>
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
            disabled={phase !== "complete"}
            onClick={exportReport}
            type="button"
          >
            导出 JSON
          </button>
        </div>

        {phase !== "complete" ? (
          <div className={`empty-state ${phase}`}>
            <strong>
              {phase === "running" ? "正在整理检测结果" : "尚未开始检测"}
            </strong>
            <p>
              {phase === "running"
                ? "正在模拟 robots 检查、HTML 识别与聚类。"
                : "选择公司后点击“检测选中网站”。"}
            </p>
            {phase === "running" && <span className="progress-bar" />}
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
                </tr>
              </thead>
              <tbody>
                {resultSites.map((site) => {
                  const status = reviewStatus(site);
                  return (
                    <tr key={site.id}>
                      <td>
                        <strong>{site.company}</strong>
                        <small>{new URL(site.url).hostname}</small>
                      </td>
                      <td>
                        <code>{site.ats}</code>
                      </td>
                      <td>
                        <div className="confidence">
                          <strong>{site.confidence.toFixed(2)}</strong>
                          <span>
                            <i style={{ width: `${site.confidence * 100}%` }} />
                          </span>
                        </div>
                      </td>
                      <td>
                        <span className={`status-pill ${status}`}>
                          {statusLabels[status]}
                        </span>
                      </td>
                      <td>
                        {site.evidence.length ? (
                          <div className="evidence-list">
                            {site.evidence.slice(0, 2).map((item) => (
                              <code key={item}>{item}</code>
                            ))}
                          </div>
                        ) : (
                          <span className="muted">暂无证据</span>
                        )}
                      </td>
                      <td>{site.requiresBrowser ? "需要" : "不需要"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <footer className="safety-note">
        <strong>检测边界</strong>
        <span>robots 优先</span>
        <span>仅同源重定向</span>
        <span>不登录、不绕过验证码</span>
        <span>不抓取具体岗位</span>
      </footer>
    </main>
  );
}
