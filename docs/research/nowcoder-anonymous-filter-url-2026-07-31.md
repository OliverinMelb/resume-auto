# 牛客匿名筛选与分页协议调研（2026-07-31）

## 结论

牛客校招页地址中的筛选值不是 MD5：

- `careerJob=11002` 是普通十进制职位分类 ID；
- `city=%E5%8C%97%E4%BA%AC` 是“北京”的标准 UTF-8 URL 百分号编码；
- 页面会把地址栏筛选转换为匿名搜索请求，分页由请求体中的 `page` 完成，并不依赖地址栏出现页码。

因此，匿名模式没有必要要求用户逐个手工拼 URL。本地 UI 可以匿名加载牛客的职位分类树和城市列表，把用户选择转换成搜索请求；筛选 URL 只需作为可读、可保存、可复现的快照。

同日后续复核发现，职位广场实际使用的匿名接口还支持自由文本关键词。自动模式应优先
使用关键词搜索，不应把关键词猜成单个职位分类。

## 第一方实现证据

牛客当前校招筛选页：

```text
https://www.nowcoder.com/jobs/school/jobs?careerJob=11002&city=北京
```

页面当前加载的第一方脚本
[`main.entry.js`](https://static.nowcoder.com/nowpick/web/nowcoder/3.2.694/javascripts-wp-c/page/jobHome/main.entry.js)
显示：

1. `careerJob` 和 `city` 从页面路由查询参数读取并写回；
2. `careerJob[2]` 被作为 `careerJobId`；
3. 首页推荐列表请求体包含 `page`、`jobCity`、`careerJobId`、`recruitType`、
   `pageSize`、`recommend`、`requestFrom` 和 `pageSource`；
4. 职位广场请求体另外包含自由文本 `query`，以及可选 `jobCity`、
   `careerJobId`；
5. 列表继续加载时会递增 `page`。

同版本第一方公共脚本
[`common.js`](https://static.nowcoder.com/nowpick/web/nowcoder/3.2.694/javascripts-wp-c/lib/common.js)
同时定义了 `POST /u/job/search` 和 `POST /u/job/square-search`。压缩代码中的逗号
表达式最终选择 `square-search` 作为职位广场请求；页面使用 `/np-api` 前缀，因此
支持关键词的实际地址是：

```text
POST https://www.nowcoder.com/np-api/u/job/square-search
```

## 匿名实测

2026-07-31 使用全新 HTTP 请求、未附带 Cookie、登录凭据或 `visitorId` 进行验证。

职位广场关键词搜索请求的关键业务字段为：

```json
{
  "query": "AI-Agent",
  "jobCity": ["北京"],
  "careerJobId": "",
  "recruitType": 1,
  "page": 1,
  "pageSize": 20,
  "requestFrom": 1,
  "pageSource": 5001
}
```

原分类接口分页实测仍说明 `careerJobId` 和 `jobCity` 有效；新增关键词接口无 Cookie
实测如下：

| 城市 | 请求页 | HTTP/业务状态 | 返回条数 | 当前页/总页数 |
|---|---:|---|---:|---|
| 上海 | 1 | 200 / `code=0` | 20 | 1 / 3 |
| 北京 | 1 | 200 / `code=0` | 20 | 1 / 2 |
| 北京 | 2 | 200 / `code=0` | 7 | 2 / 2 |

| 关键词 | 城市 | HTTP/业务状态 | 返回条数 |
|---|---|---|---:|
| AI-Agent | 广州 | 200 / `code=0` | 1 |
| 软件开发 | 广州 | 200 / `code=0` | 3 |

`AI-Agent` 返回岗位的公开数据包含 Agent 职责和 `AI Agent` 标签，说明 `query` 确实
参与检索，而不是只改变页面展示。

北京和上海返回的首条记录 ID 与城市字段不同，说明 `jobCity` 确实参与筛选，而不是只改变页面展示文字。

## 分类和城市无需手工维护

以下第一方端点在相同的无 Cookie 条件下返回 `code=0`：

- [`GET /np-api/u/job/career-job`](https://www.nowcoder.com/np-api/u/job/career-job)
  返回完整的三级职位分类树；其中 `11002` 对应 `java工程师`；
- [`GET /nccommon/city/all-with-china`](https://www.nowcoder.com/nccommon/city/all-with-china)
  返回热门城市和完整城市层级。

这意味着本地 UI 可以自动提供职位类别和城市选择器，不需要让用户识别或填写 `11002`，也不需要手工写 `%E5%8C%97%E4%BA%AC`。

## 对当前实现的影响

当前仓库的匿名实现只读取筛选页 HTML 一次，并以
`public_filtered_page_end` 结束，所以上限通常是 20 条：

- [`job_sources/nowcoder/source.py`](../../job_sources/nowcoder/source.py)
- [`job_sources/nowcoder/ui.html`](../../job_sources/nowcoder/ui.html)

这不是牛客匿名能力本身的限制，而是当前实现主动采用的保守边界。当前 UI 又让城市输入和筛选 URL 独立存在，才会出现“界面选北京、URL 仍是上海、结果仍是上海”的不一致。

## 建议的产品边界

匿名模式应调整为：

1. UI 匿名加载可选职业树和城市列表；
2. 用户输入自由文本关键词，可选职位类别、城市和招聘类型；
3. 后端生成包含 `search`、可选 `careerJob` 和 `city` 的规范化 URL，作为任务快照；
4. 后端调用匿名 `square-search`，把关键词放入 `query` 并按 `page` 分页；
5. 以接口返回的 `currentPage`、`totalPage` 和去重后的岗位 ID 控制停止；
6. 保留请求间隔、任务数量上限，并在 403、429、验证页或结构变化时停止；
7. 浏览器模式只负责匿名接口不支持、需要页面交互或需要用户处理登录/验证的场景。

## 风险与不确定性

这些端点来自牛客当前生产页面和第一方脚本，但不是对外发布的稳定 API。路径、字段、分类 ID、限流和验证策略都可能变化。因此实现时应把它们封装成可替换适配器，保留 HTML 首屏降级和浏览器模式，不应承诺长期稳定或用于高频、大规模采集。
