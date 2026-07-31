# V2EX “酷工作”岗位数据源规范

## Goal

在 `v2ex` 独立 worktree 中实现一个来源内闭合、只读、匿名的 V2EX JSON
Feed 数据源。它只访问官方固定地址
`https://www.v2ex.com/feed/jobs.json`，把每个新 item 明确返回为：

- 高置信 `recruitment` / `referral`：附带现有
  `job_sources.contracts.NormalizedJob`；
- `job_seeking` / `outsourcing` / `advertisement` / `discussion` /
  `uncertain`：来源内 review 结果；
- 单 item 结构损坏：来源内 invalid/review 结果，并把本次抓取标记为
  `partial`。

任何分类都不得因不适合规范化而被静默丢弃。

## Research baseline

实现依据为 `docs/research/v2ex-jobs-feed.md`：

- 2026-07-30 的一次匿名 GET 返回 HTTP 200，无重定向；
- `Content-Type` 为 `application/json;charset=utf-8`，没有
  `Content-Length`；
- 响应体约 286,226 UTF-8 字节，含 50 个 items；
- 顶层 `version` 为 `https://jsonfeed.org/version/1`；
- 50 个 item 均有 string `id`、`url`、`title`、`content_html`、
  `date_published`、`author`，其中 `id == url`，形式为
  `https://www.v2ex.com/t/{数字}`；
- `date_modified` 只在 11/50 项出现；`content_text` 未出现；
- 单次快照只能证明当前形状，不能把可选字段提升为长期必填，也不能把
  Feed 中消失解释为删除或岗位关闭。

## Existing behavior

- 共享模型只有 `job_sources.contracts.NormalizedJob`；本任务复用它，不复制、
  不修改共享模型。
- 当前没有 V2EX 包、全局路由、UI、调度器或数据库写入。
- `nowcoder` 基线在 `d9c2e8d` 上通过：
  `56 tests, 0 failures`。

## Owned paths

只允许修改：

- `job_sources/v2ex/`
- `test_v2ex_source.py`
- `docs/research/v2ex-jobs-feed.md`
- `workflow/tasks/v2ex-jobs-source/`

研究文档已经完成；实现不得修改电鸭、牛客、共享 contracts/router、
`main.py`、数据库、部署或环境配置。

## Public seam

测试和调用方只跨越以下来源内 interface：

```python
source = V2exJobSource(opener=optional_test_opener, now=optional_clock)
result = source.fetch(cursor=optional_v2ex_cursor)
```

公开来源内值对象：

- `V2exCursor`：调用方持有的、严格校验且有容量上限的已见身份集合；
- `V2exFetchResult`：`complete | partial | failed` 状态、逐 item outcomes、
  输入/输出 cursor、发现/重复/已见计数和安全错误码；
- `V2exItemOutcome`：身份、V2EX 原文链接、分类、置信度、
  `normalized | review | invalid` disposition、风险标签、明确文本信号、
  已脱敏纯文本和可选 `NormalizedJob`；
- 分类 literal：
  `recruitment | referral | job_seeking | outsourcing | advertisement |
  discussion | uncertain`。

`opener` 是 HTTP 测试 seam：生产默认值使用 stdlib HTTPS opener；测试 fake
只模拟响应状态、headers、最终 URL 和有界 `read()`，不 mock 来源内部函数。
模块导入和构造对象都不得请求网络或启动线程。

## Fetch and HTTP contract

1. 每次 `fetch` 最多一次 HTTP GET；不重试、不分页、不跟随 item/HTML URL。
2. URL 是固定 HTTPS 常量。请求不设置 Cookie、Authorization、Token 或登录态
   header；只设置保守的 `Accept` 和 `User-Agent`。
3. 设置 10 秒超时；最大响应体 1 MiB。即使没有 `Content-Length`，也只读取
   `limit + 1` 字节并在超限时失败。
4. 若存在 `Content-Length`，要求它是非负整数且不超过上限；错误或矛盾的长度
   作为失败处理。
5. 只接受 HTTP 200；403、429 和其他状态映射为不同的安全错误码。
6. 自动重定向后只接受最终 URL 仍精确等于固定 Feed URL；任何其他目标失败。
7. 媒体类型忽略参数后只接受 `application/json` 或
   `application/feed+json`。
8. 响应体按 UTF-8 严格解码并完整 `json.loads`；非 JSON、错误编码、超限、
   错误顶层均为整次失败。
9. 整次失败返回 `failed`，不返回 item，并令 `next_cursor == input_cursor`。
   错误结果和日志不得含正文、响应体、Cookie、Token 或联系方式。

## JSON Feed v1 parsing

- 顶层必须是 object；`version`、`title`、`items` 的类型必须正确，version
  只支持 `https://jsonfeed.org/version/1`。
- 最多处理 200 个 items；单文本字段和游标身份有独立长度上限。
- 未知字段忽略，不复制进 `raw_data`。
- 每个 item 必须是 object，并至少有 `content_html` / `content_text` 之一。
- string `id` 为主身份。缺少/无效 id 时，仅在 `url` 是合法
  `https://www.v2ex.com/t/{数字}` 原文链接时用 URL 回退，并把抓取标为
  partial；两者都不可用时仍返回一个 identity 为 `None` 的 invalid/
  uncertain outcome。
- `url` 与 id 分开校验。`source_url` 只保留合法 V2EX 主题 HTTPS 原文链接；
  不请求该链接。
- RFC 3339 `date_published` / `date_modified` 才能进入结构化结果；无效可选
  日期只使对应 item partial，不伪造时间。
- 第一次同身份 item 被处理；同一响应内重复和 cursor 已见身份通过计数显式
  报告。cursor 是调用方状态，不写文件/数据库；成功或 partial 时合并有效
  身份，失败时保持不变。游标容量固定，超出时保留最近 Feed 顺序内身份。
- 后续 Feed 缺席不生成删除、过期或关闭结果。

## HTML, privacy, and bounded text

- 使用 stdlib `HTMLParser` 提取纯文本；忽略 `script`、`style`、template 等
  非正文内容，不执行脚本、不加载图片/链接、不保留 HTML attributes。
- 合并块级换行、解码 entity、限制输入和输出长度；超长内容截断并给 item
  partial reason，不把截断补写成业务事实。
- 在分类、特征提取、review 文本和 NormalizedJob 构造之前，统一移除：
  邮箱、手机号、微信/vx、QQ 和显式联系方式值。
- 不使用 author 作为公司或身份依据；不把 author、原始正文或未知 Feed 字段
  放进 `raw_data`。
- 模块不记录帖子标题/正文；只允许记录安全枚举错误码和计数。实现可以完全
  不产生日志。

## Classification rules

分类只基于脱敏后的 title + 纯文本正文，使用确定性强信号：

1. `referral`：明确“内推/可内推”等标记，且有招聘动作和具体角色/岗位证据。
2. `recruitment`：明确“招聘/诚聘/招募/招聘岗位”等动作，且至少有具体角色，
   再有公司、地点、薪资、就业类型之一作为第二证据。
3. `job_seeking`：明确“求职/找工作/寻找工作机会/个人简历”等候选人意图。
4. `outsourcing`：明确“外包/项目需求/预算找开发/接单”等项目交付意图。
5. `advertisement`：明确“广告/推广/课程/培训/招聘服务”等销售推广意图。
6. `discussion`：明确讨论/请教/吐槽/经验交流意图，且没有上述强信号。
7. `uncertain`：信号不足，或相互冲突的强信号。

保守优先级：

- referral 与 recruitment 是兼容信号，明确内推时归 referral；
- job-seeking、outsourcing、advertisement 与招聘强信号冲突时归 uncertain，
  不规范化；
- 只有无冲突且满足双证据阈值的 recruitment/referral 是 high confidence；
- 其他全部 disposition=`review`（畸形为 `invalid`），`job=None`。

## Explicit feature extraction

只在原文有明确标签或短语时提取：

- `remote`：远程、remote、work from home、居家办公；
- `internship`：实习、internship；
- `part_time`：兼职、part-time；
- `contract`：合同工、contractor、短期合同；
- `foreign_company_or_team`：外企、海外/国外/国际团队或明确英文等价短语；
- `location`：`地点/工作地点/base` 标签后的短值，或 title 中明确地点括号；
- `salary_text`：`薪资/待遇` 标签值，或带货币/周期单位的明确区间；
- `deadline_text`：明确“截止/申请截止”标签后的 ISO 日期；否则不生成
  `expires_at`。

不使用 IP、作者、语言、时区或常识猜测公司、地点、薪资、国别、就业类型或
过期时间。

## Risk tags

- 所有有效帖子：`community_post`、`identity_unverified`；
- outsourcing 分类或弱外包信号：`possible_outsourcing`；
- 没有明确可解析截止日期：`missing_deadline`。

可以增加实现安全标签（例如正文截断），但不得省略适用的上述标签。

## NormalizedJob mapping

仅 high-confidence recruitment/referral 构造共享模型：

- `source="v2ex"`；
- `source_job_id` 使用 item.id，缺失时使用合法 URL 回退；
- `source_url` 保留合法 V2EX 原文链接；
- `official_apply_url=None`，绝不自动投递；
- title/description/company/location/salary 均来自脱敏后的明确文本；
- `recruitment_channel="internship"` 仅在实习明确时使用，否则
  `"social"`；
- `employment_type` 只在明确 full-time/intern/part-time/contract 时填；
- `work_mode="remote"` 只在 remote 明确时填；
- responsibilities/requirements 不从自由文本猜分段；
- `published_at` 只用合法 Feed 日期，`expires_at` 只用明确 deadline；
- `raw_data` 只包含 curated identity、分类、风险标签、显式布尔信号和合法
  日期，不含原文、author 或联系方式；
- `detail_status="partial"`，反映社区身份和岗位事实未被官方雇主验证。

## Non-goals

- 不登录、不抓主题页、不调用其他 V2EX 接口、不使用 Token。
- 不写数据库/文件，不新增 UI、全局路由、调度器、部署或环境变量。
- 不修改或复用电鸭代码，不提前抽取共享 UGC/Feed 模块。
- 不删除/过期本地岗位，不自动投递，不评估候选人，不调用全局 evaluator。

## TDD slices and verification

测试只通过公共 seam，按以下 tracer 顺序 red → green：

1. HTTP + 正常 JSON Feed + 最小 NormalizedJob。
2. 分类全矩阵和只规范化 high-confidence recruitment/referral。
3. HTML 纯文本、联系方式脱敏、超长正文、显式特征提取。
4. item 缺失可选字段、单条畸形 partial、重复与 cursor。
5. 非 JSON、超限、重定向、403、429、失败不推进 cursor。
6. import 无网络/线程，fetch 不写数据库或文件，共享模型身份复用。

最终验证至少执行：

```powershell
C:\Users\admin\Desktop\test\czc-good-job\.venv\Scripts\python.exe -m unittest test_v2ex_source.py
C:\Users\admin\Desktop\test\czc-good-job\.venv\Scripts\python.exe -m unittest discover -p "test_*.py"
C:\Users\admin\Desktop\test\czc-good-job\.venv\Scripts\python.exe -m compileall -q job_sources\v2ex test_v2ex_source.py
git diff --check
git status --short
```

## Acceptance criteria

- 所有用户列出的分类、HTTP、安全、隐私、游标和 partial 场景都有公共 seam
  测试并通过。
- 每个新 item 都有 outcome；非 high-confidence recruitment/referral 没有
  NormalizedJob。
- 个人联系方式不出现在 outcome、NormalizedJob、curated raw_data、错误文本或
  日志。
- 只复用 `job_sources.contracts.NormalizedJob`，没有复制共享模型。
- 导入无副作用，fetch 不写数据库/文件。
- 最终 Git 变更只落在 owned paths；不提交、不合并、不推送。
