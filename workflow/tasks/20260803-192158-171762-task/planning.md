# Planning Record

- status: `ready`
- grilling_status: `confirmed`
- multi_session: `false`
- spec_ref: `.scratch/eleduck-public-listing/spec.md`

## Unresolved decisions

- None

## Decisions

- 固定读取最近 20 页，首次最多 500 条；后续整页均已见时提前停止

## Acceptance criteria

- [ ] 匿名公开职位 API 最多读取 20 页；过滤关闭/隐藏/删除项并跨页去重；已见整页提前停止；失败时研究零写入且不推进游标；UI 明确最近 20 页；专项与全库验证通过

## Tracer-bullet tickets

- Workflow dependency tickets: none (`multi_session=false`).
- Local tracker ticket: `.scratch/eleduck-public-listing/issues/T1.md` (`in-progress`).
