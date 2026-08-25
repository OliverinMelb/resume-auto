# 本地 Codex 简历定制流水线设计

日期：2026-08-26
状态：已完成口头设计确认，待书面规范评审

## 1. 目标

在现有 `jd-prompt` 的候选人、原始简历、岗位、调研报告和事实约束 prompt 之上，增加一条可实际产出定制简历的本地流水线。用户既可以从批处理脚本启动，也可以调用本机 FastAPI；两种入口必须复用同一个深模块，不能各自实现一套改写逻辑。

首个真实用例是：

- 原始可编辑简历：`C:\Users\Administrator\Desktop\caoshenao\曹申傲-AI-Agent简历-修改版.md`
- 现有 PDF：`C:\Users\Administrator\Desktop\caoshenao\output\pdf\曹申傲-AI-Agent简历-修改版.pdf`
- 产物命名：`曹申傲-AI-Agent简历-<公司或岗位>-定制版.pdf`
- 原始 Markdown 和 PDF 均不得覆盖。

成功标准：给定一份或一组真实 JD 后，单条命令能完成导入、证据整理、调用本地 Codex CLI、事实审计、Markdown 生成、ATS 友好 PDF 排版和渲染检查；证据不足时必须停在 `NEEDS_FACTS`，不得产生可投递 PDF。

## 2. 范围与非目标

本期包含：

- 复用 `job_decisions.jsonl`、`POST /jobs` 和手工 JD 文件作为岗位来源；
- 复用数据库中的候选人、原始简历和研究报告能力；
- 使用已安装的 `$improving-agent-resumes` 技能约束审阅与改写；
- 通过本机已登录的 Codex CLI 调用模型，不新增 OpenAI API Key；
- 固定 JSON Schema、事实门禁、运行状态、审计文件和 PDF 质量检查；
- 提供批处理入口与本机 HTTP 入口。

本期不包含：

- 自动登录或自动抓取 BOSS 直聘；现有脚本继续负责产生岗位判断日志；
- 自动投递、自动发送简历或修改招聘网站数据；
- Web 前端、分布式任务队列、并发调度和云端部署；
- OCR 修改原 PDF；流水线以可编辑 Markdown 为事实文本来源，重新排版生成新 PDF；
- 为匹配 JD 而生成候选人没有证据的经历、技能或指标。

`analysis/` 目前保存的是项目分析文档，不是 JD 数据源。新增的 `analysis/runs/` 仅保存每次运行的可审计产物，并加入 Git 忽略；不会改动或提交现有 `analysis/` 文件。

## 3. 已选方案

采用“本地 Codex CLI 适配器 + 单一流水线深模块”。

未选方案及原因：

1. 直接调用 OpenAI API：需要新增密钥管理，与“复用本机 Codex 登录态”的选择不符。
2. 只生成 prompt 后人工复制粘贴：无法稳定执行 JSON 校验、事实门禁、PDF 排版和审计，脚本之间也没有形成闭环。
3. CLI 和 FastAPI 分别编排：会复制状态迁移、错误处理和事实校验，长期容易出现两个入口行为不一致。

## 4. 架构与模块

### 4.1 外部入口

```text
rewrite_resume.bat ──> CLI 适配层 ─┐
                                  ├──> ResumeRewritePipeline.run(...)
FastAPI /rewrite-runs ─> 路由层 ──┘
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              v                           v                           v
       Repository/研究能力          CodexRunner 端口            ResumeRenderer 端口
                                      │                           │
                              CodexCliAdapter              ReportLabAdapter
                              FakeCodexAdapter              FakeRenderer
```

`ResumeRewritePipeline` 是唯一对入口公开的深模块。它隐藏以下实现复杂度：输入归一化、所有权校验、调研报告生成、快照、prompt 拼装、Codex 进程调用、结构化结果校验、事实门禁、状态迁移、文件原子写入、Markdown 输出、PDF 生成及验收。

两个真实 seam 只放在会变化或需要替换测试的外部依赖处：

- `CodexRunner`：生产使用 `CodexCliAdapter`，测试使用 `FakeCodexAdapter`；
- `ResumeRenderer`：生产使用 `ReportLabAdapter`，测试使用 `FakeRenderer`，集成测试再覆盖真实渲染。

数据库 Repository 沿用现有实现，不在公共流水线 interface 中泄漏 SQL 或表结构。CLI 的文件导入属于入口适配，不让路径处理污染核心请求。

### 4.2 建议文件归属

- `resume_agent/rewrite_pipeline.py`：深模块、状态机和事实门禁；
- `resume_agent/codex_runner.py`：Codex 端口、CLI 适配器、结果 Schema 调用；
- `resume_agent/resume_renderer.py`：受限 Markdown 到 PDF 的渲染及验收；
- `resume_agent/rewrite_schemas.py`：请求、结果、八段输出和运行状态模型；
- `resume_agent/rewrite_cli.py`：命令行解析、路径导入和最终文件导出；
- `resume_agent/codex_resume_result.schema.json`：传给 `codex exec --output-schema` 的静态 Schema；
- `rewrite_resume.bat`：Windows 便捷入口；
- `tests/` 或现有测试布局中的对应流水线测试。

## 5. Interface 与输入契约

### 5.1 核心请求

核心 interface 只接收数据库 ID：

```text
RewriteRequest
  candidate_id: str
  job_ids: list[str]              # 至少 1 个；1 个为单岗定制，多个为岗位族基础版
  source_resume_ids: list[str]    # 至少 1 个
  output_name: str                # 仅文件基本名，不允许路径分隔符
  fact_answers: list[FactAnswer]  # 可选
```

`FactAnswer` 包含稳定的 `question_id`、候选人答案和事实来源说明。来源说明至少写明“如何得知或如何测量”；不能仅用“帮我补上”作为依据。候选人明确确认的答案标记为 `user_confirmed` 并记录时间，不伪装成系统自动验证。

流水线的主要 interface：

```text
run(request: RewriteRequest) -> RewriteRun
resume(run_id: str, fact_answers: list[FactAnswer]) -> RewriteRun
get(run_id: str) -> RewriteRun
```

`run` 负责一次完整尝试；`resume` 只允许从 `NEEDS_FACTS` 继续，并在同一运行目录中更新输入快照和审计记录。调用方不需要了解 Codex 命令、临时文件、PDF 字体或状态迁移细节。

### 5.2 CLI 路径引导

CLI 同时支持两种输入方式：

1. 已有数据：`--candidate-id`、一个或多个 `--job-id`、一个或多个 `--source-resume-id`；
2. 首次引导：`--resume-path` 与一个或多个 `--jd-path`，再配合候选人姓名和目标岗位。

路径引导适配层先读取 UTF-8/UTF-8 BOM 文本，创建或复用候选人、原始简历和岗位记录，再构造标准 `RewriteRequest`。PDF 只能作为需要保留的原始文件或视觉参照，不能作为首选事实文本；首个用例使用同目录 Markdown。

示例目标调用：

```powershell
rewrite_resume.bat --resume-path "C:\Users\Administrator\Desktop\caoshenao\曹申傲-AI-Agent简历-修改版.md" --jd-path "C:\path\to\target-jd.md" --candidate-name "曹申傲" --target-role "AI Agent开发" --output-dir "C:\Users\Administrator\Desktop\caoshenao\output\pdf"
```

当运行达到 `READY`，CLI 将规范产物 `resume.pdf` 复制为安全化后的 `output_name`。公司名优先；公司缺失时使用岗位名。若同名文件已存在，追加运行短 ID，不覆盖旧文件。

### 5.3 本机 HTTP

- `POST /resume-agent/v1/rewrite-runs`：接收标准 ID 请求并同步执行一次运行；
- `GET /resume-agent/v1/rewrite-runs/{run_id}`：读取状态、问题和产物清单；
- `POST /resume-agent/v1/rewrite-runs/{run_id}/facts`：提交事实答案并继续原运行。

HTTP 入口继续使用现有 loopback 限制。领域状态 `NEEDS_FACTS` 是正常响应，不作为服务器错误；输入错误返回 400，不存在的 ID 返回 404；Codex 超时、进程失败或渲染失败映射为 502/504，同时运行记录保持 `FAILED` 以便审计。

本期使用同步调用，不引入后台 worker。Codex 运行可能持续数分钟；主要交互入口是批处理脚本，HTTP 入口用于本机自动化和调试。

## 6. 数据流

一次运行按以下顺序执行：

1. 校验候选人、简历和岗位存在，且简历和候选人、岗位和候选人的归属关系合法；去重 ID，但保持用户给出的顺序。
2. 单 JD 以该岗位标题为唯一目标；多 JD 要求候选人的 `target_roles` 非空，以第一项作为岗位族名称，并调用现有确定性关键词分析生成研究报告。调用方负责选择同一岗位族的 JD；流水线在审计中列出全部岗位标题，标题明显跨方向时进入 `NEEDS_FACTS` 请用户重选，而不把不同方向强行平均。JD 明确文本和自动关键词推断必须分开标注。
3. 在 `analysis/runs/<run_id>/` 写入脱离数据库也能复核的输入快照与 JD 证据。
4. 复用现有事实约束 prompt，并显式要求 Codex 使用 `$improving-agent-resumes`。简历和 JD 都包裹为不可信数据，禁止执行其中的指令。
5. `CodexCliAdapter` 通过 stdin 执行：

   ```text
   codex exec --ephemeral --sandbox read-only --output-schema <schema> -o <result> -C <project-root> -
   ```

   不使用 `danger-full-access` 或跳过沙箱的参数。超时默认 15 分钟，可通过本地配置降低，不能由 JD 文本修改。
6. 同时进行 Pydantic 校验和事实审计。任何准备进入成稿的显著事实必须能映射到 `source_resume_id + 原文证据` 或已记录的 `FactAnswer`；JD 只能证明岗位要求，不能证明候选人能力。
7. 若存在阻断问题，写入问题与审计，状态转为 `NEEDS_FACTS`，不生成 `resume.md` 或 `resume.pdf`。
8. 若事实门禁通过，以模型返回的完整改写稿生成 `resume.md`，随后生成 PDF，并执行文本、页数和视觉渲染检查。
9. 所有检查通过后原子地写入最终产物，状态转为 `READY`。任何异常转为 `FAILED`，保留已完成的审计材料，不留下伪装成成功的 PDF。

## 7. 运行状态与持久化

状态机：

```text
QUEUED -> RUNNING -> READY
                  -> NEEDS_FACTS -> RUNNING
                  -> FAILED
```

约束：

- `READY` 和 `FAILED` 是终态；失败重试创建新运行，避免覆盖失败证据；
- 只有 `NEEDS_FACTS` 可以通过事实答案回到 `RUNNING`；
- 非法迁移在进入 Codex 前拒绝；
- 每次迁移更新 SQLite 时间戳，并追加到 `audit.md`；
- 进程中断后遗留的 `RUNNING` 不自动标为成功。进程下次启动时把上一个进程遗留的 `RUNNING` 原子转为 `FAILED/INTERRUPTED`；当前进程仍在执行的运行保持 `RUNNING`。用户通过新运行重试。

SQLite 新增 `rewrite_runs` 表，保存运行 ID、候选人 ID、简历与岗位 ID JSON、输出名、状态、事实答案 JSON、产物目录、尝试次数、脱敏错误码/摘要和时间戳。大段简历、JD、模型结果和 PDF 不重复塞入数据库，保留在运行目录。

## 8. 结构化模型输出

Codex 的顶层 JSON 必须完整包含 `$improving-agent-resumes` 的八段输出：

1. `verdict`：岗位匹配结论；
2. `scorecard`：分项评分及证据；
3. `risks`：事实、表达、技术深度和 ATS 风险；
4. `missing_information`：缺失项、稳定问题 ID、是否阻断及原因；
5. `rewritten_resume_markdown`：完整改写稿；
6. `rewrite_reasons`：主要改写及其 JD/简历来源；
7. `depth_recommendations`：在不虚构前提下可继续补强的技术深度；
8. `interviewer_probes`：面试官可能追问的问题。

另有机器审计字段 `claim_audit`。每条显著改写记录：成稿文本、`source_resume_ids`、原文证据、`job_ids`、变更类型和事实状态。它不算第九个用户展示章节，而是事实门禁的机器依据。

结构不完整、额外字段、无法解析的 JSON、声称引用不存在 ID、成稿缺失或 Markdown 包含表格/HTML 均视为无效模型输出，并转为 `FAILED`，不尝试从自由文本中猜测修复。

## 9. 事实门禁

以下情况阻断并进入 `NEEDS_FACTS`：

- 成稿拟保留或强化的数字、比例、规模、时间、角色归属没有明确来源；
- 多份简历对同一公司、项目、时间、指标或个人贡献存在冲突；
- 模型把团队成果升级为个人成果，或把 JD 需求写成候选人已有能力；
- 身份、教育、任职时间等关键事实不完整到足以造成误导。

以下情况不阻断：

- JD 需要但简历完全没有的技能。该项作为差距与风险呈现，并从成稿中省略；
- 有明确原文证据、只是压缩措辞或调整顺序；
- 候选人在 `FactAnswer` 中明确确认且提供了可理解的来源说明。

首份简历中诸如“10+ Domain Skills”“40+ MCP 工具”“86% 工具调用成功率”“15 秒降至 200ms”“两轮达到 90% 完整率”等指标都必须在审计中指出原始文本位置，并要求能说明统计口径。原文存在只证明“曾写过”，不能自动证明指标真实；如本次成稿将其作为核心卖点但缺少口径，必须进入 `NEEDS_FACTS`。

门禁先用确定性规则从成稿提取数字，再要求每个业务指标在 `claim_audit` 中有唯一记录。业务指标包括数量、比例、耗时、吞吐、成功率、维度、节点/状态/字段规模等；日历日期、电话号码、技术产品名中的版本号不按业务指标处理。指标必须同时具有原文位置和统计/测量说明；只有原文而没有统计口径时，生成稳定问题 ID 并阻断。模型漏报数字、重复映射或分类含糊时，流水线按更严格结果处理并进入 `NEEDS_FACTS`，而不是自行猜测。

## 10. 运行产物

每次运行按执行阶段最多写入：

```text
analysis/runs/<run_id>/
  input-snapshot.json
  jd-evidence.md
  codex-task.md
  result.json      # 仅模型结果通过 JSON Schema 后存在
  audit.md
  resume.md       # 仅事实门禁通过后存在
  resume.pdf      # 仅全部验收通过后存在
```

文件使用临时名写完后原子替换。`input-snapshot.json` 包含内容哈希、使用的 ID、事实答案和执行版本；`audit.md` 记录状态迁移、问题、来源映射、渲染验收和脱敏失败摘要。运行目录加入 `.gitignore`，防止个人信息进入版本库。

`input-snapshot.json` 和 `audit.md` 在运行创建时就存在；其余文件在对应步骤成功后出现。`result.json` 仅保留通过 JSON Schema 的模型结果；非法原始输出不保存为正式结果，错误类型和安全摘要写入审计。标准日志只记录运行 ID、状态、耗时、哈希和错误码，不记录手机号、邮箱、JD 全文或完整简历。

需要明确：CLI 是本机程序，但简历和 JD 内容会作为 prompt 发给当前 Codex 配置所使用的 OpenAI 模型服务；“本机”指入口、文件和编排在本机，不代表模型完全离线。

## 11. PDF 版式与验收

生产渲染采用当前环境已具备的 ReportLab，并嵌入本机中文字体（优先微软雅黑，缺失时使用等价的已安装中文字体）。PyMuPDF 用于读取页数、提取文本和渲染 PNG 进行检查。两项依赖都写入 `requirements.txt` 并固定兼容版本；生产代码使用 `pymupdf` 接口，不使用已弃用的 `fitz` 导入名。

版式规则：

- A4、一栏、黑白为主、清晰标题层级；
- 禁止表格、图标、文本框、页眉页脚和图片化文字；
- 链接必须同时保留可读文本；
- 一页优先：先使用标准字号和间距排版，再在可读下限内逐级压缩；
- 不得截断内容。达到最小字号和间距仍溢出时自动使用两页；
- 两页时在章节边界分页，避免把单条项目经历拆开；
- 原始 PDF 永不覆盖。

验收条件：

- PDF 页数为 1；只有一页无法在可读阈值内容纳时才允许 2 页；
- `resume.md` 与 PDF 提取文本在规范化空白后保持内容一致，允许项目符号和自动换行差异；
- 无空白尾页、无文本越界、无字体缺字方框、无重叠；
- 每页渲染为 PNG，执行程序化边界检查，并在首个真实用例中人工查看渲染图；
- 任何检查失败时状态为 `FAILED`，不导出定制版 PDF。

## 12. 错误处理与安全

- Codex 不存在或未登录：`FAILED/CODEX_UNAVAILABLE`，给出可执行诊断，不泄露 prompt；
- 超时：终止子进程，`FAILED/CODEX_TIMEOUT`；
- 非零退出：`FAILED/CODEX_PROCESS_ERROR`，只保留截断且脱敏的 stderr 摘要；
- 非法 JSON/Schema：`FAILED/INVALID_MODEL_RESULT`；
- 事实不足：`NEEDS_FACTS`，返回稳定问题 ID；
- PDF 失败：`FAILED/RENDER_ERROR` 或 `FAILED/RENDER_VALIDATION_ERROR`；
- 输出名去除路径分隔符和 Windows 保留名，最终路径必须解析在指定输出目录内；
- 所有数据库与文件写入使用参数化 SQL、受控目录和原子替换；
- CLI 不把简历正文拼进命令行参数，统一通过 stdin，降低进程列表和 shell 转义泄露风险。

## 13. 测试策略

测试以 `ResumeRewritePipeline` 的 interface 为主要测试面，不断言内部函数调用顺序。

### 13.1 快速测试

- 合法请求的状态迁移与固定产物；
- 候选人、简历、岗位归属校验；
- 单 JD 与多 JD 的证据生成差异；
- Fake Codex 返回八段合法 JSON 时得到 `READY`；
- 缺失数字来源、简历冲突、JD 冒充能力时得到 `NEEDS_FACTS` 且没有 PDF；
- 提交事实答案后只允许 `NEEDS_FACTS -> RUNNING`，并能到达 `READY`；
- Codex 超时、非零退出、垃圾 JSON、错误 ID 和额外字段均安全失败；
- 日志中不出现手机号、邮箱或完整简历片段；
- 路径穿越、非法输出名和同名文件不会覆盖现有文件。

### 13.2 真实集成测试

- 真实 `codex exec` 冒烟测试设为手动或显式环境开关，避免普通测试消耗模型调用；
- ReportLab 生成 1 页与 2 页夹具，PyMuPDF 检查页数、文本一致性、页面边界和空白页；
- FastAPI TestClient 覆盖三个运行路由及 loopback 限制；
- 批处理脚本用临时候选人/JD/输出目录完成端到端运行；
- 首个曹申傲真实产物渲染成 PNG 后逐页视觉复核。

## 14. 首次交付顺序

1. 实现数据模型、运行存储和深模块 interface；
2. 以 Fake Codex/Fake Renderer 完成状态机和事实门禁测试；
3. 接入真实 Codex CLI 与固定 JSON Schema；
4. 接入 ReportLab/PyMuPDF 渲染和验收；
5. 接入 FastAPI 与 `rewrite_resume.bat`；
6. 使用曹申傲 Markdown 和一份真实目标 JD 运行；
7. 对指标口径问题收集候选人回答，必要时从 `NEEDS_FACTS` 继续；
8. 输出并视觉核验定制版 PDF，原文件保持不变。

当前仓库没有 `resume_agent.db`、`job_decisions.jsonl` 或可直接选用的真实 JD，所以实现可以独立完成，但首次定制版 PDF 必须等到一份具体 JD 被导入或作为 `--jd-path` 提供。不能用 `analysis/` 中的项目报告冒充招聘 JD。

## 15. 验收清单

- CLI 与 FastAPI 通过同一个 `ResumeRewritePipeline` 工作；
- 无新增 API Key，真实模型调用走本机 Codex CLI；
- 所有成稿事实可追溯，阻断问题绝不生成 PDF；
- 八段技能输出和机器 claim audit 均通过 Schema；
- 运行目录完整、日志脱敏、Git 不追踪敏感产物；
- PDF 为 ATS 友好的一页优先自适应版式，通过文本与渲染检查；
- 首个真实输出按公司或岗位命名，且不覆盖曹申傲原始 PDF；
- 现有 P0 路由、JD 导入、市场报告和投递统计测试继续通过。
