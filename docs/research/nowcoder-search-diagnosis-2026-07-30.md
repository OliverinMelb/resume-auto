# 牛客职位搜索失败诊断

观测时间：2026-07-30（Asia/Shanghai）

范围：匿名、低频、只读访问牛客公开职位页面；未登录、未使用 Cookie、未调用
需要权限的接口，也未尝试验证码、签名或设备指纹绕过。

## 结论

本地搜索“后端 + 实习”得到 0 条结果并伴随 1 条详情失败，是两个问题叠加：

1. 牛客公开“实习职位”首屏本身混合了校招、实习和企业 ATS 聚合岗位。官方公开
   页面同时展示“2027届秋招”“AI Agent开发实习生”“后端开发工程师（Rust方向）”
   等不同类型的卡片，因此入口名称不能直接作为岗位用工类型。
2. 失败岗位 `30300000145061` 是牛客聚合的 Moka 岗位。URL 中的数字 ID 对应详情
   载荷的 `globalJobId`；载荷自己的 `id/jobId` 是 Moka UUID。当前实现只允许
   `URL ID == detail.id`，因此把有效详情误判为页面结构变化。

该失败岗位的公开载荷同时表明：

- `globalJobId = 30300000145061`
- `id/jobId = 64ed9e91-98e4-4b02-b969-e1c818ebf1bb`
- `platform = moka`
- `commitment = 实习`
- `recruitType = 1`
- `jobDetailUrl` 指向 Moka 企业招聘门户

因此，仅放宽 ID 校验仍不够。当前原生牛客标准化器还会把它误判为全职，并遗漏
外部 ATS 的 `description`、`salary` 和 `jobDetailUrl`。

## 可重复复现

公开列表中的三个“后端”标题候选：

| 来源入口 | 路径 ID | 标题 | 详情结果 |
|---|---:|---|---|
| internship | 30300000145061 | 后端开发工程师（Rust方向） | 被现有 ID 校验拒绝 |
| campus | 456580 | 【华为27届秋招】软件开发工程师-Java后端 | campus/full_time |
| campus | 456656 | Agent / 后端开发 | campus/full_time |

所以最终显示 0 条实习：两个成功详情确实是校招全职；唯一真正带
`commitment=实习` 的 Rust 岗位被兼容性错误过滤。

同类只读样本进一步验证这是外部 ATS 的稳定形状，而非单条脏数据：

| 路径 ID | 平台 | commitment | ID 对应关系 |
|---:|---|---|---|
| 30300000145667 | Moka | 实习 | path ID = globalJobId，payload id 为 UUID |
| 30300000145061 | Moka | 实习 | path ID = globalJobId，payload id 为 UUID |
| 30300000146073 | 北森 | 兼职 | path ID = globalJobId，payload id 为 UUID |

## HTTP 与浏览器差异

- 匿名访问[牛客实习职位页](https://www.nowcoder.com/jobs/intern/center)时，
  服务端 HTML 直接包含一组混合岗位卡片。
- 浏览器访问后 URL 会出现 `recruitType=2`。
- 但匿名 HTTP 直接访问
  [带 recruitType 的页面](https://www.nowcoder.com/jobs/intern/center?recruitType=2)
  时，服务端 HTML 只有筛选界面和空列表，具体筛选结果依赖前端 JavaScript。
- 当前没有确认到牛客正式承诺、无需登录且适合第三方长期调用的公开职位搜索 API。
  页面内部接口不应被当成稳定公共契约，更不应以破解签名为实现目标。

### 校招职位页的例外

用户提供的校招筛选 URL：

```text
https://www.nowcoder.com/jobs/school/jobs?careerJob=11022&city=上海
```

经登录浏览器和匿名 HTTP 对照验证：

- `careerJob=11022` 对应“前端工程师”；
- `city=上海` 对应上海；
- 浏览器渲染出 4 条去重后的上海前端校招岗位；
- 匿名 HTTP 返回同一批筛选岗位卡片，不需要 Cookie。

在页面选择“软件开发 → 后端开发 → Java”后，URL 变为：

```text
https://www.nowcoder.com/jobs/school/jobs?careerJob=11002&city=上海
```

匿名 HTTP 能直接解析 20 条筛选卡片，其中包括“Java开发实习生”。这说明校招职位页
可以采用“用户生成筛选 URL → 后端匿名读取 → 详情确认用工类型”的轻量方案。

相同的 `careerJob/city` 参数用于 `/jobs/intern/center` 时，匿名服务端 HTML 仍不包含
岗位卡片。因此不能把校招职位页和实习中心视为同一种渲染契约。

修正后的判断是：

- 纯 HTTP 足以读取校招筛选 URL、服务端首屏和详情；
- 纯 HTTP 仍不足以实现实习中心的完整动态筛选或牛客全站搜索；
- 浏览器可优先用于帮助用户生成/发现公开筛选 URL；
- 仅当目标入口没有服务端结果时，再评估匿名浏览器辅助读取用户可见结果；
- 遇到登录、验证码、403 或 429 时停止。

## 建议修正

1. 将牛客详情解析区分为两种明确 schema：
   - 牛客原生岗位；
   - ATS 聚合岗位（Moka、北森等）。
2. ATS 岗位使用 `globalJobId` 校验来源路径 ID，同时保留 ATS UUID。
3. ATS 用工类型优先读取 `commitment`，不要把 `recruitType` 同时当作用工类型。
4. ATS 字段映射：
   - `jobDetailUrl` → `officialApplyUrl`
   - `description` → `description`
   - `salary` → `salaryText`
   - `platform` → ATS 来源标识
5. 搜索结果返回安全的失败摘要，包括公开岗位 ID、标题和错误类型，避免只显示
   “1 条详情失败”而无法判断影响。
6. HTTP 搜索继续明确标注“首屏候选”；完整搜索单独评估匿名浏览器适配器，不调用
   未承诺的内部接口。
7. 优先增加“粘贴牛客校招筛选 URL”入口，保留并校验 `careerJob`、`city` 等公开
   参数；不要一开始硬编码完整职位分类表。

## 资料

- [牛客实习职位公开页面](https://www.nowcoder.com/jobs/intern/center)
- [带 recruitType 的公开页面](https://www.nowcoder.com/jobs/intern/center?recruitType=2)
- [上海前端校招筛选页](https://www.nowcoder.com/jobs/school/jobs?careerJob=11022&city=%E4%B8%8A%E6%B5%B7)
- [上海 Java 后端校招筛选页](https://www.nowcoder.com/jobs/school/jobs?careerJob=11002&city=%E4%B8%8A%E6%B5%B7)
- [失败岗位牛客详情链接](https://www.nowcoder.com/jobs/detail/30300000145061)

页面内容和非公开内部实现可能变化；上述字段关系是 2026-07-30 的只读实测结果，
不是牛客对第三方提供的 API 稳定性承诺。
