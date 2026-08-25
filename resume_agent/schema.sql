PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    display_name TEXT,
    target_roles_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_resumes (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    candidate_id TEXT REFERENCES candidates(id) ON DELETE SET NULL,
    external_job_id TEXT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    salary TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    score REAL,
    qualified INTEGER,
    search_keyword TEXT,
    job_fingerprint TEXT,
    source_logged_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_candidate ON jobs(candidate_id);
CREATE INDEX IF NOT EXISTS idx_jobs_external ON jobs(source, external_job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(candidate_id, job_fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_qualified ON jobs(candidate_id, qualified);

CREATE TABLE IF NOT EXISTS research_reports (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    target_role TEXT NOT NULL,
    job_ids_json TEXT NOT NULL,
    keyword_counts_json TEXT NOT NULL,
    manual_findings_json TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_events (
    id TEXT PRIMARY KEY,
    candidate_id TEXT REFERENCES candidates(id) ON DELETE SET NULL,
    job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    event_at TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_delivery_events_time ON delivery_events(event_at);
CREATE INDEX IF NOT EXISTS idx_delivery_events_candidate ON delivery_events(candidate_id);
CREATE INDEX IF NOT EXISTS idx_delivery_events_type ON delivery_events(event_type);

CREATE TABLE IF NOT EXISTS rewrite_runs (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    job_ids_json TEXT NOT NULL,
    source_resume_ids_json TEXT NOT NULL,
    output_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('QUEUED', 'RUNNING', 'NEEDS_FACTS', 'READY', 'FAILED')),
    fact_answers_json TEXT NOT NULL DEFAULT '[]',
    blocking_questions_json TEXT NOT NULL DEFAULT '[]',
    artifact_dir TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rewrite_runs_candidate
ON rewrite_runs(candidate_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rewrite_runs_status
ON rewrite_runs(status, updated_at);
