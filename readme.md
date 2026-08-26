# goodjob

一个面向 Boss 直聘的轻量自动投递简历项目，采用“浏览器脚本 + 本地 Python 后端”的组合方式。

当前主线已经收敛为：
- 单简历
- 单方向投递
- 规则筛选岗位
- 固定打招呼
- 收到 Boss 新消息后直接发送简历
- 默认不继续自动聊天

它不是多招聘平台框架，不是复杂多轮自动聊天助手，也不是招聘 SaaS。

## 适合什么方向

当前仓库模板默认更适合这些岗位：
- AI 产品工程师
- AI 应用工程师
- AI Agent / 智能体
- 工作流工程师
- AI Native / Vibe Coding / 大模型应用落地类岗位

当前评分逻辑的核心偏好：
- 标题只做弱信号
- JD 正文里的真实技术要求、工具链、工作流是强信号
- 更关注 Claude Code、Cursor、Codex、Agent、Workflow、Prompt，以及需求到调试部署上线的闭环能力

不适合作为主目标方向的岗位：
- 传统算法训练 / 模型研发
- 传统运维 / SRE / DevOps
- 销售 / 运营
- 纯 C/C++/Go 底层岗

## 现在能做什么

当前主链能力：
- 在 Boss 直聘岗位列表里轮换搜索关键词
- 对岗位做规则打分
- 达到阈值后自动打招呼
- 收到 Boss 新消息后直接发送指定简历
- 连续多轮没有新岗位时自动切换关键词继续挂机
- 遇到超时、详情异常、打招呼异常时自动恢复

## Resume Agent P0：调研 Prompt 与投递统计

P0 以独立子路由 `/resume-agent/v1` 挂载在现有 FastAPI 中，不改变原来的：

- `/get-job-score`
- `/log-action`
- 规则评分
- 固定 `resumeIndex`
- `web_script.js`

P0 明确不生成、不修改简历，也不会调用任何大模型。它只完成：

- 保存候选人和原始简历文本；
- 同一候选人可保存多份历史简历；
- 从现有 `job_decisions.jsonl` 自动导入并去重合格 JD；
- 对 JD 做确定性关键词统计；
- 保存人工核验的调研结论；
- 生成多简历候选人画像 Prompt；
- 生成可复制给其他模型的简历修改 Prompt；
- 统计合格 JD 的月薪、年薪和 P25/P50/P75 区间；
- Prompt 强制要求外部模型不得编造经历，并要求逐条标注事实和 JD 来源；
- 读取现有 `job_actions.jsonl` 统计岗位评估、打招呼、回复信号和简历发送；
- 手工补录投递、招聘方回复、面试、拒绝、Offer 和主动撤回事件。

面试内容、面试反馈记忆、自动简历改写、简历版本审批和模型 Token 统计不在本 P0 范围内。

### P0 数据文件

默认使用仓库根目录下的：

```text
resume_agent.db
```

该文件及 SQLite WAL 文件已被 Git 忽略。可以通过环境变量改到其他位置：

```env
RESUME_AGENT_DB_PATH=
RESUME_AGENT_ACTION_LOG_PATH=
RESUME_AGENT_DECISION_LOG_PATH=
RESUME_AGENT_ALLOW_REMOTE=false
```

配置示例见 `resume_agent.env.example`。P0 没有 API Key 配置，也不会发起模型网络请求。虽然原服务仍保持现有监听方式，但新的 Resume Agent 子路由默认只接受本机请求；只有明确设置 `RESUME_AGENT_ALLOW_REMOTE=true` 才允许远程访问。

### P0 最小使用流程

1. `POST /resume-agent/v1/candidates` 创建候选人；
2. 多次调用 `POST /resume-agent/v1/candidates/{candidate_id}/source-resumes` 保存历史简历；
3. `POST /resume-agent/v1/prompts/candidate-profile` 生成多简历候选人画像 Prompt；
4. 正常运行 Boss 脚本，让 `/get-job-score` 继续记录岗位判断；
5. `POST /resume-agent/v1/imports/job-decisions` 从现有日志导入合格 JD；
6. `GET /resume-agent/v1/market-report` 查看岗位要求和薪资分布；
7. 使用导入结果的 `job_id` 调用 `POST /resume-agent/v1/research-reports`；
8. `POST /resume-agent/v1/prompts/resume-rewrite` 获取可复制的改简历 Prompt；
9. `GET /resume-agent/v1/delivery-stats` 查看投递漏斗；
10. `POST /resume-agent/v1/delivery-events` 补录面试、拒绝或 Offer 等结果。

`candidate-profile` 和 `resume-rewrite` 都只返回 Prompt，不调用模型，也不生成或保存修改后的简历。`resume-rewrite` 支持通过 `source_resume_ids` 同时选择多份历史简历。

## 本地 Codex 简历定制

简历定制命令由 `rewrite_resume.bat` 启动，并复用当前机器已登录的 Codex。批处理会将简历和 JD 的 Prompt 内容发送给 Codex 所配置的 OpenAI 模型服务；命令编排、数据库和运行产物则保留在本机。无需新增 OpenAI API Key。

岗位输入必须是已导入的 `job_id`，或真实的 `--jd-path` 文件。`analysis/` 中的报告是项目分析文档，不是 JD，不能把它们作为岗位输入。每次运行的可审计产物仅写入被 Git 忽略的 `analysis/runs/`。

### 首次运行（曹申傲）

先将一份真实目标 JD 保存为 `C:\Users\Administrator\Desktop\caoshenao\target-jd.md`。确认文件存在后，在仓库根目录运行：

```powershell
$runResult = & .\rewrite_resume.bat run --candidate-name "曹申傲" --target-role "AI Agent开发" --resume-path "C:\Users\Administrator\Desktop\caoshenao\曹申傲-AI-Agent简历-修改版.md" --jd-path "C:\Users\Administrator\Desktop\caoshenao\target-jd.md" --output-dir "C:\Users\Administrator\Desktop\caoshenao\output\pdf" | Out-String | ConvertFrom-Json
$runResult | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 'analysis\runs\last-cli-result.json'
$runResult
```

不要用 `analysis/` 报告替代真实 JD。该流程从不覆盖原始 Markdown 简历或原始 PDF；只有事实门禁和 PDF 验收均通过时，才会在输出目录生成按公司或岗位命名的新定制版 PDF。

### 事实问题与继续运行

如果命令返回 `NEEDS_FACTS`，它只会创建待确认的问题，绝不会生成 PDF。把候选人原样确认的答案保存成 JSON 数组，每项使用以下格式：

```json
[
  {
    "question_id": "returned-stable-question-id",
    "answer": "候选人确认的答案",
    "source_note": "答案如何得知或如何测量",
    "user_confirmed": true,
    "confirmed_at": "2026-08-26T01:30:00Z"
  }
]
```

`user_confirmed` 必须显式为 `true`，`confirmed_at` 必须是 UTC 时间戳（`Z`），且 `source_note` 必须写明具体记录或测量来源，答案才可作为指标依据。旧版三字段 facts 仍可读取，但会安全地视为“未确认”，不能解锁指标。

仅使用本次命令 JSON 结果返回的 `question_id` 和 `run_id`，不要猜测或改写它们。随后可执行：

```powershell
$runId = (Get-Content -Raw -Encoding UTF8 'analysis\runs\last-cli-result.json' | ConvertFrom-Json).id
.\rewrite_resume.bat resume --run-id $runId --facts-json "C:\Users\Administrator\Desktop\caoshenao\resume-facts.json" --output-dir "C:\Users\Administrator\Desktop\caoshenao\output\pdf"
```

### JD逆向调研与薪资口径

新版浏览器请求只额外携带当前 `searchKeyword`，不改变原有评分请求体、分数、阈值和投递动作。后端的 `job_decisions.jsonl` 额外记录：

- `jobFingerprint`
- `searchKeyword`
- `threshold`
- `qualified`

导入接口会：

- 兼容没有以上新字段的历史日志；
- 使用历史分数和指定阈值判断旧记录是否合格；
- 解析 `rawJob` 中的标题、薪资和 JD；
- 使用完整 JD 内容哈希去重；
- 默认只导入合格岗位。

市场报告对 `20-30K`、`20-30K·13薪`、`30-50万/年` 等可识别格式做确定性统计，并输出样本数、P25、P50、P75和按搜索关键词分组的数据。该结果是当前搜索条件下的可见样本估计，不是模型承诺或最终 Offer 预测。

服务启动后可通过 FastAPI 的 `/docs` 查看完整请求结构。

### 投递统计口径

现有浏览器日志可以自动提供：

- 已评估岗位事件数；
- 规则拒绝和跳过事件数；
- 打招呼排队、成功和失败事件数；
- 发送简历事件数；
- 根据“收到新消息后发送简历”推断的招聘方回复信号。

历史日志没有候选人 ID 和稳定的 Boss 岗位 ID，因此这些数字是事件数，不保证等于去重后的岗位数。面试、拒绝和 Offer 需要通过新接口手工补录。

## 演示视频

- B 站演示视频：https://www.bilibili.com/video/BV1MyX6BFEp3

## 项目结构

- `main.py`：FastAPI 后端入口
- `core.py`：规则评分主逻辑 + 遗留聊天能力
- `config.py`：配置加载与岗位评分配置
- `web_script.js`：Boss 页面 Tampermonkey 脚本
- `user_config.example.json`：用户配置模板
- `resume-example.md`：简历模板
- `PROJECT_MEMORY.md`：长期项目背景与关键决策
- `DEV_LOG.md`：开发演进记录

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 准备用户配置

```bash
cp user_config.example.json user_config.json
```

首次最少只需要改这些字段：
- `introduce`：固定打招呼语
- `tags`：搜索关键词列表
- `frontend.resumeIndex`：发第几份简历，从 0 开始
- `frontend.thread`：投递阈值

### 3. （可选）准备简历文件

```bash
cp resume-example.md resume.md
```

说明：
- 当前自动投递主链并不依赖 `resume.md` 做岗位打分
- 它主要用于你自己管理简历内容，或保留给遗留接口扩展使用

### 4. 启动后端

```bash
python main.py
```

Windows 下也可以直接双击：

```text
start_backend.bat
```

### 5. 部署浏览器脚本

把 `web_script.js` 内容粘贴到 Tampermonkey 中，然后打开 Boss 直聘页面即可。

## 最小使用路径

1. 复制 `user_config.example.json` 为 `user_config.json`
2. 修改：
   - `introduce`
   - `tags`
   - `frontend.resumeIndex`
   - `frontend.thread`
3. 启动后端 `python main.py`
4. 浏览器装入 `web_script.js`
5. 打开 Boss 直聘页面测试

## 配置说明

当前推荐把所有用户差异化内容集中维护在 `user_config.json`。

普通用户主要改：
- `introduce`
- `tags`
- `frontend.resumeIndex`
- `frontend.thread`

更细的岗位评分规则默认由项目维护者在 `scoring` 中调整，不要求普通用户自己从零设计。

### 顶层字段
- `resume_name`
- `think_model`
- `chat_model`
- `introduce`
- `character`
- `tags`

### `frontend`
浏览器端运行参数，例如：
- `resumeIndex`
- `thread`
- `manualFilterWaitMs`
- `roundRestartDelayMs`
- `maxEmptyRounds`
- `detailTimeout`
- `greetTimeout`
- `preloadScrollPixels`
- `preloadScrollWaitMs`

### `backend`
后端运行参数，例如：
- `job_score_delay_base_ms`
- `job_score_delay_jitter_ms`

### `scoring`
岗位评分规则主要分成：
- 标题强负向词
- 标题弱负向扣分词
- 标题强匹配词
- 标题中匹配词
- 正文强正向词
- 正文辅助正向词
- 正文负向扣分词

当前推荐理解方式：
- 标题只做快速筛选
- JD 正文里的真实技术要求才是主要判断依据

前端启动时会优先请求后端 `/client-config`，统一读取：
- `introduce`
- `tags`
- `frontend`

## 关于大模型依赖

当前主链在未安装 `ollama` 的情况下也能运行，覆盖这些能力：
- 固定 `tags`
- 固定 `introduce`
- 规则岗位评分
- 收到新消息后直接发简历

如果你还要继续启用这些遗留接口：
- `/reply`
- `/is-need-resume`
- `/is-need-works`

再额外安装 `ollama` 即可。

也就是说：
- `ollama` 现在属于遗留能力
- 不是主链运行前提

## 仓库说明

- `user_config.json`、`resume.md`、日志文件等本地文件默认不进入仓库
- `user_config.example.json` 是公开模板，不建议直接提交真实配置
- 当前公开仓库优先服务中文用户，因此默认中文说明

## 历史说明

这个项目在个人使用阶段曾演化出一版“双简历 / 双方向自动路由”的复杂版本。

那版代码已经单独归档到分支：
- `archive/double-routing-chaos`

该分支仅供历史参考，不推荐新用户直接从那里开始。

## 后续方向

当前更合理的继续方向是：
- 保持用户配置外置化
- 保持岗位规则可调
- 继续优先保证 Boss 自动化链路稳定
- 不把主线重新做回复杂双路由或重度模型依赖版本
- 继续让评分更贴近 JD 正文真实技术要求，而不是岗位名字字面词
