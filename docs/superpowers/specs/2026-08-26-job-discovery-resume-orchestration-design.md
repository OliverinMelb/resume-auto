# Boss / Nowcoder JD 联动与简历生成设计

日期：2026-08-26  
状态：口头设计已确认，待书面规范评审

## 1. 背景与目标

仓库现有能力分成两层：

- `origin/codex/boss-dom-resilience`、`origin/nowcoder` 等研究分支负责发现和采集真实岗位；
- `main` 的 `ResumeRewritePipeline` 负责把数据库中的真实 JD 与候选人简历交给本地 Codex，经过事实门禁后生成 ATS 友好 PDF。

当前缺口是两层没有形成稳定的自动联动。用户不应为了使用下游改写管线，再手工把已采集 JD 复制为 `target-jd.md`。

本设计增加一个主线编排器，将 Boss 与 Nowcoder 分支产生的真实 JD 归一化、去重、筛选并导入 `main`，随后支持两种简历模式：

1. **海投综合版**：从 Boss 与 Nowcoder 的同一 Agent 职业族样本中归纳高频要求，生成一份综合投递简历；
2. **定向版**：接受单个真实 JD 的文本、文件、URL 或数据库 `job_id`，按候选人画像生成单岗版本。

优化目标是提高 ATS 关键词覆盖和简历筛选匹配度，不承诺一定通过筛选。

## 2. 已选方案

采用“**主线编排器 + 本机分支适配器 + 统一 JD 契约**”。

各研究分支保留独立 worktree、依赖和本地服务。`main` 不直接导入分支内部 Python 模块，也不让多个版本共享或并发迁移同一个 SQLite 文件。主线只通过本机 HTTP/JSON 或显式 JSON 导出读取结果，再写入自己的 Repository。

未选方案：

1. **把所有研究分支直接合并进 `main`**：分支之间已有大量独立演化，硬合并会引入路由、依赖、数据库迁移和测试冲突；
2. **让多个 worktree 直接写同一个 SQLite**：不同分支的 schema 与生命周期不一致，存在迁移和并发破坏风险；
3. **继续手工复制 JD 文件**：无法形成真正联动，也丢失来源 URL、验证时间和采集状态。

## 3. 来源能力边界

### 3.1 Boss

- 使用 `codex/boss-dom-resilience` 的调研模式；
- 依赖用户已有的 Boss 登录态和 Tampermonkey 页面；
- 浏览器负责读取真实列表与详情，分支本地服务负责保存调研岗位；
- 主线编排器可以创建/选择调研项目、轮询已采集岗位并导入，但不绕过登录、验证码、平台限制或风控；
- 现有 Boss 投递模式可以消费综合版简历，但新 PDF 在首次投递前仍需成为 Boss 账号中可选的简历附件，并绑定正确的 `resumeIndex`。本期不自动上传附件。

### 3.2 Nowcoder

- 使用 `nowcoder` 分支的公开搜索或可恢复调研任务；
- 可由主线通过本机 API 创建任务、轮询状态并读取已采集岗位；
- 当前能力定位为 JD 发现与调研，不宣称已经具备可靠的自动投递；
- 遇到登录、验证页、详情不完整或公开接口变化时，必须返回降级状态，不能把摘要冒充完整 JD。

### 3.3 本期范围

首期只打通 Boss 与 Nowcoder。ATS、V2EX、Eleduck 以后可以实现相同适配器契约，但不进入本期交付。

## 4. 统一 JD 契约

所有来源先映射为 `DiscoveredJob`：

```text
DiscoveredJob
  source: "boss" | "nowcoder"
  source_job_id: str
  canonical_url: str
  title: str
  company: str
  location: str | null
  salary_raw: str | null
  detail: str
  detail_status: "complete" | "partial"
  search_keyword: str
  published_at: UTC datetime | null
  verified_at: UTC datetime
  score: float | null
  qualified: bool | null
  source_metadata: safe structured metadata
```

导入前必须满足：

- 标题和完整 JD 正文非空；
- `detail_status=complete`；
- 来源 URL 或来源岗位 ID 可追踪；
- `verified_at` 存在；
- 职业族属于 Agent / AI 应用 / RAG / 工作流等允许范围；
- 不含明显过期、关闭、实习误标社招或跨职业族岗位。

去重优先级：

1. 规范化 `canonical_url`；
2. `source + source_job_id`；
3. 规范化公司、标题和正文内容哈希。

导入 `main` 后继续使用现有 `jobs.id` 作为改写管线的唯一岗位引用。

## 5. 编排器接口

新增统一入口，批处理和 FastAPI 复用同一个核心服务：

```text
JobDiscoveryOrchestrator
  discover(request) -> DiscoveryRun
  import_results(run_id) -> list[job_id]
  build_broad_rewrite(run_id, candidate_id, resume_ids) -> RewriteRun
  build_targeted_rewrite(target, candidate_id, resume_ids) -> RewriteRun
```

`DiscoveryRequest` 包含：

- 来源列表，首期允许 `boss`、`nowcoder`；
- 搜索关键词，例如 `AI Agent`、`智能体开发`、`AI 应用工程师`；
- 城市、招聘类型和每来源上限；
- 目标职业族；
- Boss 调研项目 ID 或创建项目所需信息；
- 来源本机 endpoint，默认只允许 loopback 地址。

所有运行均写入受 Git 忽略的 `analysis/runs/<run_id>/`，保存安全快照、导入摘要、选岗理由和错误码；不保存登录凭据、Cookie 或页面会话。

## 6. 海投综合版流程

```text
Boss 调研结果 ─┐
               ├─> 归一化 ─> 完整性校验 ─> 去重 ─> 职业族过滤
Nowcoder 任务 ─┘                                      │
                                                      v
                                   高频要求聚合 + 代表 JD 选择
                                                      │
                                                      v
                                  ResumeRewritePipeline(job_ids=[...])
```

规则：

- 至少需要 3 个完整、同职业族的真实 JD；不足 3 个时不生成“综合版”，改为报告样本不足；
- 每批最多使用 20 个 JD，避免 prompt 无界增长；
- 代表 JD 兼顾来源、公司、职责完整度、新鲜度和关键词覆盖，不能只按单一评分取前 N；
- 已知跨职业族组合继续由现有多 JD 闸门阻断；
- 聚合只影响目标关键词、项目排序和表达重点，不把市场常见要求自动当成候选人已经具备的经历；
- 输出名默认采用 `姓名-Agent综合版-YYYYMMDD.pdf`，绝不覆盖原始 Markdown、原始 PDF 或先前投递版。

## 7. 定向版流程

定向目标允许四种输入：

- `job_id`；
- 本地 JD 文件；
- 直接 JD 文本；
- 可读取的真实岗位 URL。

URL 必须经过对应来源适配器读取并形成完整 `DiscoveredJob`。读取失败、只能得到摘要或岗位已关闭时，不进入改写。定向模式只使用该 JD，不混入综合样本的公司或岗位特有要求。

## 8. 经历状态与“待补齐验收”

候选经历分为三类：

```text
VERIFIED
  原始简历、代码、记录或用户确认可以支持的既有事实。

REFRAMED
  对 VERIFIED 事实做 ATS 强化表达；不得扩大职责、结果或指标。

IN_PROGRESS
  为弥补 JD 缺口而创建、正在完成且尚未验收的项目。
```

投递 PDF 每次只生成一份。允许包含独立章节：

```text
在建项目（待验收）
```

该章节必须遵守：

- 明确使用“在建”“计划验收”“正在实现”等现在进行时；
- 不写成任职经历，不伪造公司、客户、上线状态或生产结果；
- 未测量前不填写 QPS、准确率、节省比例、用户数等指标；
- 可以写已确定的技术栈、目标架构、当前已完成步骤和验收目标；
- 每条都绑定稳定 `gap_id` 和验收计划。

模型结果与事实门禁增加 `claim_status`。`IN_PROGRESS` 不能借由普通 `user_confirmed` 自动升级；只有完成验收并产生证据记录后，后续运行才能将其作为 `VERIFIED` 项目。

## 9. 验收升级

每个在建项目生成一条 `GapAcceptancePlan`：

```text
GapAcceptancePlan
  gap_id: str
  jd_requirements: list[str]
  project_scope: str
  required_artifacts: list[str]
  test_commands: list[str]
  acceptance_metrics: list[str]
  interview_topics: list[str]
```

验收输入必须包含：

- 项目或代码路径；
- 实际运行的测试命令和结果；
- 完成时间；
- 指标的具体测量记录；
- 用户确认及 UTC 时间戳。

验收只确认已经存在的证据。编排器不根据计划目标反推已完成结果，也不把未执行测试写成通过。

## 10. 每次运行产物

除唯一投递 PDF 外，生成：

- `claim-ledger.json`：每条简历内容的状态、简历来源、JD 来源和变更原因；
- `gap-roadmap.md`：待补项目、学习任务、实现顺序和验收标准；
- `ats-report.md`：修改前后关键词覆盖、缺失硬条件和选用 JD；
- `input-snapshot.json`、`audit.md`：沿用现有可重建运行审计；
- `discovery-report.md`：来源成功/失败、完整 JD 数、去重数、拒绝原因和代表 JD 选择结果。

终端输出继续递归脱敏手机号和邮箱。Cookie、Token、Boss/Nowcoder 登录信息不得写入任何产物。

## 11. 状态与错误处理

发现运行状态：

```text
CREATED -> COLLECTING -> READY_FOR_IMPORT -> IMPORTED
                     \-> PARTIAL
                     \-> FAILED
```

- Boss 等待浏览器调研时保持 `COLLECTING`，提供可恢复的 run ID；
- 单一来源失败而另一来源获得足够样本时，可进入 `PARTIAL` 并生成带降级说明的综合版；
- 总完整样本不足、所有来源失败或职业族不一致时，不生成 PDF；
- 重试必须复用 run ID、去重结果和来源 checkpoint；
- loopback endpoint 之外的来源服务默认拒绝连接。

## 12. CLI 与使用方式

建议扩展 `rewrite_resume.bat`：

```powershell
# 创建或恢复海投 JD 调研
.\rewrite_resume.bat discover --mode broad --sources boss nowcoder ...

# 样本足够后生成综合版
.\rewrite_resume.bat broad --discovery-run-id <id> --resume-path <path> ...

# 单 JD 定向版
.\rewrite_resume.bat targeted --jd-path <path> --resume-path <path> ...

# 验收一个在建项目并在下次改写时升级
.\rewrite_resume.bat accept-gap --run-id <id> --gap-id <id> --evidence-json <path>
```

CLI 返回紧凑 JSON 和稳定错误码。用户无需创建固定名称的 `target-jd.md`。

## 13. 测试与验收标准

实现必须测试：

1. Boss 与 Nowcoder payload 到 `DiscoveredJob` 的契约映射；
2. URL、来源 ID 和内容哈希三层去重；
3. partial、过期和跨职业族 JD 的拒绝；
4. 海投少于 3 个样本不生成 PDF；
5. 多来源部分失败时的 `PARTIAL` 降级；
6. 综合版只使用被选中的同职业族真实 JD；
7. 定向版不借用其他岗位的特有要求；
8. `IN_PROGRESS` 必须出现在独立“在建项目（待验收）”章节；
9. 未验收项目不能使用已完成语态、生产结果或无证据指标；
10. 验收证据完成 round-trip 后才能升级为 `VERIFIED`；
11. `claim-ledger.json` 与 PDF 每条内容状态一致；
12. 登录信息与个人联系方式不出现在 CLI、审计或 Git 跟踪文件；
13. 现有 184 个测试继续通过；
14. 生成 PDF 保持一至两页、中文可提取、无空白尾页且不覆盖既有文件。

## 14. 非目标

- 绕过招聘平台验证码、登录或风控；
- 把未完成项目写成既有工作经历；
- 自动生成虚假公司、客户、上线状态或业务指标；
- 首期合并 ATS、V2EX、Eleduck 的实现；
- 自动上传新 PDF 到 Boss 或自动在 Nowcoder 投递；
- 保证 ATS 或招聘方一定通过。
