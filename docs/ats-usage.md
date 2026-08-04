# ATS 岗位发现与采集使用文档

## 功能说明

本模块用于发现公司的公开 ATS 招聘站、读取公开岗位并按关键词筛选和评分。
它不依赖公司的自建招聘页面正文，岗位详情直接来自 ATS 的公开接口。

当前支持直接采集岗位的 ATS：

- Greenhouse
- Lever
- Ashby
- SmartRecruiters
- Workable
- 北森智业（`*.zhiye.com`）

采集过程不需要 Token、账号或 Cookie，不会登录、绕过验证码或自动投递。

## 本地启动

在仓库根目录启动 Python API：

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8010
```

另开一个终端启动前端：

```powershell
Set-Location job_sources\ats\ui
npm install
npm run dev
```

浏览器打开：

```text
http://localhost:3000
```

API 文档地址：

```text
http://127.0.0.1:8010/docs
```

前端和 API 都只监听或接受本机请求。浏览器页面应使用 `localhost:3000`，否则会被后端的 Origin 校验拒绝。

## 页面使用方式

### 方式一：只输入岗位关键词，自动搜索

这是日常使用的推荐入口，不需要逐个粘贴公司 URL。

1. 在“自动发现 ATS 公司”区域填写岗位关键词，例如“前端”“Java”或“产品经理”。
2. 设置“查询公司数”，范围为 1–20。
3. 设置“返回岗位数”，范围为 1–50。
4. 点击“直接搜索公开岗位”。
5. 在结果表中查看公司、岗位、来源、地点、评分，并点击“查看岗位”进入官方申请页。

系统会先读取本地公司目录缓存，再直接请求各公司的公开 ATS 岗位接口。发现目录只负责找到 ATS 租户，不提供 JD。

如果目录过期或希望重新发现公司，点击“刷新公司目录”。刷新会访问 Common Crawl 的公开 URL 索引，首次执行可能需要等待网络响应。

### 方式二：检测已知公司招聘页

1. 在“已收录的企业招聘站”中选择一个或多个公司。
2. 点击“检测选中网站”。
3. 查看识别出的 ATS、置信度、证据和访问状态。
4. 对支持直采的结果填写岗位关键词和数量，然后获取公开岗位。

一次最多检测 20 个 URL。检测阶段会遵守目标站的 robots 规则。

### 方式三：添加新的招聘 URL

1. 点击“添加其他官网 URL”。
2. 填写公司名称和完整的 HTTPS 招聘页地址。
3. 运行检测。
4. 如果识别为六种可直采 ATS 之一，即可继续获取岗位。

一个普通公司官网 URL 只有在页面或跳转中能识别出 ATS 时才可直采。最稳定的输入是 ATS 官方托管地址。

## 支持的 URL 形式

| ATS | 推荐 URL 示例 |
| --- | --- |
| Greenhouse | `https://job-boards.greenhouse.io/{tenant}` |
| Lever | `https://jobs.lever.co/{tenant}` |
| Ashby | `https://jobs.ashbyhq.com/{tenant}` |
| SmartRecruiters | `https://careers.smartrecruiters.com/{tenant}` |
| Workable | `https://apply.workable.com/{tenant}` |
| 北森智业 | `https://{tenant}.zhiye.com/...` |

注意：`https://apply.workable.com/j/{shortcode}` 只有岗位短码，没有公司租户，不能单独用于发现或批量采集。应使用公司的 Workable 首页地址。

## 本地 API

所有接口均为 `POST`，请求头使用 `Content-Type: application/json`。

### 自动发现 ATS 公司

```powershell
$body = @{
  limit = 50
  refresh = $false
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8010/job-sources/ats/boards/discover `
  -ContentType 'application/json' `
  -Body $body
```

- `limit`：1–50。
- `refresh=false`：优先复用 24 小时内的缓存。
- `refresh=true`：重新查询公共 URL 索引。

### 跨公司搜索岗位

```powershell
$body = @{
  keyword = 'frontend'
  limit = 20
  max_boards = 10
  refresh = $false
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8010/job-sources/ats/jobs/search `
  -ContentType 'application/json' `
  -Body $body
```

- `keyword`：必填，最多 100 个字符。
- `limit`：返回 1–50 个岗位。
- `max_boards`：本次查询 1–20 家公司。
- `refresh`：是否先刷新公司目录。

### 检测招聘页 ATS

```powershell
$body = @{
  urls = @(
    'https://jobs.ashbyhq.com/linear',
    'https://jobs.lever.co/example'
  )
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8010/job-sources/ats/detect `
  -ContentType 'application/json' `
  -Body $body
```

每批 1–20 个 URL，单个 URL 最长 8192 字节。

### 采集一家公司的岗位

```powershell
$body = @{
  url = 'https://jobs.ashbyhq.com/linear'
  company = 'Linear'
  keyword = 'frontend'
  limit = 20
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8010/job-sources/ats/jobs `
  -ContentType 'application/json' `
  -Body $body
```

- `company` 和 `keyword` 可以为 `$null`。
- `limit` 范围为 1–50。
- 返回结果包含标准化岗位字段、评分、是否合格、官方岗位页和申请页。

## 数据范围与限制

- 公司目录缓存位于 `.runtime/ats-boards.json`，有效期为 24 小时。
- 自动搜索每次最多查询 20 家公司、返回 50 个岗位。
- 单个 ATS 看板最多扫描 200 条公开岗位。
- SmartRecruiters 单次列表最多读取 100 条，并最多请求 50 条岗位详情。
- Workable 使用公开 account endpoint，不使用需要 Token 的 `/spi/v3`。
- 当前不做搜索引擎全网抓取、WARC 下载、定时任务、数据库持久化或自动投递。
- 公共索引可能不是实时全量数据；岗位内容以 ATS 实际公开接口返回为准。

## 常见错误

| 页面或 API 错误 | 含义与处理 |
| --- | --- |
| `origin_not_allowed` | 从非本机允许地址打开了页面；使用 `http://localhost:3000`。 |
| `unsupported_job_board` | URL 不是支持的 ATS 官方托管地址，或缺少租户信息。 |
| `discovery_failed` | Common Crawl 索引不可用且没有有效本地缓存；稍后重试。 |
| `collection_failed` | ATS 公共接口暂时不可用、响应格式异常或返回过大。 |
| `search_failed` | 跨公司搜索流程失败；先刷新目录，再缩小公司数重试。 |
| `*_in_progress` | 同类任务正在执行；等待当前任务完成后再提交。 |

开发期间的后台日志可写入或查看：

```text
.scratch/ats-detection/logs/api.stderr.log
.scratch/ats-detection/logs/api.stdout.log
.scratch/ats-detection/logs/ui.stderr.log
.scratch/ats-detection/logs/ui.stdout.log
```

## 验证命令

后端完整测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover
```

前端构建、页面测试和静态检查：

```powershell
Set-Location job_sources\ats\ui
npm test
npm run lint
```
