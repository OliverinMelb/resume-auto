# 电鸭公开列表分页契约调研

调研日期：2026-08-03（Asia/Shanghai）
范围：仅检查电鸭官方网页、官方静态前端资源和官方服务响应；没有抓取帖子详情页。

## 结论

RSS 的 25 条不是电鸭公开数据的总上限。电鸭首页和职位聚合列表均有分页；官方前端使用的匿名 JSON 列表接口可以直接获取职位分页。当前快照中，职位接口共有约 11,126 条、446 页，每页 25 条。因此可以保留 RSS 作为轻量兜底，把正式增量采集改为职位列表接口。

该接口由电鸭自己的生产前端调用，但没有找到对外稳定性承诺或公开 API 文档；实现时应把它当作“第一方公开但可能变更的前端接口”，做 schema 校验、限量抓取和失败回退。

## 1. RSS 与首页分页

- [最新 RSS](https://eleduck.com/feed/latest.xml) 返回 `200 application/xml`，本次响应恰有 25 个 `<item>`。
- [首页](https://eleduck.com/) 的服务器渲染响应每页包含 25 个帖子；本次快照的 pager 为 `total_count=20403`、`total_pages=817`、`limit_value=25`。
- [`/?page=2`](https://eleduck.com/?page=2) 和 [`/?page=3`](https://eleduck.com/?page=3) 均返回 200，并分别给出下一组 25 个不同帖子。`/page/2` 不是分页路由，返回 404。
- 官方[首页前端 bundle](https://static.eleduck.com/_next/static/chunks/pages/index-7f75d157d652144e.js) 将 `query.page` 做 `parseInt(...) || 1`，再以 `{sort, page}` 请求列表。因此正确 URL 形态是 `/?page=N`。

## 2. `__NEXT_DATA__` 中可用的数据

首页 HTML 的 `<script id="__NEXT_DATA__" type="application/json">` 顶层字段为：

`props`, `page`, `query`, `buildId`, `assetPrefix`, `isFallback`, `isExperimentalCompile`, `gip`, `appGip`, `scriptLoader`。

其中：

- `props` 包含 `isServer`, `initialState`, `initialProps`。
- 实际首页列表位于 `props.initialProps.pageProps.postList`，而不是 Redux 的 `initialState.post.postList`。
- `pageProps` 包含 `postList`, `categories`, `page`, `blogrolls`。
- `postList` 包含 `posts` 和 `pager`。
- `pager` 字段为 `total_pages`, `current_page`, `next_page`, `prev_page`, `total_count`, `limit_value`。
- 本次响应中 `buildId=8mPdbyyuNvLILG-56dVcn`、`assetPrefix=https://static.eleduck.com`；这些部署值会变，不应硬编码。

`/?page=2` 的 `query.page` 为字符串 `"2"`，`pageProps.page` 和 `pager.current_page` 均为数字 `2`；其 25 个帖子 ID/顺序与官方 `/api/v1/posts?page=2` JSON 响应完全一致。[首页第 2 页](https://eleduck.com/?page=2) · [官方帖子 API 第 2 页](https://svc.eleduck.com/api/v1/posts?page=2)

## 3. 招聘列表与验证页的区别

- 匿名访问 [`/jobs-channel`](https://eleduck.com/jobs-channel) 会收到 `302`，跳到 `/verification?redirect=%2Fjobs-channel`；`?page=2` 会保留在 `redirect` 参数中。
- 匿名访问招聘分类 [`/categories/5`](https://eleduck.com/categories/5) 同样收到 `302`，跳到验证页。
- 官方[职位页 bundle](https://static.eleduck.com/_next/static/chunks/pages/jobs-channel-78292b8e170f1877.js) 明确先进行验证检查，未通过时构造 `/verification?redirect=...`；通过后才渲染职位页。
- 与网页 UI 不同，官方前端调用的匿名 JSON 列表接口无需验证码：[`GET /api/v1/jobs_channel/posts?page=1&sort=-published_at`](https://svc.eleduck.com/api/v1/jobs_channel/posts?page=1&sort=-published_at) 本次直接返回 `200 application/json`。

官方[_app bundle](https://static.eleduck.com/_next/static/chunks/pages/_app-45ca9f712aa10bec.js) 给出的生产配置为 `apiUrl=https://svc.eleduck.com`，Axios `baseURL` 为 `${apiUrl}/api/v1`；同一 bundle 中首页列表调用 `GET /posts`，职位列表调用 `GET /jobs_channel/posts`。这证明该 JSON 服务是电鸭生产前端自身使用的第一方来源。

## 4. 职位接口实测契约

对最新排序的第 1～3 页实测：

| 请求 | HTTP | posts | current/next | 本次 total/页数 |
|---|---:|---:|---|---:|
| [`page=1`](https://svc.eleduck.com/api/v1/jobs_channel/posts?page=1&sort=-published_at) | 200 | 25 | 1 / 2 | 11,126 / 446 |
| [`page=2`](https://svc.eleduck.com/api/v1/jobs_channel/posts?page=2&sort=-published_at) | 200 | 25 | 2 / 3 | 11,126 / 446 |
| [`page=3`](https://svc.eleduck.com/api/v1/jobs_channel/posts?page=3&sort=-published_at) | 200 | 25 | 3 / 4 | 11,126 / 446 |

顶层结构为 `{posts, pager}`，`pager` 与首页相同，包含 `total_pages/current_page/next_page/prev_page/total_count/limit_value`。

职位页官方 bundle 会把以下查询参数传给该接口：`sort`, `page`, `tags`, `category`, `recruitment_type`, `talent_included`。其中 `recruitment_type` 的 UI 值为 `jd`（社区帖子招聘）、`jobs_channel`（简历智能匹配）、`talent`（精选职位推荐）。本次匿名实测结果：

- [`recruitment_type=jd`](https://svc.eleduck.com/api/v1/jobs_channel/posts?page=1&sort=-published_at&recruitment_type=jd)：10,957 条，返回项 `category.code` 均为 `jd`。
- [`recruitment_type=jobs_channel`](https://svc.eleduck.com/api/v1/jobs_channel/posts?page=1&sort=-published_at&recruitment_type=jobs_channel)：156 条，返回项 `category.code` 均为 `jobs_channel`。
- [`recruitment_type=talent`](https://svc.eleduck.com/api/v1/jobs_channel/posts?page=1&sort=-published_at&recruitment_type=talent)：13 条，返回项 `category.code` 均为 `talent`。

这些总数是实时数据快照，不应写进业务断言。

## 5. 无需详情页即可取得的字段

列表项目前提供：

- 标识和摘要：`id`, `title`, `full_title`, `summary`。
- 时间和状态：`published_at`, `modified_at`, `touched_at`, `last_comment_at`, `closed`, `deleted`, `featured`, `pinned`, `pinned_in_category`, `status`, `visibility` 等。
- 互动指标：`views_count`, `comments_count`, `upvotes_count`, `downvotes_count`, `marks_count`。
- 分类：`category {id, name, code, tag_list, kind}`。
- 标签：`tags[] {id, name, tag_group {id, name, code}}`；可直接获得招聘类型、职业、工作方式、城市、领域等标签（以具体帖子实际返回为准）。
- 发布者：`user {id, nickname, roles, level, created_at, avatar_url}`。
- 其他：`contributors`，部分帖子有 `last_comment {id, user}`。

列表项没有 `content`, `raw_content`, `contact_info`, `contact_methods`, `email_address`, `phone_number`。因此不抓详情页时能完成标题/摘要/标签层面的发现、去重和初筛，但不能保证拿到完整 JD 或联系方式。响应也没有独立的 `url` 字段；可依据官网帖子路由从 `id` 推导 `https://eleduck.com/posts/{id}`，这是基于官方页面链接形态的实现推断。

## 建议接入方式

1. 以 `/api/v1/jobs_channel/posts?page=N&sort=-published_at` 作为主增量源，RSS 保留为故障兜底。
2. 每次只拉最近 3～10 页（75～250 条），遇到已见 `id` 或日期截止点即停止；不要一次性遍历 446 页。
3. 用 `id` 去重，同时记录 `modified_at/touched_at` 以识别更新。
4. 先用 `category.code`、标签、标题和 `summary` 做筛选；只有用户明确打开时才访问详情页。
5. 校验 `{posts, pager}` 和关键字段；接口异常、schema 变化或出现验证时自动回退 RSS，并降低请求频率。响应当前带私有缓存指令和弱 ETag，但未观察到公开的限流配额说明。
