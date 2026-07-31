# Task Brief

- task_id: `v2ex-jobs-source`
- title: V2EX 酷工作岗位数据源
- tier: `contract`
- workflow_class: `python-source-change`
- base_sha: `d9c2e8dab030cea0f36b2ded5f7567c53310445e`
- workspace: `linked-worktree`
- owned_paths: docs/research/v2ex-jobs-feed.md, job_sources/v2ex, test_v2ex_source.py, workflow/tasks/v2ex-jobs-source

## Delegation

- subagent_limit: runtime
- parallel_write_isolation: dedicated-worktree

## Cognitive routing

- skills: code-review, codebase-design, research, tdd
- source_refs: https://www.v2ex.com/feed/jobs.json
- notes: none

## Acceptance criteria

- [ ] Define before implementation.

## Plan

- [ ] Define bounded implementation slices.
