# 电鸭公开招聘 RSS 数据源规格

- Workflow：`20260730-155439-886308-rss`
- 阶段：`PLAN`
- 调研证据：`docs/research/eleduck-public-rss-2026-07-30.md`
- 共享契约状态：牛客 workflow 已关闭并冻结 `job_sources/contracts.py`，确认
  SHA256 为 `D6AEB7225810C4CAE3E1568F2B86EE8AD3D254B8A2D8CE5316F38279FED7181A`。
  本 workflow 不拥有且不修改该文件；电鸭特有的分类与游标留在来源内模型。

## 目标

在不影响 BOSS、牛客和现有数据的前提下，提供一个独立、匿名、只读的电鸭
RSS 数据源：

1. 只访问官方公开的 `https://eleduck.com/feed/latest.xml`，不抓 HTML 详情，
   不登录，不绕过验证。
2. 安全解析当前 RSS 窗口，保存 GUID、发布时间、原文链接和经过脱敏的公开
   排错字段。
3. 使用可解释、确定性的规则把帖子区分为招聘、求职、外包需求、找合伙人、
   广告、普通讨论和无法确定。
4. 只有高置信招聘帖进入 `NormalizedJob`；其他条目均可见但不进入 JD 调研，
   `uncertain` 单独进入调用方可承接的人工确认结果。
5. 支持调用方持有的增量游标和来源内去重；预览模式无持久化副作用。
6. 以来源内错误边界隔离网络、解析和上游变化，避免电鸭失败污染其他来源。

## 非目标

- 不抓取帖子详情页、招聘频道 HTML 或站外链接。
- 不实现登录、验证码处理、代理池、限流绕过或隐私采集。
- 不实现自动找 JD、定时调度、通知、审核 UI、自动联系或自动投递。
- 不修改 `main.py`、`web_script.js`、BOSS、牛客目录、牛客测试、现有数据库
  结构、部署配置、环境变量或密钥。
- 不由本 workflow 修改 `job_sources/contracts.py`、全局来源路由或统一持久化
  协调层。
- 不根据 RSS 条目缺失推断删除、下线或失效。
- 不接入真实 LLM，也不使用概率模型替代确定性分类。

## 已确认的上游事实

- 官方页脚公开 feed 为 `https://eleduck.com/feed/latest.xml`。
- 2026-07-30 匿名样本为 RSS 2.0、25 个最新混合帖子，不是招聘专属 feed。
- item 样本字段只有 `title`、`description`、`link`、`guid`、`pubDate`，没有
  分类、标签、作者或更新时间字段。
- 响应未提供 `ETag`、`Last-Modified` 或 `Cache-Control`；带条件头仍返回
  `200`，第一版不能依赖条件 GET。
- 当前样本 25/25 的 GUID 与链接相同且声明 `isPermaLink=true`；这只能证明
  当前窗口内可用，不能证明长期永久稳定。
- `description` 很可能在 200 字符附近截断。匿名详情访问会转入验证页，因此
  第一版将摘要视为不完整证据，不补抓详情。

## 领域词汇

### FeedItem

成功解析的一个 RSS item。它保留采集所需的来源身份和经过安全处理的公开文本，
但不是 JD，也不代表已通过分类。

### PostCategory

一个互斥的主分类：

- `recruitment`：雇主或招聘方发布的个人岗位。
- `job_seeking`：候选人主动求职或展示能力以寻找岗位。
- `outsourcing_request`：购买项目成果、团队服务或交付物的需求。
- `partner_seeking`：以共同创业、股权、投资或共同承担事业为核心的招募。
- `advertisement`：没有明确岗位或项目采购的产品、服务推广或导流。
- `discussion`：没有明确供给、招募或交易动作的问题、经验或观点交流。
- `uncertain`：主体不清、证据不足、多个类别同时成立或截断导致无法可靠判断。

分类结果必须包含命中的规则 ID 和简短理由。多个非 `uncertain` 类别同时达到
成立条件时，主分类必须为 `uncertain`，不得通过隐藏优先级强行选一个类别。

### AcceptedJob

同时命中明确招聘意图和岗位结构证据的高置信 `recruitment` 条目。只有
AcceptedJob 可以转换为 `NormalizedJob`。

### ReviewItem

主分类为 `uncertain` 的条目。它包含来源身份、脱敏摘要、候选类别和命中理由，
但不进入 JD 项目。第一版只随采集结果返回，不自建持久审核队列或 UI。

### NonJobItem

被确定性识别为求职、外包需求、找合伙人、广告或讨论的条目。它不进入 JD
项目，但仍随来源结果返回，不能静默丢弃。

### EleduckCursor

由调用方保存的来源游标。它保存一个有界的最近身份集合；身份优先使用非空 GUID，
否则使用验证后的 canonical URL。`pubDate` 只用于排序和诊断，不能单独作为
增量边界。

## 拟议模块边界

实现时根据实际内聚性保持最小文件数，预期结构为：

```text
job_sources/eleduck/
  __init__.py
  models.py
  rss.py
  classifier.py
  parser.py
  source.py
test_eleduck_source.py
```

- `models.py`：来源内的 feed item、分类结果、review/non-job item、游标和
  `EleduckCollectionResult`；其中 `accepted_jobs` 直接引用共享
  `NormalizedJob`，不得复制或重新定义它。
- `rss.py`：固定 URL 的低频 HTTP 读取、响应边界和安全 XML 解析。
- `classifier.py`：无 I/O 的确定性分类规则和命中理由。
- `parser.py`：字段校验、URL canonicalization、文本净化、脱敏和日期转换。
- `source.py`：拉取、解析、分类、去重、游标推进和共享契约适配。
- `__init__.py`：只导出稳定的电鸭来源入口和来源内错误类型。

第一版不创建 `router.py`：全局路由和 `main.py` 不在本 workflow 的授权路径内，
而来源适配器无需为了占位创建不可达 HTTP 路由。

## HTTP 与 XML 安全

1. 请求 URL 和最终响应 URL 必须是精确允许的 HTTPS feed URL。
2. 使用明确的 User-Agent、超时、最大响应大小和单次串行请求；不发送 Cookie、
   Token、Referer 或登录状态。
3. 仅接受成功响应和 XML 内容类型。重定向、非 2xx、超时、超限、错误内容类型
   或非法 XML 转换为电鸭来源内的类型化错误。
4. XML 中出现 `DOCTYPE` 或 `ENTITY` 时直接拒绝；不解析外部实体。
5. 不请求 item 中的链接。canonical URL 只允许
   `https://eleduck.com/posts/<validated-id>` 形态。
6. 不记录响应正文、请求头、用户文本或异常中的上游载荷；日志只记录来源、错误
   类型、计数和耗时。
7. 上游未来若提供 ETag/Last-Modified，必须先通过独立实测确认 `304` 语义，
   才能另行规划条件请求；第一版不伪造支持。

本模块不内置调度。生产轮询建议由未来协调层设置为 15--30 分钟并加入抖动与
指数退避。

## 字段处理与隐私

### 精确保留

- RSS GUID；
- canonical 原文链接；
- `pubDate` 原值和解析后的 UTC 时间；
- 抓取时间；
- 分类规则 ID、类别和理由。

### 只保留安全版本

`title` 和 `description` 必须先：

1. 去除 HTML，解码字符实体并规范化控制字符和空白；
2. 限制字段长度；
3. 脱敏邮箱、手机号、微信、QQ、疑似 Cookie/Token/Authorization 值；
4. 保留足以解释分类的非敏感文本。

分类器可以在单次调用内处理未持久化的原始 RSS 字符串，但返回值、日志、
`raw_data`、fixture 和持久化载荷均不得包含原始 XML、HTML、联系方式或疑似
凭据。

## 身份、去重与增量

单条身份键按以下顺序确定：

```text
("eleduck", non-empty GUID)
("eleduck", canonical URL)  # GUID 缺失时
```

- 同一 feed 内重复身份只产生一个条目。
- 调用方游标中已有的身份不作为新条目再次发出。
- 当前 feed 中所有有效身份可以更新 `last_seen_at`，但条目缺失不会产生删除
  或下线事件。
- 只有整个响应完成 HTTP 校验、XML 解析、item 校验和分类后，才返回推进后的
  游标。
- 任一整体失败保持输入游标不变。单个畸形 item 的策略必须是可观察的：记录
  安全计数并将该次结果标为 partial；不能假装整次完整成功。
- 游标集合必须有明确上限并覆盖多个当前窗口；具体上限在实现时以 fixture
  验证，不能无限增长。

## 确定性分类

分类器以标题和摘要中的“主体、动作、对象”分组证据判断，不能只看一个关键词。

### 招聘成立条件

必须同时满足：

1. 至少一个雇主招聘意图，例如招聘、直招、岗位、任职或投递；
2. 至少一个岗位结构证据，例如职责、任职要求、薪酬、全职/兼职、工作地点或
   明确职位名称；
3. 没有同时成立的求职、项目交付或合伙类别。

满足条件但摘要疑似截断时仍可接纳，输出的 JD 必须为
`detail_status="partial"`。

### 其他类别

- 求职需要候选人主体以及求职、可入职、简历或作品集等能力供给证据。
- 外包需求需要项目成果/团队服务对象以及交付、预算、报价或招标证据。
- 找合伙人需要合伙/共同创业主体以及股权、投资、融资或共同承担事业证据。
- 广告需要推广、自荐、优惠或导流证据，且不存在招聘、外包或合伙动作。
- 讨论需要问答、交流或观点证据，且不存在明确交易或招募动作。

零个类别成立、多个类别成立、主体不清或关键证据可能被截断时输出
`uncertain`。每条规则使用稳定 ID，测试直接断言规则 ID，保证行为可审计。

## NormalizedJob 映射

仅 AcceptedJob 映射；所有字段遵循“有证据才填充”：

- `source = "eleduck"`；
- `source_job_id` 使用非空 GUID；GUID 缺失时使用验证后的 canonical URL；
- `source_url` 使用验证后的 canonical URL；
- `title`、`description` 使用脱敏纯文本；
- `published_at` 从 `pubDate` 解析；
- `last_verified_at` 使用本次成功抓取时间；
- `detail_status = "partial"`；
- `raw_data` 只含本规格允许的安全字段；
- 公司、薪资、职责、要求、地点、雇佣类型等缺失值保持空，不从截断自然语言
  猜测。

如果未来共享契约明确允许从文本提取结构字段，应作为协调后的规格变更重新通过
grilling；本任务当前不提前实现。

## 采集结果与模式

来源内 `EleduckCollectionResult` 必须分离：

- `accepted_jobs`：可以交给共享 JD 协调层的招聘记录；
- `review_items`：等待人工确认的 uncertain；
- `non_job_items`：已确定的非招聘类别；
- 计数、partial/complete 状态、安全错误摘要和返回游标。

电鸭不复用共享 `JobQuery` 或 `CollectionResult` 作为采集 interface：前者要求
公开 RSS 不支持的 keyword/recruitment_channel，后者无法表达 review/non-job
items 与游标。这个来源内结果不是共享契约的复制品；唯一跨来源输出是其中的
共享 `NormalizedJob`。

预览模式是纯读取：不写数据库、不写文件、不保存游标。明确的 JD 调研模式由
统一协调层决定目标项目、保存游标并持久化 `accepted_jobs`；本 workflow 不修改
数据库或全局协调代码。

## 失败隔离

- 所有电鸭网络、HTTP、内容类型、XML、schema 和分类错误使用来源内错误类型，
  不修改全局状态，也不捕获后伪装成其他来源错误。
- 导入电鸭模块不得启动请求、线程、调度器或写文件。
- 共享协调层应逐来源捕获失败，使电鸭失败只产生电鸭失败结果；由于协调层不在
  本 workflow 的授权路径内，该集成验收必须在共享契约稳定后由协调方完成。
- 本地测试至少证明电鸭失败后，一个独立假来源仍能被调用并返回结果；不导入或
  修改 BOSS、牛客实现来制造隔离。

## 共享契约决定

冻结的 `job_sources/contracts.py` 不能直接表达本来源的全部语义：

- `JobQuery` 要求 keyword 和 recruitment_channel，而公开电鸭 feed 没有这些
  上游筛选能力；
- `CollectionResult` 只包含 jobs，没有 review/non-job items 或增量游标；
- `NormalizedJob.source_job_id` 为必填，但“GUID 缺失时用 canonical URL”
  的共享语义尚未确认；
- 跨来源的逐来源失败结果尚未在本任务可见的契约中表达。

因此，本任务采用深模块 seam：

1. `job_sources/contracts.py` 保持零修改；
2. `EleduckCollectionResult`、`EleduckCursor`、ReviewItem 与 NonJobItem 只存在于
   `job_sources/eleduck/`；
3. AcceptedJob 在 source 最后一步直接构造共享 `NormalizedJob`；
4. 不复用或包装成共享 `JobQuery/CollectionResult`；
5. 未来统一协调层只消费 `accepted_jobs`，真正的跨来源持久化和人工审核入口
   另行协调。

## 验收标准

1. 使用 fixture 正常解析 RSS 2.0，并在缺少可选字段时保持稳定。
2. GUID 去重优先；GUID 缺失时使用 canonical URL；重复运行同一游标不重复
   产出。
3. 整体失败不推进游标，条目离开 feed 窗口不产生删除或下线。
4. 招聘帖只有在招聘意图和岗位结构同时成立时进入 AcceptedJob。
5. 求职、外包需求、找合伙人、广告、讨论均有正例、负例和冲突测试。
6. 无证据、证据冲突和截断边界保留为 uncertain，并带可解释规则理由。
7. 高置信但摘要不完整的招聘帖映射为 partial JD，不虚构缺失字段。
8. 恶意 HTML、DOCTYPE/ENTITY、异常 Unicode、超长文本和非法 URL 被安全处理。
9. 返回值、日志和 `raw_data` 不包含 Cookie、Token、邮箱、手机号、微信/QQ
   或原始 HTML/XML。
10. 预览模式不写数据库、文件或游标；电鸭失败不阻止独立来源执行。
11. 不修改或导入耦合到 BOSS、牛客、`main.py`、数据库、部署和环境配置。
12. 共享契约适配只有在协调完成后实现，并直接使用共享
    `NormalizedJob`，不复制同名模型。

## 确定性验证

实现阶段按 TDD 逐个纵向 slice 执行：

```powershell
.venv\Scripts\python.exe -m unittest test_eleduck_source.py
.venv\Scripts\python.exe -m compileall -q job_sources\eleduck test_eleduck_source.py
git diff --check
```

共享契约稳定且并发窗口停止写入后，再运行 workflow 配置中的完整
`python-source-change` 检查矩阵。公网 live smoke 只验证固定 feed 的匿名可达性，
不替代 fixture 测试，也不因验证页失败而尝试绕过。

验证通过后使用独立 `code-review`，分别检查：

- Standards：路径所有权、安全输入、隐私、错误边界和无副作用；
- Spec：分类、游标、去重、partial、人工确认语义与所有非目标。

## 回滚

本 workflow 的实现保持在独立目录和单一测试文件中。若需回滚，在用户明确授权
后只删除：

- `job_sources/eleduck/`
- `test_eleduck_source.py`
- 本 workflow 自己的任务产物和电鸭调研文档（若用户也要求删除）

不回退、不覆盖任何 BOSS、牛客、共享契约或其他现存脏文件。
