# ATS detection UI

完整的页面操作、API 示例、支持范围和排错说明见 [ATS 使用文档](../../../docs/ats-usage.md)。

企业招聘官网 ATS 检测的本地 Web 界面。

页面直接连接仓库内的 Python ATS 检测模块，可以：

- 选择已收录企业招聘站；
- 执行 robots 检查和真实 ATS 检测；
- 查看 ATS、置信度、证据、复核状态和浏览器需求；
- 不填写公司 URL，使用本地租户目录直接跨公司搜索岗位；
- 从 Greenhouse、Lever、Ashby、SmartRecruiters、Workable 和北森智业的公开接口获取、筛选并评分岗位；
- 添加一个临时自定义官网；
- 原样导出本次检测报告为 JSON。

## Local development

先在仓库根目录启动只监听本机的 Python 服务：

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8010
```

再打开另一个终端启动前端：

```powershell
npm install
npm run dev
```

默认页面地址为 `http://localhost:3000`。页面只会请求本机的
`/job-sources/ats/detect`、`/job-sources/ats/jobs`、
`/job-sources/ats/boards/discover` 和 `/job-sources/ats/jobs/search`；后端只接受本机客户端，
且浏览器来源只允许 `localhost:3000`、`127.0.0.1:3000` 和 `[::1]:3000`。

自动搜索会复用 `.runtime` 中 24 小时内的租户目录；缓存缺失或手动刷新时，从
Common Crawl 的 URL 索引重新发现租户。该步骤不下载 WARC 或公司页面正文；
公共索引不可用且没有有效缓存时，页面会明确显示发现失败。

每次最多提交 20 个网址，同一时间只允许一个 Web 检测批次和一个岗位采集。
检测遵守 robots；岗位采集只访问 Greenhouse、Lever、Ashby、SmartRecruiters、
Workable 的官方公开 GET 接口，或 `*.zhiye.com` 北森招聘站的同源公开 POST
接口，最多扫描 200 条并返回 1–50 条。SmartRecruiters 每次最多获取 50 条岗位
详情；所有采集都不登录、不使用 Token、不绕过验证码，也不会自动投递。

## Validation

```powershell
npm run build
node --test tests/rendered-html.test.mjs
```
