# Boss / Nowcoder 联动发现与简历工作流

本指南说明如何在本机隔离运行 Boss、Nowcoder 和主线编排器，并生成综合版或定向版简历。它不会绕过登录、验证码、验证页或平台风控，也不会把摘要、测试数据或不完整 JD 冒充真实完整岗位。

## 1. 隔离边界

三个进程使用三个数据库；来源 worktree 之间、来源与主线之间都不能共享 SQLite 文件。

| 进程 | 代码 | 建议端口 | 数据库 |
|---|---|---:|---|
| Boss 来源 | `origin/codex/boss-dom-resilience` 的 detached worktree | `8011` | `boss-source.db` |
| Nowcoder 来源 | `origin/nowcoder` 的 detached worktree | `8012` | `nowcoder-source.db` |
| 主线 CLI / API | 当前功能 worktree | 无需长期监听即可运行 CLI | `main-orchestrator.db` |

来源 endpoint 只能使用 `http://127.0.0.1`、`http://localhost` 或 `http://[::1]`。主线拒绝非 loopback 地址。数据库、PDF、日志、来源 checkpoint、facts、真实 JD 和 `analysis/runs/` 都是本机运行数据，不得加入 Git。

以下命令块是供操作者执行的文本步骤。本仓库不会自动执行命令块；模型生成的 `gap-roadmap.md` 中 `test_commands` 也始终只是文本。

## 2. 准备两个来源 worktree

在主仓库目录执行一次：

```powershell
Set-Location 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt'
git worktree add --detach 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-boss-source' origin/codex/boss-dom-resilience
git worktree add --detach 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-nowcoder-source' origin/nowcoder
```

为运行数据准备仓库外目录：

```powershell
$runtimeRoot = 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-runtime'
New-Item -ItemType Directory -Force $runtimeRoot | Out-Null
```

不要把两个来源指向同一 `RESUME_AGENT_DB_PATH`。也不要把来源数据库指向主线数据库。

## 3. 启动 Boss 来源服务

在独立 PowerShell 窗口中：

```powershell
Set-Location 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-boss-source'
$env:RESUME_AGENT_DB_PATH = 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-runtime\boss-source.db'
$env:RESUME_AGENT_ACTION_LOG_PATH = 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-runtime\boss-actions.jsonl'
$env:RESUME_AGENT_DECISION_LOG_PATH = 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-runtime\boss-decisions.jsonl'
uv sync
uv run python -m uvicorn main:app --host 127.0.0.1 --port 8011
```

Boss 分支的 Tampermonkey 脚本源文件默认可能仍以 `8010` 为启动地址。安装本地副本前，把脚本顶部 `OPTIONS.serverHost` 的本机端口改为 `8011`，并确认请求仍指向 `127.0.0.1`。不要提交这项本机端口调整。

Boss 调研依赖用户自己的正常登录态：

1. 安装该 worktree 的最新 `web_script.js`，打开 Boss 岗位搜索页；
2. 在右下角面板选择“调研”按钮，不要选择“投递”；
3. 输入一个或多个关键词，设置每个关键词的预加载上限；
4. 手工点击“开始”，按需要在等待窗口中设置地区、薪资和经验；
5. 等脚本采集岗位名称、公司、薪资和完整 JD。四个目标字段缺失的岗位不会入库；
6. 从页面日志或 `GET http://127.0.0.1:8011/resume-agent/v1/candidates` 取得自动创建的项目 ID，作为主线 `--boss-project-id`。

调研模式不评分、不打招呼、不发送简历，也不处理聊天消息。主线只读取来源服务返回的岗位 JSON，不读取 Boss Cookie。

## 4. 启动 Nowcoder public research

在第二个 PowerShell 窗口中：

```powershell
Set-Location 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-nowcoder-source'
$env:RESUME_AGENT_DB_PATH = 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-runtime\nowcoder-source.db'
$env:RESUME_AGENT_ACTION_LOG_PATH = 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-runtime\nowcoder-actions.jsonl'
$env:RESUME_AGENT_DECISION_LOG_PATH = 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-runtime\nowcoder-decisions.jsonl'
uv sync
uv run python -m uvicorn main:app --host 127.0.0.1 --port 8012
```

主线的 `discover` 命令会为每个 `--keyword` 调用 Nowcoder 本机任务 API，并固定创建 `mode=research` 任务。`--nowcoder-acquisition-mode public` 使用公开调研；关键词 `AI Agent`、`--limit 10` 是首轮 smoke 的建议值。public 接口是上游页面内部接口，不是稳定的官方 API；HTTP 403/429、验证页、网络失败或响应结构变化必须保留为安全错误码和降级状态，不能伪造成功。

Nowcoder 的 `.runtime/nowcoder/tasks/` 保存可恢复任务 checkpoint，浏览器模式的独立配置位于 `.runtime/nowcoder/browser-profile/`。这些内容不提交。Nowcoder 本期只做岗位发现与调研，不自动投递，也不访问聚合岗位的公司官网替用户申请。

## 5. 配置主线并创建 discovery run

在当前功能 worktree 的 PowerShell 窗口中设置独立主库和两个 loopback endpoint：

```powershell
Set-Location 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt\.worktrees\boss-nowcoder-resume-orchestration'
$env:RESUME_AGENT_DB_PATH = 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-runtime\main-orchestrator.db'
$env:RESUME_AGENT_RUNS_ROOT = 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-runtime\main-runs'
$env:RESUME_AGENT_BOSS_RESEARCH_ENDPOINT = 'http://127.0.0.1:8011'
$env:RESUME_AGENT_NOWCODER_RESEARCH_ENDPOINT = 'http://127.0.0.1:8012'
$env:RESUME_AGENT_DISCOVERY_TIMEOUT_SECONDS = '30'
```

主库中的 `candidate_id` 和 `source_resume_id` 与 Boss 来源项目 ID 属于不同数据库，不能混用。先在主库创建候选人和来源简历，再使用 Boss 来源数据库中的项目 ID 创建发现运行：

```powershell
.\rewrite_resume.bat discover --candidate-id "candidate_<main-id>" --sources boss nowcoder --keyword "AI Agent" --city "上海" --limit 10 --boss-project-id "candidate_<boss-project-id>" --nowcoder-acquisition-mode public
```

CLI 返回紧凑 JSON。保存返回的 `id`，不要根据目录名猜测 run ID。状态含义：

- `COLLECTING`：来源任务仍在运行或等待用户交互；退出码为 `3`，不是失败；
- `READY_FOR_IMPORT`：全部来源结束且有足够候选样本；
- `PARTIAL`：至少一个来源失败，但仍有至少 3 条合格样本；
- `FAILED`：来源全部终止后完整样本仍不足，或没有可用来源结果；不生成 PDF；
- `IMPORTED`：筛选结果已经幂等导入主库。

## 6. 恢复和轮询同一个 discovery run

始终用返回的原 run ID 继续轮询，不要为了“重试”重复创建 Nowcoder 任务：

```powershell
.\rewrite_resume.bat poll-discovery --discovery-run-id "discovery_run_<id>"
```

主线从主库恢复来源 checkpoint 和去重状态。Nowcoder 服务重启后，research 任务会从 `.runtime/nowcoder/tasks/` 恢复为暂停状态；先在 Nowcoder 本地 UI 点击继续，或调用该来源任务的 `/resume` API，再轮询同一个 discovery run。Boss 需要用户操作时，保留 run ID，在已登录页面完成“调研”采集后再轮询；等待本身不等于失败。

如果所有来源已经结束且 run 进入 `FAILED`，它不能靠补写测试数据恢复。先取得足够的完整真实 JD，再创建新的 discovery run。运行报告只记录 run ID、状态、安全错误码和计数；不要记录 Cookie、Token、联系方式或真实 JD 全文。

## 7. 生成综合版

只有至少 3 条、最多 20 条完整、同一 AI 工程职业族、可追踪来源的真实 JD 才能进入综合版。partial JD、跨职业族岗位、已关闭岗位和无法追踪来源的内容会被拒绝。

```powershell
.\rewrite_resume.bat broad --discovery-run-id "discovery_run_<id>" --candidate-id "candidate_<main-id>" --source-resume-id "source_resume_<id>" --output-dir "C:\Users\Administrator\Desktop\caoshenao\output\pdf"
```

验收以下内容：

- 选用 JD 可以在 `discovery-report.md` 和 `ats-report.md` 中追踪；
- 每次只有一份投递 PDF，文件名不覆盖原始简历或旧投递版；
- `claim-ledger.json` 的 claim 状态与 PDF 表达一致；
- 未完成内容只出现在“在建项目（待验收）”章节；
- `gap-roadmap.md` 有实现顺序、产物和验收步骤；
- `ats-report.md` 是确定性覆盖报告，不是通过承诺。

少于 3 条完整同族真实 JD 时，正确结果是“不生成综合版 PDF”。不得复制测试 fixture、手写假 JD 或把 partial 摘要补足到 3 条。

## 8. 生成定向版

CLI 只接受以下三种目标之一：主库 `--job-id`、真实 `--jd-path`、可由来源适配器读取的 `--jd-url`。三者互斥。

```powershell
.\rewrite_resume.bat targeted --candidate-id "candidate_<main-id>" --source-resume-id "source_resume_<id>" --job-id "job_<id>" --output-name "曹申傲-目标岗位.pdf" --output-dir "C:\Users\Administrator\Desktop\caoshenao\output\pdf"
```

或：

```powershell
.\rewrite_resume.bat targeted --candidate-id "candidate_<main-id>" --source-resume-id "source_resume_<id>" --jd-path "C:\Users\Administrator\Desktop\caoshenao\某公司-Agent工程师.md" --output-name "曹申傲-目标岗位.pdf" --output-dir "C:\Users\Administrator\Desktop\caoshenao\output\pdf"
```

或：

```powershell
.\rewrite_resume.bat targeted --candidate-id "candidate_<main-id>" --source-resume-id "source_resume_<id>" --jd-url "https://www.nowcoder.com/jobs/detail/<id>" --output-name "曹申傲-目标岗位.pdf" --output-dir "C:\Users\Administrator\Desktop\caoshenao\output\pdf"
```

Boss URL 需要浏览器登录态时会返回 `SOURCE_INTERACTION_REQUIRED`；Nowcoder URL 只能得到摘要时会返回 `PARTIAL_JD`。两种情况都不会留下半条岗位或可投递 PDF。

## 9. Gap 验收与后续升级

`IN_PROGRESS` 内容必须绑定 `gap_id`，并只出现在“在建项目（待验收）”章节。它不能使用已完成语态、公司或客户归属、生产结果或未测量指标。

项目实际完成后，由用户在受控环境中运行测试并记录结果。将证据保存为 JSON；`command` 是已经运行过的命令记录，主线只验证和保存文本，绝不执行它：

```json
{
  "gap_id": "gap-123456789abc",
  "project_path": "C:/work/agent-eval",
  "verified_claims": ["实现本地 Agent 评测集并保存逐案例结果"],
  "artifact_hashes": {
    "tests/eval_cases.json": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "test_records": [
    {
      "command": "python -m unittest test_agent_eval.py -v",
      "exit_code": 0,
      "output_sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    }
  ],
  "completed_at": "2026-08-29T08:00:00Z",
  "user_confirmed": true,
  "confirmed_at": "2026-08-29T08:05:00Z"
}
```

提交证据：

```powershell
.\rewrite_resume.bat accept-gap --candidate-id "candidate_<main-id>" --evidence-json "C:\Users\Administrator\Desktop\caoshenao\gap-evidence.json"
```

后续改写只能把 `verified_claims` 中逐条列出的断言升级为 `VERIFIED`，不能从路径、hash、测试命令或计划目标推导额外经历和指标。

## 10. 投递边界

Boss：生成 PDF 后，用户必须手工把它上传为 Boss 账号的简历附件，确认附件列表中的实际顺序，再把 Boss 来源 worktree 本地 `user_config.json` 的 `frontend.resumeIndex` 设为对应的从 0 开始索引。首次投递前在页面核对选中的附件。主线不自动上传，也不会猜测 `resumeIndex`。

Nowcoder：本期没有自动上传、自动申请或自动联系能力。公开调研得到的链接只用于来源追踪和人工查看。

## 11. 本地验收与提交前检查

真实来源 smoke 需要用户登录态、上游网络和正常页面交互。没有完成这些条件时，记录安全错误码、降级状态或 `COLLECTING` run ID，不要声称成功。完整真实样本不足 3 条时停止在“不生成综合版”。

提交前确认：

```powershell
python -m unittest discover -p "test*.py" -v
$pyFiles = Get-ChildItem -Path resume_agent -Recurse -Filter *.py | ForEach-Object { $_.FullName }
python -m py_compile $pyFiles
git diff --check origin/main..HEAD
git ls-files -- analysis .superpowers "*.pdf" "*facts*.json" "*.db"
git status --short
```

`git ls-files` 不应列出运行数据库、PDF、facts、`analysis/runs`、来源 checkpoint 或真实 JD。若列出这些文件，先从暂存区移除并保留在本机；不要删除用户的原始材料。
