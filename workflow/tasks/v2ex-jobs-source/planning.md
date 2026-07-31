# Planning Record

- status: `ready`
- grilling_status: `confirmed`
- multi_session: `true`
- spec_ref: `workflow/tasks/v2ex-jobs-source/spec.md`

## Unresolved decisions

- None

## Decisions

- 公共测试 seam 为 V2exJobSource.fetch(cursor)，HTTP opener 仅作构造注入。
- 单 item 可恢复错误返回 partial outcome；整次 HTTP/JSON 失败保持输入游标。
- 只有无冲突双证据的 recruitment/referral 规范化，其他全部显式 review/invalid。

## Acceptance criteria

- [ ] HTML 转有界纯文本并在任何结构化输出前移除邮箱、手机号、微信和 QQ。
- [ ] item 级畸形产生 partial 语义，导入无网络线程，fetch 不写数据库或文件。
- [ ] 七类分类完整返回且仅高置信 recruitment/referral 复用共享 NormalizedJob。
- [ ] 官方固定匿名 GET 具备超时、1 MiB 上限、最终 URL 和 Content-Type 校验。
- [ ] 最终变更只在明确 owned paths，所有单测、compileall 和 diff-check 通过。
- [ ] 调用方 cursor 支持增量去重；失败不推进；Feed 缺席不表示删除。

## Tracer-bullet tickets

### T1: 建立安全 Feed fetch 与首个规范化 tracer

- blocked_by: none
- [ ] 公共 fetch seam 对正常 JSON Feed 产生逐 item outcome 和最小 NormalizedJob，并通过 HTTP 边界测试。

### T2: 完成分类矩阵与明确字段提取

- blocked_by: T1
- [ ] 七类样本、冲突 uncertain、remote/internship/part_time/contract/foreign/location/salary 规则通过测试。

### T3: 完成隐私、HTML、partial 与 cursor 语义

- blocked_by: T2
- [ ] 恶意/超长 HTML、联系方式脱敏、畸形 item、重复/cursor 和失败不推进均通过公共 seam 测试。

### T4: 完整验证与独立双轴审查

- blocked_by: T3
- [ ] V2EX 专项、全量测试、compileall、diff-check、scope 检查及 Standards/Spec 审查全部通过。
