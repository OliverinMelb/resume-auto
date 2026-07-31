# V2EX 酷工作 JSON Feed 官方调研

## 范围与方法

- 数据源：V2EX 官方公开的 [`https://www.v2ex.com/feed/jobs.json`](https://www.v2ex.com/feed/jobs.json)。
- 观察时间：2026-07-30 09:34:49 UTC（北京时间 17:34:49）。
- 只进行 **1 次匿名 HTTP GET**；没有携带 token、Cookie 或登录态，没有调用其他 V2EX API，也没有为核验重复请求 Feed。
- 格式语义只以 [JSON Feed Version 1 官方规范](https://www.jsonfeed.org/version/1/) 为依据。
- 本文不复制帖子正文、用户名、邮箱、手机号、微信或 QQ 等个人信息。样本只记录字段名、JSON 类型、出现次数、字符串长度范围和脱敏后的 URL 结构。

下文严格区分：

- **观察事实**：上述单次响应中实际看到的内容，只代表该时刻的快照。
- **规范要求**：JSON Feed v1 对发布者或阅读器的要求。
- **实现推断**：针对本项目适配器的建议，不代表 V2EX 的稳定性承诺。

## 观察事实

### HTTP 响应

| 项目 | 单次 GET 的观察结果 |
| --- | --- |
| HTTP 状态 | `200` |
| 最终 URL | `https://www.v2ex.com/feed/jobs.json`，没有发生重定向 |
| `Content-Type` | `application/json;charset=utf-8` |
| `Content-Length` | 响应头未提供 |
| 响应体大小 | 对已解码内容重新按 UTF-8 计数为 286,226 字节；这是客户端计数，不是服务端声明的 `Content-Length` |

### Feed 顶层

实际顶层字段为 `version`、`title`、`home_page_url`、`feed_url`、`description`、`icon`、`favicon`、`items`。

| 字段 | 实际值或结构 |
| --- | --- |
| `version` | `https://jsonfeed.org/version/1` |
| `title` | `酷工作` |
| `home_page_url` | `https://www.v2ex.com/go/jobs` |
| `feed_url` | `https://www.v2ex.com/feed/jobs.json` |
| `items` | JSON 数组，共 50 项 |
| `next_url` | 未出现 |

### Item 字段、类型与快照内可选性

“出现数”只描述这 50 项，不能提升为长期字段保证。

| 字段 | JSON 类型 | 出现数 | 脱敏观察 |
| --- | --- | ---: | --- |
| `id` | string | 50/50 | 长度均为 30；全部唯一；结构均为 `https://www.v2ex.com/t/{数字}` |
| `url` | string | 50/50 | 长度均为 30；全部唯一；结构与 `id` 相同 |
| `title` | string | 50/50 | 10–96 个字符；未记录原文 |
| `content_html` | string | 50/50 | 74–20,519 个字符；未记录或复制内容 |
| `date_published` | string | 50/50 | 长度均为 25；全部可按 RFC 3339 解析，均带数字时区偏移 |
| `date_modified` | string | 11/50 | 长度均为 25；其余 39 项完全没有该字段 |
| `author` | object | 50/50 | 每个对象都含下表三个字段 |

本次快照没有观察到 `content_text`。实际 `author` 对象结构如下：

| `author` 子字段 | JSON 类型 | 出现数 | 脱敏观察 |
| --- | --- | ---: | --- |
| `name` | string | 50/50 | 4–15 个字符；未记录原值 |
| `url` | string | 50/50 | 32–43 个字符；未记录具体用户 URL |
| `avatar` | string | 50/50 | 66–75 个字符；未记录具体图片 URL |

### 身份与更新信号

- 50 项的 `id` 与各自 `url` 全部相等，且在快照内都唯一；两者都是 V2EX 主题 URL。
- 这支持把 `id` 用作当前数据源的外部身份，但**一次快照无法证明 V2EX 永远不更改或复用该值**。跨时间稳定性依据来自 JSON Feed v1 对发布者的规范要求，而不是此次观测。
- `date_published` 在本次快照中完整，`date_modified` 仅覆盖 11 项；因此不能把 `date_modified` 当作必填字段，也不能用其缺失推断“内容从未变更”。
- `author` 和 `content_html` 本次都完整，但在格式层面仍是条件必需或可选字段，见下一节。

## JSON Feed v1 规范要求

### Feed 与 item 的最低结构

- 顶层 `version`、`title` 和 `items` 必需；`version`、`title` 是字符串，`items` 是对象数组。公开 Web Feed 的 `home_page_url` 与 `feed_url` 在语法上可选，但规范强烈建议提供，并将 `feed_url` 定义为 Feed 的唯一标识。[顶层字段规范](https://www.jsonfeed.org/version/1/#top-level)
- 每个 item 的 `id` 必需，并应在该 Feed 的时间维度内保持唯一：item 更新时不得改变 `id`，新 item 不应复用历史 `id`。规范要求阅读器把非字符串 `id` 强制转换为字符串；缺少 `id` 的 item 必须丢弃。[Item 的 `id` 规则](https://www.jsonfeed.org/version/1/#items)、[阅读器错误处理](https://www.jsonfeed.org/version/1/#suggestions-for-feed-readers)
- `url` 是可选字符串，表示 item 的永久链接，可以与 `id` 相同；阅读器打开网页时应默认使用 `url`。[Item URL 规则](https://www.jsonfeed.org/version/1/#items)、[URL 打开建议](https://www.jsonfeed.org/version/1/#suggestions-for-feed-readers)
- `content_html` 与 `content_text` 各自可选，但每个 item 至少要有一个；只有 `content_html` 允许包含 HTML。`title` 是可选的纯文本。[内容字段规范](https://www.jsonfeed.org/version/1/#items)
- `date_published` 与 `date_modified` 都是可选字符串；出现时应采用 RFC 3339。`author` 也是可选对象，结构与顶层 `author` 相同；一旦提供该对象，`name`、`url`、`avatar` 至少要有一个。[日期与作者规范](https://www.jsonfeed.org/version/1/#items)、[author 对象规范](https://www.jsonfeed.org/version/1/#top-level)

### 安全解析边界

- 规范建议无效 JSON 不应被局部解析或使用；缺少 `id` 的 item 必须丢弃。对其他可恢复的单项错误，阅读器可以按用户体验决定降级处理。[阅读器错误处理](https://www.jsonfeed.org/version/1/#suggestions-for-feed-readers)
- 自定义扩展字段名以 `_` 开头；不了解扩展的阅读器必须忽略它。未来版本可以增加字段，因此解析器不应因未知字段本身而失败。[扩展规则](https://www.jsonfeed.org/version/1/#extensions)、[向前兼容](https://www.jsonfeed.org/version/1/#future-compatibility)
- 发布者应使用 `application/json`，JSON Feed 应使用 UTF-8；item 数量没有格式层面的上限，规范同时提醒发布者控制实际 Feed 大小。[发布建议](https://www.jsonfeed.org/version/1/#suggestions-for-publishers)

## 实现推断与约束建议

以下均是本项目的实现建议，不是从单次响应推导出的 V2EX 保证。

### 获取层

1. 固定匿名 GET 官方 HTTPS URL，不注入认证头、token 或 Cookie，不依赖浏览器登录态。
2. 设置连接/读取超时、有限重定向次数和可配置响应体上限；成功后校验最终 URL 仍是允许的 HTTPS V2EX 来源。当前 286,226 字节只适合作为容量基线，不应被硬编码为精确上限。
3. 接受媒体类型 `application/json`，比较时忽略 `charset` 参数；先完整校验 JSON，再进入字段处理。若后续响应提供 ETag 或 `Last-Modified`，可按规范建议使用条件 GET 降低轮询成本，但本次没有为此追加请求。
4. 不跟随 item 或 HTML 中的任意 URL 去抓取内容；否则会把不受信任的 Feed 数据变成 SSRF/额外网络访问入口。

### 结构与字段处理

1. 要求顶层为对象、`version` 为明确支持的 `https://jsonfeed.org/version/1`、`title` 为字符串、`items` 为数组；对响应字节数、数组项数、嵌套深度和单字段长度设置防御性上限。
2. 忽略未知字段并保留监控计数，不把未知字段当成整体失败理由；但关键字段类型错误应只隔离对应 item。
3. 对规范要求的非字符串 `id` 做确定性字符串化；对象/数组等复杂异常值不要使用语言默认的模糊字符串表示，应丢弃并记录结构化错误，以避免身份碰撞。缺少或空白 `id` 的 item 丢弃。
4. 日期按 RFC 3339 解析并保留时区语义。缺失或无效日期宜存为 `null`，把抓取时间单独存为 `observed_at`，避免把抓取时间伪装成发布时间。
5. `content_html` 必须视为不可信 HTML：展示前使用允许列表净化，或通过 HTML 解析器提取纯文本；不要直接插入页面、执行脚本、加载远程资源，也不要把正文或联系方式写入普通日志。`title`、`author.name` 同样需要按不可信文本转义。

### 去重、更新与“消失”

1. 建议主身份键为 `(source = "v2ex", item.id)`，`url` 作为可点击永久链接另存。不要依赖标题、作者或发布时间去重；即使本次 `id == url`，也应分别解析和校验。
2. 同一 `id` 再次出现时按允许更新的字段刷新，并记录 `last_seen_at`；`date_modified` 存在时可作为更新信号之一，但不能作为唯一信号。
3. JSON Feed 本质是会随时间变化的列表，而不是完整、不可变的数据库。[Feed 结构说明](https://www.jsonfeed.org/version/1/#the-structure-of-a-feed) 本次只返回 50 项且没有 `next_url`，所以某个历史 item 从后续快照中缺席，可能只是滑出发布者保留窗口。**缺席不等于帖子被删除、职位关闭或内容失效**；不得仅凭一次或多次 Feed 缺席删除本地记录或标记业务状态。若未来需要确认删除/关闭，必须定义独立且获准的官方信号，本调研未验证此类信号。

## 结论

该端点在本次匿名请求中返回可解析的 JSON Feed v1，关键身份字段表现规整：50 个 item 均有唯一且相等的主题 URL 形式 `id`/`url`。适配器可以以规范中的 `id` 作为稳定身份约定，但必须对字段可选性、不可信 HTML、有限快照和 Feed 缺席语义保持保守：观察到“全都有”不等于长期必填，观察到“后来没有”也不等于删除。
