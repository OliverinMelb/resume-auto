# 牛客岗位调研数据源

## 这次增加了什么

牛客以独立的 `job_sources/nowcoder/` 数据源接入现有 FastAPI 服务，不修改
`web_script.js`，也不替换或删除 Boss 直聘功能。

这条链路完成：

- 在牛客实习、校招和社招职位页面中填写关键词及筛选条件；
- 使用独立、可见的 Playwright Chrome 保存用户正常登录状态；
- 采集职位列表，读取牛客公开详情，并标准化为统一岗位结构；
- 将岗位正文复用现有 JD 评分器评分，但不以分数过滤采集结果；
- 支持不写数据库的快速预览；
- 支持带检查点、停止和恢复能力的完整调研；
- 保留牛客原文链接及聚合岗位的官网申请链接；
- 遇到登录、验证、HTTP 403 或 429 时暂停或报错，不尝试绕过。

本功能不会自动投递、自动联系招聘者、破解接口签名、绕过验证码或访问权限。

## 目录和数据流

```text
job_sources/contracts.py             最小统一岗位与查询契约
job_sources/evaluation.py            复用现有 JD 评分器
job_sources/router.py                数据源聚合路由
job_sources/nowcoder/browser.py      可见 Chrome 搜索与翻页
job_sources/nowcoder/source.py       公开详情读取、解析和标准化
job_sources/nowcoder/tasks.py        快速/调研任务、停止与恢复
job_sources/nowcoder/checkpoints.py  原子化任务检查点
job_sources/nowcoder/research.py     写入 Resume Agent 候选项目
job_sources/nowcoder/router.py       本地 API
job_sources/nowcoder/ui.html         本地操作页面
test_nowcoder_source.py              单元与路由测试
```

```text
本地页面填写查询
→ 可见 Chrome 打开对应牛客职位频道
→ 填写关键词、城市、毕业年份和排序
→ 读取列表并翻页
→ 公开 HTTP 读取详情
→ 必要时使用同一可见页面读取详情
→ 标准化岗位
→ 复用现有 JD 评分
→ 页面展示
→ 完整调研模式才写入 Resume Agent
```

牛客使用仓库专用的 Chrome 用户目录，不复用日常 Chrome 配置：

```text
.runtime/nowcoder/browser-profile/
```

可恢复任务保存在：

```text
.runtime/nowcoder/tasks/
```

整个 `.runtime/` 已被 Git 忽略，Cookie、登录状态和本地检查点不会进入提交。

## 安装与启动

环境要求：

- Python 3.11；
- `uv`；
- 本机已安装 Google Chrome；
- 能正常访问牛客公开职位页面。

在仓库根目录执行：

```powershell
uv sync
uv run python main.py
```

服务默认地址：

```text
http://127.0.0.1:8010
```

牛客操作页面：

```text
http://127.0.0.1:8010/job-sources/nowcoder/ui
```

FastAPI 接口文档：

```text
http://127.0.0.1:8010/docs
```

如果 8010 端口已经运行旧进程，需要先正常停止旧进程，再重新启动，确保加载的是
当前分支代码。

## 如何查找“上海后端开发实习”

1. 打开牛客操作页面。
2. 渠道选择“实习”。
3. 关键词填写“后端开发”。
4. 城市填写“上海”。
5. 首次验证选择“快速预览”。
6. 点击开始。
7. 独立 Chrome 如果出现登录框，在窗口中正常登录；回到本地页面点击继续。
8. 等待状态变为完成，检查职位标题、公司、JD、牛客原文链接和匹配评分。

搜索由牛客站内搜索执行，因此不会再从首页候选中做本地标题匹配。代码保留站内搜索
返回的相关结果，不要求职位标题必须精确包含关键词。

## 快速预览与完整调研

### 快速预览

- 最多获取 30 条；
- 最多读取 3 页；
- 展示标准化岗位和评分；
- 不写入 `resume_agent.db`；
- 适合检查筛选条件和页面兼容性。

### 完整调研

- 每批最多推进 100 页；
- 保存列表、详情、失败项和当前进度；
- 可以停止，再显式恢复；
- 服务重启后，未完成任务恢复为暂停状态，不会自行继续；
- 必须新建或明确选择一个 Resume Agent 候选项目；
- 按 `nowcoder + source_job_id` 写入和去重。

实习岗位的薪资原文仍保留在岗位证据和页面展示中，但写入 Resume Agent 时通用薪资
字段为空，因此不会混入普通岗位的市场平均薪资统计。

## 状态说明

| 状态 | 含义 | 处理方式 |
|---|---|---|
| `running` | 正在读取列表或详情 | 等待任务推进 |
| `completed` | 当前范围已读取完成 | 检查结果和失败数 |
| `paused` | 用户停止或重启后等待恢复 | 点击继续 |
| `waiting_for_login` | 牛客要求正常登录 | 在独立 Chrome 登录后点击继续 |
| `verification_required` | 牛客显示人工验证 | 在独立 Chrome 完成验证后点击继续 |
| `failed` | 页面结构、网络或数据校验失败 | 查看错误信息，修复后再恢复或新建任务 |

HTTP 403、429 和明显的访问限制不会转为浏览器绕过，任务会保留已有结果并停止继续
请求。列表或详情结构无法确认时也不会把任务误报为完整完成。

## 数据与兼容边界

- Boss 直聘原有浏览器脚本、评分接口和自动投递行为保持不变；
- 快速预览不会修改数据库；
- 完整调研只写入用户明确选择的本地 Resume Agent 数据库；
- 不包含数据库迁移；
- 不提交 `.env`、Cookie、Token、登录状态或抓取结果；
- 对 Moka、北森等牛客聚合岗位，只保存牛客公开摘要及官网申请链接，不继续抓取外部
  ATS；
- 当前共享查询契约偏向牛客需要，后续接入其他数据源前应先做最小兼容评审，不应
  直接扩成重量级框架。

## 本地验证

运行全部 Python 测试：

```powershell
uv run python -m unittest discover -p "test_*.py"
```

只运行牛客测试：

```powershell
uv run python -m unittest test_nowcoder_source.py
```

检查锁文件：

```powershell
uv lock --check
```

浏览器验收应至少覆盖：

1. 实习、校招、社招三个频道各执行一次快速预览；
2. 关键词、城市、毕业年份和“最新”排序；
3. 独立 Chrome 正常登录后继续；
4. 分页到下一页；
5. 完整调研停止、服务重启和显式恢复；
6. 牛客原文链接与官网申请链接；
7. 快速预览前后数据库文件大小和修改时间不变。

## 回滚

本功能集中在 `nowcoder` 分支。若尚未合并，直接切回原分支即可停用：

```powershell
git switch codex/boss-dom-resilience
```

若未来已经通过正常 Git 流程合并，应使用 `git revert` 撤销对应提交，不要使用
`git reset --hard` 覆盖其他人的工作。

`.runtime/nowcoder/` 只包含可重新生成的本地浏览器状态和任务检查点，不属于 Git
提交。是否清理该目录由用户自行决定，回滚代码不需要删除它。
