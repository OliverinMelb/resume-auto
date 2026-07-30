# 电鸭 RSS 页面与 JD 调研持久化接入规格

- Workflow：`20260730-183005-392840-rss-jd`
- 阶段：`PLAN`
- 上游来源规格：`workflow/tasks/20260730-155439-886308-rss/spec.md`
- 调研证据：`docs/research/eleduck-public-rss-2026-07-30.md`

## 目标

把已经验证的只读 `EleduckRssSource` 接入本地 FastAPI JD 调研项目：

1. 提供与牛客风格一致、但符合 RSS 一次性读取语义的本地页面。
2. 预览公开 RSS，并分别展示高置信招聘、待确认和确定性非招聘。
3. 正式调研时重新拉取 RSS，复用共享评分器，并把高置信招聘幂等写入
   现有 SQLite `jobs` 表。
4. 使用来源内原子 JSON 保存增量游标、脱敏审核快照和人工决定。
5. 允许本地用户把待确认条目确认成招聘并入库，或确认成非招聘后退出
   待确认队列。
6. 通过来源内协调模块隐藏抓取、评分、项目解析、数据库投影和运行时状态
   的顺序约束。

## 非目标

- 不修改现有数据库结构、兼容迁移或索引。
- 不修改 `main.py`、`job_sources/contracts.py`、牛客、BOSS、ATS 或 V2EX。
- 不实现定时轮询、后台任务、任务历史、停止、恢复或每秒状态轮询。
- 不实现通知、自动找 JD、自动联系或自动投递。
- 不抓取电鸭 HTML 详情页，不登录，不绕过验证。
- 不把 `review_items` 或 `non_job_items` 塞入 `jobs` 表。
- 不接受客户端提交的 RSS 正文、分类结果、`raw_data` 或完整 job 对象。
- 不提供规则编辑、正文编辑、批量人工确认或推翻既有人工决定。
- 不承诺数据库级 exactly-once；现有表没有唯一约束。本模块只提供进程内串行和
  项目内逻辑幂等。

## 现有事实

- `main.py` 已挂载 `job_sources.router`，所以本任务无需修改 `main.py`。
- 牛客页面使用来源目录内单文件 HTML、内联 CSS/JS 和同源 FastAPI 路由。
- 现有 `jobs` 表可以保存 source、external job ID、标题、摘要、公司、评分、
  是否合格、搜索关键词和 fingerprint，但不能无损保存完整 `NormalizedJob`。
- `Repository._connect()` 可能创建数据库、迁移、WAL 和索引，因此 preview
  路径不能构造或调用 Repository。
- `create_job_if_absent()` 按 candidate + fingerprint 做应用层查重，没有数据库
  唯一约束。
- 电鸭 RSS 当前是约 25 条的有界混合流，单次 HTTP 超时 15 秒，不需要后台任务。
- ATS 和 V2EX 的当前 owned paths 均不包含 `job_sources/router.py`；该文件仍是
  共享 chokepoint，本任务只增加电鸭的 import 和 `include_router`。

## 领域语言

### Preview

一次无持久化的公开 RSS 读取。它不加载来源状态、不推进游标、不构造
Repository、不创建项目，也不写数据库或 `.runtime`。返回值只用于当前页面展示。

### Research Run

一次正式 JD 调研操作。它重新拉取 RSS，使用已保存游标只处理新 identity，评分并
写入高置信招聘，最后保存新游标和脱敏快照。

### Pending Review

确定性分类为 `uncertain`、尚无人工决定的来源条目。它不是 JD，不能写入 `jobs`。

### Review Decision

本地用户对一个来源 identity 的不可自动推翻决定：

- `recruitment`：构造 partial `NormalizedJob`，评分并幂等写入所选调研项目。
- `non_job`：不写 `jobs`，从待确认队列移除。

决定保存 identity、决定、时间、原分类证据、脱敏展示字段，以及招聘决定的目标
项目和持久化 job ID。第一版不记录用户账户，因为路由只允许本机请求且系统没有
审核用户模型。

### Source State

来源内运行时状态，包括有界 `EleduckCursor`、pending reviews、最近的确定性
non-job 快照和 review decisions。它是运行状态，不是 JD 数据库。

## 深模块与 seam

对路由和测试暴露一个来源内协调模块：

```python
class EleduckCoordinator:
    def preview(self) -> EleduckCollectionResult: ...
    def research(
        self,
        *,
        candidate_id: str | None,
        project_name: str | None,
    ) -> EleduckResearchOutcome: ...
    def list_reviews(self) -> EleduckReviewQueue: ...
    def decide_review(
        self,
        identity: str,
        *,
        decision: Literal["recruitment", "non_job"],
        candidate_id: str | None,
        project_name: str | None,
    ) -> EleduckReviewDecision: ...
```

内部 seam：

- `EleduckRssSource`：公开 feed 读取、解析、分类、游标计算。
- `EleduckStateStore`：来源状态的读取和原子替换。
- `EleduckResearchSink`：项目解析和 `NormalizedJob -> jobs` 有损投影。
- `evaluate_normalized_job`：共享确定性评分器；测试注入 fake evaluator。

协调模块拥有一个进程内锁，串行化 research 和 review decision。preview 不取得写锁
也不访问 store/sink。

## 来源状态格式

默认路径：

```text
.runtime/eleduck/state.json
```

JSON 使用固定 `schema_version = 1`，只包含允许列表：

- `cursor.seen_identities`，最多 256 个；
- `pending_reviews`，按 identity 唯一，最多 256 个；
- `non_job_items`，最近快照，最多 256 个；
- `decisions`，按 identity 唯一，最多 512 个；
- `last_research_at`。

条目字段只允许 identity、GUID、canonical URL、脱敏 title/description、原始和解析后
pubDate、分类、规则 ID、理由。禁止原始 XML/HTML、Cookie、Token、联系方式和任意
客户端字段。

保存流程使用同目录临时文件、flush 后 `os.replace`。读取时验证 schema、类型、长度
和上限；损坏或未知版本抛来源内安全错误，不把文件内容写入异常或日志。

## Preview 行为

`GET /job-sources/eleduck/preview`：

1. 直接执行 `source.collect(cursor=None)`。
2. 不加载 state store，不构造 Repository，不评分，不保存任何返回值。
3. 返回 accepted/review/non-job、计数、complete/stop reason 和只用于诊断的
   `next_cursor`；服务端不会持久化该 cursor。
4. HTTP、XML 或来源错误映射为不含上游载荷的 `502`。
5. partial 结果可以展示，但明确标记“不完整”，不得通过 preview 写入。

页面初始加载不请求候选项目列表；用户进入调研区域或主动点击“加载调研项目”时才
调用现有 `/resume-agent/v1/candidates`。

## Research 行为

`POST /job-sources/eleduck/research` 请求只允许：

```json
{
  "candidate_id": "optional-existing-id",
  "project_name": "optional-new-name"
}
```

执行顺序：

1. 在进程内锁内加载来源状态。
2. 使用保存的 cursor 重新拉取并分类 RSS。
3. 若结果 `complete == false`，返回安全的 partial failure；不创建项目、不写
   `jobs`、不保存状态。
4. 对每个 accepted job 调用共享评分器；评分失败使整次操作在持久化前失败。
5. 仅在至少有一个待写 accepted job 时解析项目：
   - candidate ID 存在：验证它存在；
   - 否则 project name 非空：创建该名称的 candidate；
   - 两者都空：创建 `电鸭 JD 调研：YYYY-MM-DD`。
6. 使用 `SHA256("eleduck:" + source_job_id)` 作为 fingerprint，通过
   `create_job_if_absent()` 按项目内逻辑去重。
7. 所有 DB 写入成功后，把新 cursor、合并后的 pending review、最新 non-job
   快照和时间原子保存。
8. 若 DB 已写入但 state 保存失败，返回失败且旧 cursor 保持不变；下一次重放由
   fingerprint 去重，不会漏 JD。
9. 当前 feed 缺少旧 identity 不产生删除、下线或失效事件。

当没有新 accepted job 时不自动创建空项目；响应的 candidate ID 可以为空。指定了
不存在的 candidate ID 时返回 `400`。

## `jobs` 投影

不修改 schema，沿用牛客研究 sink 的语义：

- `external_job_id = source_job_id`
- `source = "eleduck"`
- `title = title`
- `detail = description`，为空时不写该 job
- `company = ""`
- `salary = ""`
- `score / qualified = 共享评分结果`
- `search_keyword = "电鸭 RSS"`
- `job_fingerprint = SHA256(source + ":" + source_job_id)`
- `source_logged_at = 当前 UTC 时间`

URL、pubDate、分类证据和完整 `raw_data` 保留在来源运行时快照，不偷塞进 `detail`
或现有无关字段。现有 DB 是有损 JD 投影，这一点必须在页面说明。

## 人工确认

`GET /job-sources/eleduck/reviews` 返回：

- `pending`
- `confirmed_recruitment`
- `ignored_non_job`

`POST /job-sources/eleduck/reviews/decision` 请求只允许：

```json
{
  "identity": "guid:... or url:...",
  "decision": "recruitment | non_job",
  "candidate_id": "required only when selecting an existing project",
  "project_name": "optional"
}
```

服务端取数规则：

1. 优先从可信 pending snapshot 按完整 identity 查找。
2. snapshot 中不存在时，可重新拉取当前 RSS 并只接受仍分类为 uncertain 的同一
   identity。
3. 两处都不存在时返回 `404`；不得使用客户端回传内容补齐。

决定规则：

- 已存在同一决定：幂等返回，不重复写 job。
- 已存在相反决定：返回 `409`；V1 不支持推翻。
- `non_job`：只原子保存决定并移出 pending。
- `recruitment`：使用安全来源字段构造 partial `NormalizedJob`，在 `raw_data`
  明确记录原分类和 `manual_review_decision="recruitment"`，评分、解析项目、幂等
  写 job，然后保存决定和 job ID。
- DB 成功而 state 保存失败时，重试仍通过 fingerprint 找到同一 job，不重复创建。

## 页面

新增来源内 `ui.html`，路由为 `/job-sources/eleduck/ui`。页面使用内联 CSS/JS、
`Cache-Control: no-store` 和与牛客相同强度的 CSP；所有来源文本使用 DOM
`textContent`，禁止 `innerHTML`。

页面区域：

1. 来源说明：公开混合 RSS、摘要可能截断、预览零写入、正式调研有损入库。
2. Preview：按钮、loading/error、计数、accepted/review/non-job 三组卡片。
3. Research：按需加载已有项目、项目名称输入、运行按钮、写入/去重/游标结果。
4. Reviews：pending、confirmed、ignored 三个视图；pending 支持单条确认招聘或
   非招聘。

页面不自动轮询，不自动拉取 RSS，不自动加载项目，不在 localStorage 保存 RSS
正文或审核载荷。

## 路由与错误

新增 `job_sources/eleduck/router.py`：

- `GET /ui`
- `GET /preview`
- `POST /research`
- `GET /reviews`
- `POST /reviews/decision`

全部路由复用 `Depends(require_local_request)`。`job_sources/router.py` 只增加电鸭
router 的 import 和 include；不修改 `main.py`。

错误映射：

- 请求字段或项目错误：`400`
- identity 不存在：`404`
- 相反人工决定：`409`
- 电鸭 HTTP/XML/state/评分/持久化错误：安全 `502` 或 `500`，响应和日志不包含
  RSS 正文、联系方式、Token、Cookie、数据库路径或 SQL。

## 并发与失败语义

- research 和 review decision 在单进程内串行，避免同一 state 文件丢更新。
- preview 可以并发执行，但每个请求只产生一次固定 URL GET。
- 不声称跨进程 exactly-once；若未来运行多个 worker，必须先增加数据库唯一约束或
  使用受控写事务，再修改部署配置。
- 电鸭失败只影响当前电鸭请求；聚合 router 不捕获或改变牛客、BOSS、ATS、V2EX
  的状态。

## 验收标准

1. `/ui` 使用本机限制、no-store、CSP；页面不包含 `innerHTML`、自动轮询或自动
   RSS 请求。
2. preview 正常展示三类结果，且 spy 证明不调用 state store、Repository、sink、
   evaluator 或任何文件写入。
3. preview 后 research 仍处理同一批帖子；客户端不能提交 job/RSS/分类字段。
4. research 使用保存 cursor，只写新 accepted jobs；重复运行不重复写项目内 JD。
5. research partial、抓取失败、评分失败时不创建项目、不写 DB、不推进 cursor。
6. DB 写成功后才保存 cursor；state 保存失败后的重试不重复 JD。
7. 已有项目、新项目和默认日期项目三种归属均通过；无新 JD 时不创建空项目。
8. accepted job 复用共享评分，按允许字段有损投影到现有 `jobs`，schema 零修改。
9. pending review 跨重启保留；确认招聘、确认非招聘、重复同决定和相反决定均有测试。
10. 人工确认只按服务端 identity 取数，不接受客户端正文；无法重取时拒绝。
11. state JSON 原子保存、版本和上限校验、损坏文件安全失败、脱敏允许列表通过。
12. RSS 缺失不产生删除；游标、pending、决定集合均有明确上限。
13. 所有响应、state、日志和 HTML 不输出 Cookie、Token、联系方式、原始 XML/HTML
    或数据库路径。
14. 电鸭错误不影响独立假来源；牛客、BOSS、ATS、V2EX、contracts 和 main 均零修改。
15. 目标测试、compileall、全仓测试、diff-check、workflow 验证和双轴独立审查通过。

## 确定性验证

```powershell
.venv\Scripts\python.exe -m unittest test_eleduck_source.py
.venv\Scripts\python.exe -m compileall -q job_sources\eleduck test_eleduck_source.py
.venv\Scripts\python.exe -m unittest discover -p test_*.py
git diff --check
```

## 回滚

本任务只在以下业务路径新增或修改：

- `job_sources/eleduck/`
- `test_eleduck_source.py`
- `job_sources/router.py`

回滚时在用户明确授权后移除新增页面、路由、协调器、state/sink 代码和对应测试，并从
聚合 router 删除电鸭的两行挂载。运行时 `.runtime/eleduck/` 不纳入 Git；如需删除，
必须先确认精确路径，并说明其中审核状态不可从 Git 恢复。不得回退或覆盖其他来源、
共享契约、数据库文件或现有用户数据。
