# Task Brief

- task_id: `20260804-145553-963840-collect-and-score-greenhouse-and-lev`
- title: Collect and score Greenhouse and Lever jobs
- tier: `contract`
- workflow_class: `app-change`
- base_sha: `311d5e04981992b81a4a3eb3ae8e0723034f351f`
- workspace: `linked-worktree`
- owned_paths: job_sources/ats/__init__.py, job_sources/ats/listings, job_sources/ats/router.py, job_sources/ats/ui, test_ats_detection_web.py, test_ats_job_listings.py, workflow/tasks

## Delegation

- subagent_limit: runtime
- parallel_write_isolation: dedicated-worktree

## Cognitive routing

- skills: codebase-design, research, tdd
- source_refs: official:greenhouse-job-board, official:lever-postings-api
- notes: Official public GET contracts verified before designing two listing adapters.

## Acceptance criteria

- [ ] Define before implementation.

## Plan

- [ ] Define bounded implementation slices.
