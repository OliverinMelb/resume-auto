# ATS 公开站点受控调研（2026-07-30）

## 方法与合规边界

- 访问日期：2026-07-30（Asia/Shanghai）。每类只选择 1 个样本或 1 个规范性参考，没有全网扫描。
- 仅使用公开搜索结果、厂商官方文档、企业第一方招聘页，以及对明确选定 URL 的普通 HTTP GET。未使用 Playwright，未登录，未调用页面内发现的接口，未复制 cookie、token、带凭证查询串或完整岗位正文。
- 对每个实际招聘样本先检查同主机 `robots.txt`；没有命中针对所选路径的禁止规则。随后只获取一次页面 HTML。若出现 noindex、登录、验证码、401/403/429，本轮约定立即停止该样本。
- “已证实”只表示 2026-07-30 的公开响应或厂商文档直接展示该事实；`version_hint`、`requires_browser` 与产品归属中没有官方明示的部分均标为推断。HTTP 状态和短签名会随厂商发布变化，不应固化为永久事实。
- “可作测试证据”区分外部 smoke/回归证据与 hermetic 单元测试 fixture。活站可以证明规则方向，但岗位数量、资源文件名和构建 hash 都不是稳定断言。

## 样本结果

### 1. `json_ld`

样本/规范参考：[Google Search Central 的 JobPosting 结构化数据说明](https://developers.google.com/search/docs/appearance/structured-data/job-posting)。

- 访问状态（已证实）：HTTP 可见、无需登录；它是官方规范页，不是实际招聘详情页，因此本轮没有把它当作 detector 的正样本。
- 规范信号（已证实）：Google 要求 `JobPosting` 结构化数据放在单个职位详情页，并要求该页可被 Googlebot 访问、不得被 `robots.txt`、`noindex` 或登录阻挡；类型语义也由 [Schema.org `JobPosting`](https://schema.org/JobPosting) 定义。
- hostname/resource/generator/JSON-LD：本参考页自身的 hostname 为 `developers.google.com`；本轮只将文档中的 `@type=JobPosting` 当规范信号，没有声称参考页自身包含可检测的 `application/ld+json`。
- `version_hint`：`None`；JSON-LD 类型不是 ATS 产品版本。
- `requires_browser`：`false`（仅对规范页的可见访问）；`blocked_reason=null`。
- 可作测试证据：不能作为 live positive fixture；可以作为解析语义、阻断政策和“只在单岗位页断言”的规范依据。当前 detector 对嵌套对象/数组中的精确 `@type="JobPosting"` 的处理方向与规范相符，但仍需一个经批准、可长期维护的实际职位详情页 smoke 样本。

### 2. `greenhouse`

样本：[Wikimedia Foundation 的 Greenhouse job board](https://job-boards.greenhouse.io/wikimedia)；企业归属由 [Wikimedia 第一方招聘页](https://wikimediafoundation.org/jobs/) 的职位链接交叉确认。Greenhouse 官方也说明托管 job board URL 的 root/token 结构，并公开 Job Board API 的 board token 与 `absolute_url` 语义：[托管 URL 说明](https://support.greenhouse.io/hc/en-us/articles/360020776251-Job-board-URL-for-Greenhouse-hosted-job-board)、[Job Board API](https://developer.greenhouse.io/job-board.html)。

- 访问状态（已证实）：GET 200，无跳转，`text/html; charset=utf-8`；没有 robots/noindex、登录、验证码或 403/429。`blocked_reason=null`。
- 公开短签名（已证实）：hostname `job-boards.greenhouse.io`；资源域 `job-boards.cdn.greenhouse.io`；SSR HTML 中可见 `job-card`/`opening` 结构、`window.__remixContext`；无 generator meta、无页面级 JobPosting JSON-LD。
- `version_hint`：页面公开 `LOTUS_GIT_COMMIT=f26283ef…` 已证实；将其解释为 renderer 构建标识是高置信度推断，不是公开语义版本。建议 detector 仍返回 `None`，不要把完整 commit 或变化的 asset hash 暴露为稳定版本。
- `requires_browser=false`：已证实 HTTP HTML 自带职位列表，浏览器不是读取列表的必要条件。
- 可作测试证据：可以做 hostname/resource/SSR marker 的外部 smoke；不能把岗位数量、asset 文件名或 commit 固化为 hermetic 单测。

### 3. `lever`

样本：[Dun & Bradstreet 的 Lever postings](https://jobs.lever.co/dnb)；[D&B 第一方 careers 页](https://www.dnb.com/en-us/about/careers.html) 的 “Find Your Ideal Position” 链接确认企业归属。Lever 官方开源文档明确 published postings 会公开出现在 `https://jobs.lever.co/<site>`：[Lever Postings API](https://github.com/lever/postings-api)。

- 访问状态（已证实）：GET 200，无跳转，`text/html; charset=utf-8`；没有 robots/noindex、登录、验证码或 403/429。`blocked_reason=null`。
- 公开短签名（已证实）：hostname `jobs.lever.co`；资源 `/js/list.js`、`/js/cookieconsent.min.js`、`/js/jquery-3.6.1.min.js`；HTML 可见 `posting-title`/job-card 类结构；无 generator meta、无页面级 JobPosting JSON-LD。
- `version_hint=None`：`jquery-3.6.1` 是第三方依赖版本，不是 Lever ATS 版本；`/js/list.js` 只能作为资源 marker。
- `requires_browser=false`：HTTP HTML 已含可读列表。`blocked_reason=null`。
- 可作测试证据：可以做 hostname、`/js/list.js` 和 `posting-*` 的外部 smoke；岗位正文和数量不应固化。

### 4. `moka`

样本：[Tesla South Korea 的公开 Moka 招聘页](https://hire-r1.mokahr.com/social-recruitment/tesla/100000166/)；公开高校就业页也把该地址列为 Tesla 招聘官网，例如 [SUNY Korea 的招聘公告](https://www.sunykorea.ac.kr/en/html/sub04/040404.html?GotoPage=1&mode=V&no=15f0f381c150dd7235ab2dc8a9e2f6b4)。Moka 官方把“招聘官网 API”定义为获取招聘官网与职位信息的接口，并公开使用 `api.mokahr.com`：[Moka API 文档](https://www.mokahr.com/docs/api/index.html)。

- 访问状态（已证实）：GET 200，最终 URL 不变，title `Tesla - South Korea`；`robots.txt` 返回 404，但页面本身没有 robots/noindex、登录、验证码或 403/429。404 表示未取得站点级 robots 规则，不等价于发现禁止。`blocked_reason=null`。
- 公开短签名（已证实）：hostname `hire-r1.mokahr.com`；资源域 `hire-r1-ats.mokahr.com`、`mokahr.com`、`moka-public.s3.ap-southeast-1.amazonaws.com`；存在 `id="app"`；无 generator meta、无页面级 JobPosting JSON-LD。
- `version_hint` 候选：URL surface `hire-r1`/`social-recruitment` 可用于低粒度 `career_site_r1` 聚类，这是低置信度推断，不是 Moka 官方版本号。当前稳定返回 `None` 比把 `r1` 宣称为产品版本更安全。
- `requires_browser=false`（中等置信度推断）：HTML 体量约 96 KB，并非只有空 app shell；但没有用浏览器渲染验证交互功能。
- 可作测试证据：可以做 `*.mokahr.com` hostname、`hire-r1-ats.mokahr.com` 资源和 app-root 的外部 smoke；不能把第三方 S3 资源或页面大小当稳定 fixture。

### 5. `beisen`

样本：[方太集团 `fotile.zhiye.com/social`](https://fotile.zhiye.com/social)。北森归属不是仅凭品牌猜测：[`bocichina.zhiye.com` 的北森招聘门户隐私政策](https://bocichina.zhiye.com/Portal/Apply/PrivacyPolicy) 明确称该招聘门户由北森云计算有限公司及关联公司开发；[北森官方产品页](https://www.beisen.com/product/recruitment/)也将招聘官网搭建列为招聘管理系统能力。

- 访问状态（已证实）：GET 200，title `方太集团招聘官网网申系统--社会招聘`；没有 robots/noindex、登录、验证码或 403/429。`blocked_reason=null`。
- 公开短签名（已证实）：hostname `fotile.zhiye.com`；资源域 `stc.beisen.com`、`stc-cms.beisen.com`；无 generator meta、无页面级 JobPosting JSON-LD。
- `version_hint=None`：`zhiye.com` 与 `stc-cms` 是公开 surface/resource 标记，不是官方版本。
- `requires_browser=false`：公开 HTTP HTML 可读，不是空壳。
- 可作测试证据：可以做 `*.zhiye.com` + `stc*.beisen.com` 的双信号 smoke。单独把全部 `*.zhiye.com` 当“官方 hostname”风险较高，建议要求 Beisen 资源域或北森门户隐私/版权 marker 之一作为第二信号。

### 6. `hotjob`

样本：[旭辉集团 `www.hotjob.cn/wt/cifi/web/index`](https://www.hotjob.cn/wt/cifi/web/index)；访问后公开重定向到同站 `/wt/cifi/web/index/social?brandCode=1`。同平台的 [伊利公开招聘页](https://yili.hotjob.cn/) footer 明示 “Powered by Dayee”，而旭辉样本自身 footer 显示 “WinTalent 招聘管理系统”，构成厂商/产品归属的一手公开证据。

- 访问状态（已证实）：GET 200；robots 200 且未见适用于样本路径的禁止规则；无 noindex、登录、验证码或 403/429。`blocked_reason=null`。
- 公开短签名（已证实）：hostname `www.hotjob.cn`；路径 `/wt/cifi/`；资源域 `hztp.hotjob.cn`；可见 WinTalent footer；无 generator meta、无页面级 JobPosting JSON-LD。
- `version_hint`：`/wt/` 可解释为 WinTalent surface（高置信度推断），但不是 `legacy` 版本证据；页面没有当前 detector 所需的 `data-hotjob-version="legacy"`，因此应返回 `None`。
- `requires_browser=false`：HTTP HTML 有可见职位页结构。
- 可作测试证据：hostname、`/wt/`、`hztp.hotjob.cn` 和 WinTalent/Dayee footer 适合外部 smoke；`brandCode` 值和租户路径不应固化。

### 7. `workday`

样本：[Workday 自有公开招聘站](https://workday.wd5.myworkdayjobs.com/Workday)；[Workday 第一方 Careers 页](https://www.workday.com/en-us/company/careers/overview.html) 的 Job Search/Explore Roles 直接链接该站，产品背景见 [Workday Candidate Experience 官方文档](https://doc.workday.com/workday-education/en-us/course-manuals/recruiting-for-administrators/introduction-to-workday-recruiting.html?toc=1)。

- 访问状态（已证实）：普通 GET 200，`text/html`；原始 HTML 约 8.5 KB，只有 bootstrap/OG shell，职位列表不在初始 HTML。没有 403/429、验证码或登录。`blocked_reason=null`。
- 公开短签名（已证实）：hostname `workday.wd5.myworkdayjobs.com`；canonical 同 URL；OG title `Careers at Workday`；bootstrap 中有 `tenant=workday`、`siteId=Workday`、`environment=PROD`、`isExternal=true`、`appName=cxs`、`cdnEndpoint=wd5.myworkdaycdn.com`；资源含 `/wday/asset/candidate-experience-jobs/cx-jobs.min.js`；无 generator meta、无首页 JSON-LD。
- `version_hint=None`：`wd5` 很可能是数据中心/分片，`plate=BMT216A` 很可能是分析资产构建标识；两者都是推断，不能当产品版本。
- `requires_browser=true`：若目标是职位列表或交互 DOM；只做 hostname/CXS 静态指纹无需浏览器。
- 可作测试证据：可以做 200 + CXS bootstrap/resource + browser-needed 的外部 smoke；不能断言无浏览器可读取职位内容。

### 8. `sap_successfactors`

样本：[SAP 自有招聘站 `jobs.sap.com`](https://jobs.sap.com/)；产品归属由 SAP 官方 [Recruiting Career Sites](https://help.sap.com/docs/successfactors-recruiting/setting-up-and-maintaining-sap-successfactors-recruiting/career-sites-for-sap-successfactors-recruiting) 与 [Career Site Builder](https://help.sap.com/docs/successfactors-recruiting/setting-up-and-maintaining-sap-successfactors-recruiting/configure-career-sites-using-career-site-builder) 文档确认。[SAP Site Configuration 文档](https://help.sap.com/docs/successfactors-recruiting/setting-up-and-maintaining-sap-successfactors-recruiting/site-configuration-in-career-site-builder-settings)还明确标准公开域可由 SAP 管理为 `jobs.hr.cloud.sap.com` 或 `jobs.hr.sapcloud.cn`，也允许企业自定义域，并说明部分搜索页配置会加 `noindex`。

- 访问状态（已证实）：普通 GET 200，页面可见 title `Jobs at SAP | SAP Careers`、关键词/地点控件和 `Loading...`，结果动态加载；没有 403/429、验证码或登录。`blocked_reason=null`。
- 公开短签名（已证实）：custom hostname `jobs.sap.com`；CSP/HTML 引用 `performancemanager5.successfactors.eu`、`rmkcdn.successfactors.com`；资源含 `/platform/js/j2w/` 与 `/platform/csb/`；无 generator meta、无首页 JSON-LD。
- hostname 规范（已证实）：`<tenant>.jobs.hr.cloud.sap.com`、`<tenant>.jobs.hr.sapcloud.cn`；custom domain 合法存在。当前 detector 只含 `jobs.hr.sapcloud.cn` exact host，而官方形式明确要求租户子域。
- `version_hint`：页面可见 `/platform/bootstrap/3.4.8_NES/` 及短 hash，但它们更像前端 bundle/hash（推断），不是 SuccessFactors 产品版本；仅在公开 CSB marker 存在时，`career_site_builder` 才适合作 coarse surface hint。
- `requires_browser=true`：动态职位结果需要渲染；只做 RMK/CSB 静态指纹不需要。
- 可作测试证据：可以做 custom-domain resource fingerprint、200 与 browser-needed 的外部 smoke；动态结果不是稳定 fixture。

## 已证实信号与低置信度推断

已证实：

- Greenhouse `job-boards.greenhouse.io`、Lever `jobs.lever.co`、Moka `hire-r1.mokahr.com`、北森资源 `stc*.beisen.com`、Hotjob/WinTalent `*.hotjob.cn`/`/wt/` 都在公开页面中实际出现。
- 这五个 live 样本均为 HTTP 200，未见 noindex、登录、验证码、401/403/429；Greenhouse、Lever、Moka、北森、Hotjob 样本均未见页面级 JobPosting JSON-LD 或 generator meta。
- Greenhouse 与 Lever 的列表数据直接存在于 HTTP HTML；北森与 Hotjob 也不是空 app shell。
- SAP 官方明确标准 CSB 域与 custom domain 并存，并明确某些搜索页可能配置 `noindex`。

仅推断或未证实：

- Greenhouse commit 是 renderer build、Moka `r1` 是 generation、Hotjob `/wt/` 是 surface，这些都不是厂商公开语义版本。
- Moka “无需浏览器”只针对列表 HTML 可读，不代表所有交互无需 JavaScript。
- `*.zhiye.com` 与北森强相关，但共享域单信号是否足够高置信度需要更多租户样本；本轮只确认“主机 + Beisen 资源”组合。
- Workday `wd5`/`plate` 与 SAP bootstrap/hash 都不是公开语义产品版本；本轮 generic JSON-LD live positive 仍未达到证据标准。

## 对当前 `detector.py` 的差距建议

1. **北森 hostname 覆盖缺口是已证实问题。** 当前仅把 `*.beisen.com` 当官方域，真实招聘入口可在 `*.zhiye.com`。不要无条件把整个 `zhiye.com` 提升到 0.97；建议新增“`*.zhiye.com` + `stc.beisen.com`/`stc-cms.beisen.com` 或公开北森门户 marker”的组合规则，至少可稳定从当前 0.55 资源候选提升为可审计候选。
2. **SAP 标准域规则与官方文档不一致。** 应覆盖租户形式 `*.jobs.hr.cloud.sap.com` 与 `*.jobs.hr.sapcloud.cn`。当前 `official_hosts=("jobs.hr.sapcloud.cn",)` 不会识别文档明示的租户子域，且完全遗漏 `jobs.hr.cloud.sap.com`。保留 custom-domain 只靠资源/marker 的低置信度路径。
3. **live 页面普遍没有 synthetic generator。** 本轮七个 live 页面都没有当前规则期待的 generator meta，因此 `version_hint` 基本都会是 `None`。应把 generator 当加分证据而非版本覆盖前提；不要为了填充 version_hint 把 asset hash、Git commit、jQuery 版本、租户路径或查询参数当产品版本。
4. **Greenhouse 资源域可更精确。** 当前 `resource_host_suffixes=("greenhouse.io",)` 能命中 `job-boards.cdn.greenhouse.io`，但范围也会接受所有 Greenhouse 子域。建议优先列举 `job-boards.cdn.greenhouse.io` 等已证实公共资源面，再保留广后缀为低权重兼容。
5. **Lever 可增加稳定结构 marker，但不要替代 hostname。** `/js/list.js` 与 `posting-*` 是本轮已见公开签名，适合作为 custom-domain 或 smoke 辅助证据；它们可能随前端发布改变，不能单独给高置信度。
6. **Moka `requires_browser` 不应只因 app root 为真。** 当前启发式还要求可见文本很短，这一保护是必要的；Tesla Moka 页面有 app root，但 HTTP snapshot 并非空壳。建议增加针对 SSR/预载职位结构的负向条件，并以 fixture 测试。
7. **Hotjob 的 `legacy` 规则缺少活站支持。** 已证实 `/wt/`、Hotjob 资源域和 WinTalent/Dayee footer，但没有 `data-hotjob-version="legacy"`。保留 `legacy` 只用于显式属性；可新增 `surface_hint="wintalent_wt"` 或仍返回 `None`，不要由 `/wt/` 推断“旧版本”。
8. **JSON-LD 需要实际 leaf-page smoke。** 当前精确匹配 `@type="JobPosting"` 是合理起点，但应补充大小写/完整 IRI 是否接受的产品决定，并用经批准的单岗位详情页做外部 smoke；列表页无 JSON-LD 不应被当作厂商“不支持 JSON-LD”的全局结论。
9. **保留合规短路。** SAP 官方文档明确某些搜索页会加 `noindex`，印证当前检测 noindex 后停止 body 证据升级的方向；但 caller 仍需在 fetch 前解析 robots，detector 本身不能从 HTML 代替 robots 检查。
10. **Workday custom/resource 证据可更深。** live 页证实 `/wday/asset/candidate-experience-jobs/cx-jobs.min.js`、`appName=cxs` 与 `cdnEndpoint=*.myworkdaycdn.com`。当前相对 `/wday/` 已能给低置信度候选，但解析公开 bootstrap 的 `appName=cxs`/`isExternal=true` 可以减少 custom-domain 误报；不要把 `wd5` 当版本。
11. **SAP custom domain 的 live 证据已确认。** `jobs.sap.com` 不会命中当前 official host，却有 `rmkcdn.successfactors.com`、`performancemanager5.successfactors.eu`、`/platform/j2w/`、`/platform/csb/` 多个一致信号。建议把 RMK/CSB 路径作为辅助证据；多个同厂商信号可以提升 custom-domain 候选，但仍应低于厂商托管 hostname。

## 样本计数与限制

- 实际 live 招聘样本：7（Greenhouse、Lever、Moka、Beisen、Hotjob、Workday、SAP SuccessFactors）。
- 官方规范参考：1（generic JSON-LD）；另有各厂商官方文档用于归属与域名交叉验证。
- 阻塞：没有样本触发 401/403/429、验证码、登录或 noindex。唯一异常是 Moka 样本 `robots.txt` 404；按“未发现规则但不扩大访问”的保守处理完成一次页面 GET。generic JSON-LD live positive 没有在时限内取得足够一手证据，因此明确标记为未证实，而非猜测。
