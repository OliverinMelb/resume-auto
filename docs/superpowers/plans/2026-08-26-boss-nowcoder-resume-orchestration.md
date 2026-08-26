# Boss / Nowcoder JD 联动与简历生成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Boss 与 Nowcoder 研究分支发现的真实 Agent JD 自动导入 `main`，支持海投综合版、单岗定向版、明确标注的在建项目及验收升级。

**Architecture:** `main` 新增一个只访问 loopback HTTP/JSON 的 `JobDiscoveryOrchestrator`，通过 Boss 与 Nowcoder 适配器读取分支结果，归一化为统一 `DiscoveredJob` 后在主库中持久化。发现流程与现有 `ResumeRewritePipeline` 通过 `job_id` 连接；改写结果增加 `claim_status`、`GapAcceptancePlan` 和配套报告，但 CLI、FastAPI 仍复用同一个核心服务。

**Tech Stack:** Python 3.11+、Pydantic v2、FastAPI、SQLite、stdlib `urllib`、ReportLab、PyMuPDF、`unittest`、本机 Codex CLI。

## Global Constraints

- 首期只实现 `boss` 与 `nowcoder`；ATS、V2EX、Eleduck 和 dev-yifei 不进入业务代码。
- 各分支保留独立 worktree、依赖、本地服务和 SQLite；禁止多个分支共享数据库文件。
- 来源 endpoint 默认只允许 `http://127.0.0.1`、`http://[::1]` 或 `http://localhost`。
- Boss 依赖既有登录态与 Tampermonkey 调研模式；不得绕过验证码、登录或平台风控。
- Nowcoder 首期只保证发现/调研，不宣称自动投递。
- 综合版至少使用 3 个、最多使用 20 个完整且同职业族的真实 JD。
- 单一来源失败但仍有至少 3 个合格 JD 时允许 `PARTIAL`；总样本不足时不得生成综合版 PDF。
- `IN_PROGRESS` 只能出现在“在建项目（待验收）”章节；不得伪造公司、客户、上线状态、生产结果或未测量指标。
- 模型生成的 `test_commands` 只作为学习/验收建议文本保存，产品代码绝不自动执行这些命令。
- 每次改写只导出一份 PDF，并同时生成 `claim-ledger.json`、`gap-roadmap.md`、`ats-report.md`。
- 原始 Markdown、原始 PDF 和任何已有投递版均不得覆盖。
- CLI、审计和错误信息继续递归脱敏手机号、邮箱，不保存 Cookie、Token 或登录会话。
- 所有生产代码必须按 RED → GREEN → REFACTOR 实施；不得先写实现再补测试。

---

### Task 1: 统一 JD 模型、职业族与确定性去重

**Files:**
- Create: `resume_agent/discovery_schemas.py`
- Create: `resume_agent/job_family.py`
- Modify: `resume_agent/rewrite_pipeline.py`（用共享职业族函数替换文件内私有分类）
- Create: `test_resume_discovery_models.py`
- Modify: `test_resume_rewrite_pipeline.py`

**Interfaces:**
- Produces: `DiscoveredJob`, `DiscoveryRequest`, `DiscoveryRunStatus`, `canonicalize_job_url()`, `discovered_job_fingerprint()`, `deduplicate_discovered_jobs()`, `known_job_family()`。
- Consumes: 无；本任务是所有后续来源适配器和选择逻辑的基础。

- [ ] **Step 1: 写统一模型、URL 规范化、内容指纹和职业族的失败测试**

```python
class DiscoveryModelTests(unittest.TestCase):
    def test_url_identity_removes_fragment_and_tracking_query(self):
        self.assertEqual(
            canonicalize_job_url(
                "HTTPS://Jobs.Example.com/role/7?utm_source=x&jobId=7#detail"
            ),
            "https://jobs.example.com/role/7?jobId=7",
        )

    def test_deduplication_uses_url_then_source_id_then_content(self):
        jobs = [
            discovered(source="boss", source_job_id="7", url="https://x/job/7"),
            discovered(source="boss", source_job_id="8", url="https://x/job/7#top"),
            discovered(source="nowcoder", source_job_id="9", url="https://n/job/9"),
        ]
        self.assertEqual([item.source_job_id for item in deduplicate_discovered_jobs(jobs)], ["7", "9"])

    def test_known_job_family_covers_agent_sales_accounting_and_nursing(self):
        self.assertEqual(known_job_family("AI Agent Engineer"), "ai_engineering")
        self.assertEqual(known_job_family("销售经理"), "sales")
        self.assertEqual(known_job_family("Accountant"), "finance_accounting")
        self.assertEqual(known_job_family("临床护士"), "healthcare_nursing")
        self.assertIsNone(known_job_family("Backend Specialist"))
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest test_resume_discovery_models.py -v`

Expected: import 失败，指出 `resume_agent.discovery_schemas` 或 `resume_agent.job_family` 不存在。

- [ ] **Step 3: 实现严格模型和共享职业族接口**

```python
# resume_agent/discovery_schemas.py
class DiscoveredJob(StrictModel):
    source: Literal["boss", "nowcoder"]
    source_job_id: str = Field(min_length=1, max_length=500)
    canonical_url: str = Field(min_length=1, max_length=8192)
    title: str = Field(min_length=1, max_length=500)
    company: str = Field(default="", max_length=500)
    location: str | None = Field(default=None, max_length=500)
    salary_raw: str | None = Field(default=None, max_length=200)
    detail: str = Field(min_length=1, max_length=100_000)
    detail_status: Literal["complete", "partial"]
    search_keyword: str = Field(min_length=1, max_length=500)
    published_at: datetime | None = None
    verified_at: datetime
    score: float | None = Field(default=None, ge=0, le=100)
    qualified: bool | None = None
    source_metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("verified_at", "published_at")
    @classmethod
    def utc_timestamps(cls, value):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("discovery timestamps must be UTC")
        return value

class DiscoveryRequest(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=200)
    sources: list[Literal["boss", "nowcoder"]] = Field(min_length=1, max_length=2)
    keywords: list[str] = Field(min_length=1, max_length=10)
    target_family: Literal["ai_engineering"] = "ai_engineering"
    city: str | None = Field(default=None, max_length=100)
    recruitment_channel: Literal["internship", "campus", "social"] = "social"
    limit_per_source: int = Field(default=10, ge=1, le=20)
    boss_project_id: str | None = Field(default=None, max_length=200)
    nowcoder_acquisition_mode: Literal["public", "browser"] = "public"

    @field_validator("sources", "keywords")
    @classmethod
    def unique_in_order(cls, values):
        if len(values) != len(dict.fromkeys(values)):
            raise ValueError("discovery inputs must be unique")
        return values

DiscoveryRunStatus = Literal[
    "CREATED", "COLLECTING", "READY_FOR_IMPORT", "PARTIAL", "IMPORTED", "FAILED"
]
```

`canonicalize_job_url()` 只保留业务查询参数，移除 `utm_*`、fragment，并统一 scheme/host 大小写。`deduplicate_discovered_jobs()` 保留输入顺序中的第一条完整记录。`known_job_family()` 从现有 `rewrite_pipeline.py` 迁移 AI、销售、财会、医护词表，现有跨族闸门改为调用共享函数。

- [ ] **Step 4: 运行模型测试和现有多 JD 测试并确认 GREEN**

Run: `python -m unittest test_resume_discovery_models.py test_resume_rewrite_pipeline.RewritePipelineTests.test_known_cross_family_job_titles_block_before_model test_resume_rewrite_pipeline.RewritePipelineTests.test_coherent_and_unknown_engineering_job_titles_reach_model -v`

Expected: 5 tests pass。

- [ ] **Step 5: 提交**

```powershell
git add -- resume_agent/discovery_schemas.py resume_agent/job_family.py resume_agent/rewrite_pipeline.py test_resume_discovery_models.py test_resume_rewrite_pipeline.py
git commit -m "feat: define canonical discovered jobs"
```

---

### Task 2: Loopback JSON 客户端与 Boss / Nowcoder 适配器

**Files:**
- Create: `resume_agent/discovery_http.py`
- Create: `resume_agent/discovery_sources.py`
- Create: `test_resume_discovery_sources.py`

**Interfaces:**
- Consumes: `DiscoveredJob`, `DiscoveryRequest`。
- Produces: `LocalJsonClient.request_json(method, path, payload)`, `SourceCheckpoint`, `SourcePollResult`, `BossResearchAdapter.start()/poll()/resolve_url()`, `NowcoderResearchAdapter.start()/poll()/resolve_url()`。

- [ ] **Step 1: 写 loopback 限制和两个真实 payload 映射的失败测试**

```python
def test_local_client_rejects_non_loopback_endpoint(self):
    with self.assertRaisesRegex(ValueError, "loopback"):
        LocalJsonClient("https://jobs.example.com")

def test_boss_poll_maps_complete_candidate_jobs(self):
    transport = FakeTransport(get_results={
        "/resume-agent/v1/candidates/boss-project/jobs?qualified_only=true": {
            "jobs": [{
                "external_job_id": "https://www.zhipin.com/job_detail/7.html",
                "title": "AI Agent开发工程师",
                "company": "示例科技",
                "salary": "25-40K",
                "detail": "负责Agent工作流与RAG评测",
                "search_keyword": "AI Agent",
                "source_logged_at": "2026-08-26T01:00:00+00:00",
                "score": None,
                "qualified": True,
            }]
        }
    })
    result = BossResearchAdapter(LocalJsonClient("http://127.0.0.1:8001", transport=transport)).poll(
        SourceCheckpoint(source="boss", remote_ids=("boss-project",), state={})
    )
    self.assertEqual(result.jobs[0].detail_status, "complete")

def test_nowcoder_completed_task_maps_only_complete_details(self):
    # FakeTransport 返回 GET /tasks/nc-task 和 GET /tasks/nc-task/jobs；
    # description 非空的岗位映射为 complete，partial detail 保留但后续会被拒绝。
    self.assertEqual(result.status, "completed")
    self.assertEqual([job.detail_status for job in result.jobs], ["complete", "partial"])
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest test_resume_discovery_sources.py -v`

Expected: import 失败，指出 `resume_agent.discovery_http` 不存在。

- [ ] **Step 3: 实现可注入 transport 的本机 JSON 客户端**

```python
class LocalJsonClient:
    def __init__(self, base_url: str, *, timeout_seconds: int = 30, transport=None):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or not _is_loopback_host(parsed.hostname):
            raise ValueError("discovery endpoint must be loopback HTTP")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport or UrllibJsonTransport()

    def request_json(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("discovery path must be absolute and local")
        return self.transport.request_json(
            method, self.base_url + path, payload, self.timeout_seconds
        )
```

- [ ] **Step 4: 实现来源协议、Boss 轮询和 Nowcoder 任务生命周期**

```python
class DiscoverySourceAdapter(Protocol):
    source: Literal["boss", "nowcoder"]
    def start(self, request: DiscoveryRequest) -> SourceCheckpoint:
        raise NotImplementedError
    def poll(self, checkpoint: SourceCheckpoint) -> SourcePollResult:
        raise NotImplementedError
    def resolve_url(self, url: str, keyword: str) -> DiscoveredJob:
        raise NotImplementedError

@dataclass(frozen=True)
class SourcePollResult:
    source: str
    status: Literal["collecting", "completed", "interaction_required", "failed"]
    jobs: tuple[DiscoveredJob, ...]
    checkpoint: SourceCheckpoint
    error_code: str | None = None

@dataclass(frozen=True)
class SourceCheckpoint:
    source: Literal["boss", "nowcoder"]
    remote_ids: tuple[str, ...]
    state: dict[str, str | int | float | bool | None]

    def to_dict(self) -> dict:
        return {"source": self.source, "remote_ids": list(self.remote_ids), "state": self.state}

    @classmethod
    def from_dict(cls, payload: dict) -> "SourceCheckpoint":
        return cls(
            source=payload["source"],
            remote_ids=tuple(payload["remote_ids"]),
            state=dict(payload.get("state", {})),
        )
```

Boss `start()` 要求已有 `boss_project_id`，在 checkpoint 保存该 ID；`poll()` 调用候选项目岗位列表并按 `keywords` 过滤。Nowcoder `start()` 为每个 keyword POST 一次 `/job-sources/nowcoder/tasks`，固定 `mode=research`，将全部 `task_id` 保存为 `remote_ids`；`poll()` 聚合所有任务，将 `waiting_for_login`、`verification_required` 映射为 `interaction_required`，全部 `completed` 后读取各 `/tasks/{task_id}/jobs`。

- [ ] **Step 5: 运行来源测试并确认 GREEN**

Run: `python -m unittest test_resume_discovery_sources.py -v`

Expected: loopback、Boss 映射、Nowcoder 状态和 partial 映射测试全部通过，且测试不访问互联网。

- [ ] **Step 6: 提交**

```powershell
git add -- resume_agent/discovery_http.py resume_agent/discovery_sources.py test_resume_discovery_sources.py
git commit -m "feat: adapt local Boss and Nowcoder research"
```

---

### Task 3: 发现运行持久化与受限产物

**Files:**
- Modify: `resume_agent/schema.sql`
- Modify: `resume_agent/repository.py`
- Create: `resume_agent/discovery_artifacts.py`
- Create: `test_resume_discovery_repository.py`

**Interfaces:**
- Consumes: `DiscoveryRequest`, `SourceCheckpoint`, `DiscoveredJob`。
- Produces: Repository 的 `create_discovery_run()`, `get_discovery_run()`, `transition_discovery_run()`；`DiscoveryArtifacts.create()/open()/write_results()/append_audit()`。

- [ ] **Step 1: 写旧数据库迁移、状态 CAS 和产物白名单的失败测试**

```python
def test_discovery_run_round_trips_json_and_status(self):
    run = self.repository.create_discovery_run(
        "discovery_run_" + "a" * 32,
        candidate_id=self.candidate["id"],
        request={"sources": ["boss", "nowcoder"]},
        artifact_dir=str(self.root / "analysis" / "runs" / ("discovery_run_" + "a" * 32)),
    )
    updated = self.repository.transition_discovery_run(
        run["id"], {"CREATED"}, "COLLECTING",
        source_checkpoints={"boss": {"source": "boss", "remote_ids": ["p1"], "state": {}}},
    )
    self.assertEqual(updated["status"], "COLLECTING")

def test_discovery_artifacts_reject_unknown_file(self):
    artifacts = DiscoveryArtifacts.create(self.runs_root, self.run_id)
    with self.assertRaisesRegex(ValueError, "artifact"):
        artifacts.write_text("cookie.txt", "secret")
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest test_resume_discovery_repository.py -v`

Expected: `discovery_runs` 表或 `DiscoveryArtifacts` 不存在。

- [ ] **Step 3: 增加 discovery_runs schema 与兼容迁移**

```sql
CREATE TABLE IF NOT EXISTS discovery_runs (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'CREATED','COLLECTING','READY_FOR_IMPORT','PARTIAL','IMPORTED','FAILED'
    )),
    source_checkpoints_json TEXT NOT NULL DEFAULT '{}',
    source_statuses_json TEXT NOT NULL DEFAULT '{}',
    imported_job_ids_json TEXT NOT NULL DEFAULT '[]',
    artifact_dir TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Repository JSON 解码字段固定为 `request`、`source_checkpoints`、`source_statuses`、`imported_job_ids`。状态迁移使用按 `expected_statuses` 数量展开的 `WHERE id=? AND status IN (?, ?)` 形式，失败时抛出稳定 `ValueError`。

- [ ] **Step 4: 实现独立发现产物白名单和原子写**

```python
DISCOVERY_ARTIFACTS = {
    "input-snapshot.json", "source-results.json", "discovery-report.md", "audit.md"
}
DISCOVERY_RUN_ID = re.compile(r"^discovery_run_[0-9a-f]{32}$")
```

`audit.md` 每行只允许 run ID、状态、事件码、时间戳、来源名、数量和安全错误码；不得写原始 JD、Cookie、URL query secret 或个人联系方式。

- [ ] **Step 5: 运行测试并确认 GREEN**

Run: `python -m unittest test_resume_discovery_repository.py test_resume_rewrite_repository.py -v`

Expected: 新测试及原 Repository 回归全部通过。

- [ ] **Step 6: 提交**

```powershell
git add -- resume_agent/schema.sql resume_agent/repository.py resume_agent/discovery_artifacts.py test_resume_discovery_repository.py
git commit -m "feat: persist JD discovery runs"
```

---

### Task 4: 发现编排器、恢复轮询与来源降级

**Files:**
- Create: `resume_agent/discovery_orchestrator.py`
- Create: `test_resume_discovery_orchestrator.py`

**Interfaces:**
- Consumes: `Repository`, `DiscoveryArtifacts`, `dict[str, DiscoverySourceAdapter]`。
- Produces: `JobDiscoveryOrchestrator.start(request)`, `.poll(run_id)`, `.get(run_id)`。

- [ ] **Step 1: 写全成功、交互等待、单源失败和全源失败的失败测试**

```python
def test_poll_becomes_partial_when_one_source_fails_but_three_jobs_survive(self):
    adapters = {
        "boss": FakeSource.failed("SOURCE_UNAVAILABLE"),
        "nowcoder": FakeSource.completed(agent_jobs(3)),
    }
    run = orchestrator(adapters).start(discovery_request())
    polled = orchestrator(adapters).poll(run["id"])
    self.assertEqual(polled["status"], "PARTIAL")

def test_poll_stays_collecting_for_boss_interaction(self):
    adapters = {"boss": FakeSource.interaction_required(), "nowcoder": FakeSource.completed(agent_jobs(2))}
    polled = start_and_poll(adapters)
    self.assertEqual(polled["status"], "COLLECTING")
    self.assertFalse((Path(polled["artifact_dir"]) / "resume.pdf").exists())
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest test_resume_discovery_orchestrator.py -v`

Expected: `JobDiscoveryOrchestrator` 不存在。

- [ ] **Step 3: 实现唯一编排状态计算**

```python
class JobDiscoveryOrchestrator:
    def __init__(self, repository, runs_root: Path, adapters: dict[str, DiscoverySourceAdapter]):
        self.repository = repository
        self.runs_root = Path(runs_root)
        self.adapters = adapters

    def start(self, request: DiscoveryRequest) -> dict:
        self.repository.get_candidate(request.candidate_id)
        run_id = new_id("discovery_run")
        artifacts = DiscoveryArtifacts.create(self.runs_root, run_id)
        run = self.repository.create_discovery_run(
            run_id,
            candidate_id=request.candidate_id,
            request=request.model_dump(mode="json"),
            artifact_dir=str(artifacts.directory),
        )
        checkpoints = {
            source: self.adapters[source].start(request).to_dict()
            for source in request.sources
        }
        return self.repository.transition_discovery_run(
            run_id,
            {"CREATED"},
            "COLLECTING",
            source_checkpoints=checkpoints,
            source_statuses={source: "collecting" for source in request.sources},
        )

    def poll(self, run_id: str) -> dict:
        run = self.repository.get_discovery_run(run_id)
        artifacts = DiscoveryArtifacts.open(self.runs_root, run_id)
        jobs: list[DiscoveredJob] = []
        checkpoints: dict[str, dict] = {}
        statuses: dict[str, str] = {}
        for source, raw_checkpoint in run["source_checkpoints"].items():
            result = self.adapters[source].poll(SourceCheckpoint.from_dict(raw_checkpoint))
            jobs.extend(result.jobs)
            checkpoints[source] = result.checkpoint.to_dict()
            statuses[source] = result.status
        unique_jobs = deduplicate_discovered_jobs(jobs)
        artifacts.write_results(unique_jobs)
        status = _resolve_discovery_status(statuses, unique_jobs)
        return self.repository.transition_discovery_run(
            run_id,
            {"COLLECTING", "READY_FOR_IMPORT", "PARTIAL"},
            status,
            source_checkpoints=checkpoints,
            source_statuses=statuses,
        )
```

状态规则必须集中在 `_resolve_discovery_status()`：仍有可恢复/交互来源且样本不足为 `COLLECTING`；所有来源完成且至少 3 条为 `READY_FOR_IMPORT`；任一来源失败但至少 3 条为 `PARTIAL`；所有来源终止且不足 3 条为 `FAILED`。

- [ ] **Step 4: 实现同 run ID 恢复和幂等 poll**

重复 `poll()` 读取已保存 checkpoint，不创建第二个来源任务；相同 `DiscoveredJob` 不重复写入结果。任何适配器异常只能转成来源安全错误码，不能把异常正文写入审计。

- [ ] **Step 5: 运行编排器与来源测试并确认 GREEN**

Run: `python -m unittest test_resume_discovery_orchestrator.py test_resume_discovery_sources.py -v`

Expected: 所有状态表格测试通过，无网络调用。

- [ ] **Step 6: 提交**

```powershell
git add -- resume_agent/discovery_orchestrator.py test_resume_discovery_orchestrator.py
git commit -m "feat: orchestrate resumable JD discovery"
```

---

### Task 5: 导入、代表 JD 选择与综合版改写交接

**Files:**
- Create: `resume_agent/discovery_selection.py`
- Modify: `resume_agent/repository.py`
- Modify: `resume_agent/discovery_orchestrator.py`
- Create: `test_resume_discovery_selection.py`
- Modify: `test_resume_discovery_orchestrator.py`

**Interfaces:**
- Consumes: `DiscoveredJob`, `ResumeRewritePipeline`, 现有 `RewriteRequest`。
- Produces: `filter_complete_agent_jobs()`, `select_representative_jobs(minimum=3, maximum=20)`, Repository `create_job_if_absent()`, orchestrator `.import_results()` 与 `.build_broad_rewrite()`。

- [ ] **Step 1: 写过滤、来源多样性、样本下限和幂等导入的失败测试**

```python
def test_selection_rejects_partial_and_cross_family_jobs(self):
    selected, rejected = select_representative_jobs([
        job("AI Agent工程师", source="boss"),
        job("RAG应用工程师", source="nowcoder"),
        job("智能体开发", source="boss"),
        job("护士", source="nowcoder"),
        job("Agent平台", source="boss", detail_status="partial"),
    ])
    self.assertEqual([item.title for item in selected], ["AI Agent工程师", "RAG应用工程师", "智能体开发"])
    self.assertEqual({item.reason for item in rejected}, {"cross_family", "partial_detail"})

def test_broad_rewrite_requires_three_jobs_and_preserves_job_ids(self):
    with self.assertRaisesRegex(ValueError, "at least 3"):
        orchestrator.build_broad_rewrite(run_with_jobs(2), candidate_id, resume_ids, pipeline)
    rewrite = orchestrator.build_broad_rewrite(run_with_jobs(3), candidate_id, resume_ids, pipeline)
    self.assertEqual(pipeline.request.job_ids, imported_job_ids)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest test_resume_discovery_selection.py test_resume_discovery_orchestrator.py -v`

Expected: 选择和 broad handoff 接口不存在。

- [ ] **Step 3: 实现确定性选择和拒绝理由**

选择排序键固定为：完整正文、`qualified=True`、来源多样性、公司多样性、`verified_at` 新鲜度、正文完整度、稳定指纹。任何已知非 AI 工程职业族拒绝；未知标题只有正文明确命中 Agent/RAG/LLM/工作流时才可进入综合样本。

- [ ] **Step 4: 实现 Repository 幂等导入与 broad handoff**

```python
def create_job_if_absent(self, payload: dict[str, Any], fingerprint: str) -> tuple[dict, bool]:
    candidate_id = payload["candidate_id"]
    self.get_candidate(candidate_id)
    job_id = new_id("job")
    with self._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM jobs WHERE candidate_id=? AND job_fingerprint=? LIMIT 1",
            (candidate_id, fingerprint),
        ).fetchone()
        if row is not None:
            return dict(row), False
        connection.execute(
            """INSERT INTO jobs(
                id,candidate_id,external_job_id,source,title,salary,detail,company,
                score,qualified,search_keyword,job_fingerprint,source_logged_at,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id, candidate_id, payload.get("external_job_id"), payload["source"],
                payload["title"], payload.get("salary", ""), payload["detail"],
                payload.get("company", ""), payload.get("score"), payload.get("qualified"),
                payload.get("search_keyword"), fingerprint, payload.get("source_logged_at"),
                utc_now(),
            ),
        )
    return self.get_job(job_id), True

def build_broad_rewrite(self, run_id, candidate_id, source_resume_ids, pipeline):
    run = self.import_results(run_id)
    if len(run["imported_job_ids"]) < 3:
        raise ValueError("broad rewrite requires at least 3 complete jobs")
    return pipeline.run(RewriteRequest(
        candidate_id=candidate_id,
        job_ids=run["imported_job_ids"][:20],
        source_resume_ids=source_resume_ids,
        output_name=_broad_output_name(self.repository.get_candidate(candidate_id)),
    ))
```

- [ ] **Step 5: 运行选择、编排和 rewrite pipeline 回归并确认 GREEN**

Run: `python -m unittest test_resume_discovery_selection.py test_resume_discovery_orchestrator.py test_resume_rewrite_pipeline.py -v`

Expected: 新测试和全部 pipeline 测试通过。

- [ ] **Step 6: 提交**

```powershell
git add -- resume_agent/discovery_selection.py resume_agent/repository.py resume_agent/discovery_orchestrator.py test_resume_discovery_selection.py test_resume_discovery_orchestrator.py
git commit -m "feat: hand selected JD sets to resume rewrites"
```

---

### Task 6: 定向 JD 文本、文件、URL 与 job_id 解析

**Files:**
- Create: `resume_agent/targeted_jobs.py`
- Create: `test_resume_targeted_jobs.py`
- Modify: `resume_agent/rewrite_cli.py`（只加入可复用的文件边界 helper，命令在 Task 11 接线）

**Interfaces:**
- Consumes: `Repository`, `dict[str, DiscoverySourceAdapter]`, 现有 `parse_raw_job()`。
- Produces: `JobTarget`；`TargetedJobResolver.resolve(target: JobTarget, candidate_id: str) -> str`，返回主库 `job_id`。

- [ ] **Step 1: 写四种输入和不完整 URL 拒绝的失败测试**

```python
def test_existing_job_id_is_reused_only_for_same_candidate(self):
    resolved = self.resolver.resolve(JobTarget(job_id=self.job["id"]), self.candidate["id"])
    self.assertEqual(resolved, self.job["id"])
    with self.assertRaisesRegex(ValueError, "candidate"):
        self.resolver.resolve(JobTarget(job_id=self.job["id"]), self.other_candidate["id"])

def test_direct_text_creates_local_targeted_job(self):
    resolved = self.resolver.resolve(
        JobTarget(jd_text="岗位：Agent工程师\n职责：构建RAG工作流", title="Agent工程师"),
        self.candidate["id"],
    )
    self.assertEqual(self.repository.get_job(resolved)["source"], "targeted_text")

def test_nowcoder_url_resolves_through_source_adapter(self):
    resolved = self.resolver.resolve(
        JobTarget(jd_url="https://www.nowcoder.com/jobs/detail/7"), self.candidate["id"]
    )
    self.assertEqual(self.repository.get_job(resolved)["external_job_id"], "7")

def test_boss_url_interaction_required_does_not_create_job(self):
    before = len(self.repository.list_jobs(self.candidate["id"]))
    with self.assertRaisesRegex(ValueError, "SOURCE_INTERACTION_REQUIRED"):
        self.resolver.resolve(
            JobTarget(jd_url="https://www.zhipin.com/job_detail/7.html"), self.candidate["id"]
        )
    self.assertEqual(len(self.repository.list_jobs(self.candidate["id"])), before)

def test_partial_url_result_is_rejected(self):
    self.nowcoder.result = discovered(detail_status="partial")
    with self.assertRaisesRegex(ValueError, "PARTIAL_JD"):
        self.resolver.resolve(
            JobTarget(jd_url="https://www.nowcoder.com/jobs/detail/8"), self.candidate["id"]
        )
```

每个测试断言 Repository 中的岗位数量，确保失败路径不留下半条记录。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest test_resume_targeted_jobs.py -v`

Expected: `TargetedJobResolver` 不存在。

- [ ] **Step 3: 实现互斥目标模型与 resolver**

```python
class JobTarget(StrictModel):
    job_id: str | None = None
    jd_text: str | None = Field(default=None, max_length=100_000)
    jd_url: str | None = Field(default=None, max_length=8192)
    title: str | None = Field(default=None, max_length=500)
    company: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def exactly_one_target(self):
        supplied = [self.job_id is not None, self.jd_text is not None, self.jd_url is not None]
        if sum(supplied) != 1:
            raise ValueError("exactly one targeted JD input is required")
        return self
```

文件路径只在 CLI 边界读取为 `jd_text`；核心 resolver 不接收 `Path`。URL 根据 host 路由到来源适配器，无法完整读取时返回稳定 `SOURCE_INTERACTION_REQUIRED` 或 `PARTIAL_JD`。

- [ ] **Step 4: 运行定向测试和旧 CLI bootstrap 回归并确认 GREEN**

Run: `python -m unittest test_resume_targeted_jobs.py test_resume_rewrite_cli.py -v`

Expected: 全部通过，旧 `run --jd-path` 行为不变。

- [ ] **Step 5: 提交**

```powershell
git add -- resume_agent/targeted_jobs.py resume_agent/rewrite_cli.py test_resume_targeted_jobs.py
git commit -m "feat: resolve targeted JD inputs"
```

---

### Task 7: 模型输出契约增加经历状态和验收计划

**Files:**
- Modify: `resume_agent/rewrite_schemas.py`
- Modify: `resume_agent/rewrite_prompt.py`
- Modify: `resume_agent/research.py`
- Regenerate: `resume_agent/codex_resume_result.schema.json`
- Modify: `test_codex_runner.py`
- Modify: `test_resume_rewrite_support.py`

**Interfaces:**
- Consumes: 现有 `CodexRewriteResult`, `ClaimAudit`。
- Produces: `ClaimStatus`, `GapAcceptancePlan`, `ClaimAudit.claim_status`, `ClaimAudit.gap_id`, `ClaimAudit.acceptance_id`, `CodexRewriteResult.gap_acceptance_plans`。

- [ ] **Step 1: 写严格 Schema 和 prompt 规则的失败测试**

```python
def test_codex_result_requires_claim_status_and_gap_plans(self):
    payload = valid_result_payload()
    payload["claim_audit"][0]["claim_status"] = "in_progress"
    payload["claim_audit"][0]["gap_id"] = "gap-123456789abc"
    payload["gap_acceptance_plans"] = [{
        "gap_id": "gap-123456789abc",
        "jd_requirements": ["Agent评测"],
        "project_scope": "构建本地评测集",
        "required_artifacts": ["tests/eval_cases.json"],
        "test_commands": ["python -m unittest -v"],
        "acceptance_metrics": ["保存逐案例通过记录"],
        "interview_topics": ["评测集设计"],
    }]
    self.assertEqual(CodexRewriteResult.model_validate(payload).claim_audit[0].claim_status, "in_progress")

def test_task_marks_pending_projects_as_current_not_completed(self):
    task = build_codex_task(
        base_prompt="候选人简历与JD材料",
        fact_answers=[],
        allowed_resume_ids=["source_resume_" + "a" * 32],
        allowed_job_ids=["job_" + "b" * 32],
    )
    self.assertIn("在建项目（待验收）", task)
    self.assertIn("不得写成已上线或已产生业务结果", task)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest test_codex_runner.py test_resume_rewrite_support.py -v`

Expected: 新字段因 `extra=forbid` 被拒绝，prompt 断言失败。

- [ ] **Step 3: 实现类型与跨字段约束**

```python
ClaimStatus = Literal["verified", "reframed", "in_progress"]

class GapAcceptancePlan(StrictModel):
    gap_id: str = Field(pattern=r"^gap-[0-9a-f]{12}$")
    jd_requirements: list[str] = Field(min_length=1, max_length=20)
    project_scope: str = Field(min_length=1, max_length=2000)
    required_artifacts: list[str] = Field(min_length=1, max_length=50)
    test_commands: list[str] = Field(min_length=1, max_length=20)
    acceptance_metrics: list[str] = Field(min_length=1, max_length=20)
    interview_topics: list[str] = Field(default_factory=list, max_length=50)
```

`ClaimAudit` 增加必填 `claim_status`，可选 `gap_id`、`acceptance_id`；`in_progress` 必须有 `gap_id` 且不能有 `acceptance_id`。`verified/reframed` 没有缺口时二者均为空；验收升级项目允许 `acceptance_id`。

- [ ] **Step 4: 更新 prompt 并重新生成静态 JSON Schema**

Run:

```powershell
python -c "import json; from pathlib import Path; from resume_agent.rewrite_schemas import CodexRewriteResult; Path('resume_agent/codex_resume_result.schema.json').write_text(json.dumps(CodexRewriteResult.model_json_schema(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')"
```

Expected: `test_checked_in_schema_matches_the_strict_pydantic_contract` 能比较完全相等。

- [ ] **Step 5: 运行契约测试并确认 GREEN**

Run: `python -m unittest test_codex_runner.py test_resume_rewrite_support.py -v`

Expected: 全部通过。

- [ ] **Step 6: 提交**

```powershell
git add -- resume_agent/rewrite_schemas.py resume_agent/rewrite_prompt.py resume_agent/research.py resume_agent/codex_resume_result.schema.json test_codex_runner.py test_resume_rewrite_support.py
git commit -m "feat: model pending resume projects explicitly"
```

---

### Task 8: 事实门禁验证“在建项目（待验收）”

**Files:**
- Modify: `resume_agent/rewrite_fact_gate.py`
- Modify: `resume_agent/rewrite_pipeline.py`
- Modify: `test_resume_fact_gate.py`
- Modify: `test_resume_rewrite_pipeline.py`

**Interfaces:**
- Consumes: `claim_status`, `gap_id`, `GapAcceptancePlan`。
- Produces: `validate_in_progress_claims()`，并将有效的在建章节纳入现有 `FactGateResult`。

- [ ] **Step 1: 写在建项目允许/拒绝表格测试**

```python
def test_in_progress_claim_language_table(self):
    cases = [
        ("在建：正在实现LangGraph多Agent路由，计划以回归集验收", True),
        ("主导LangGraph多Agent平台上线", False),
        ("为客户上线RAG系统", False),
        ("准确率提升到92%", False),
        ("节省人工成本40%", False),
    ]
    for claim, allowed in cases:
        with self.subTest(claim=claim):
            result = gate_for_in_progress_claim(claim)
            self.assertEqual(not result.blockers, allowed)

def test_in_progress_claim_requires_matching_gap_plan(self):
    result = gate_with_claim_gap("gap-aaaaaaaaaaaa", plan_gap="gap-bbbbbbbbbbbb")
    self.assertTrue(result.blockers)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest test_resume_fact_gate.FactGateTests.test_in_progress_claim_language_table test_resume_fact_gate.FactGateTests.test_in_progress_claim_requires_matching_gap_plan -v`

Expected: 当前 gate 把在建项目当成无简历来源的普通 claim 并阻断，或缺少新 helper。

- [ ] **Step 3: 实现章节识别和在建 claim 专用规则**

```python
IN_PROGRESS_HEADING = "在建项目（待验收）"
IN_PROGRESS_MARKERS = ("在建", "正在", "计划验收", "待验收")
COMPLETED_MARKERS = ("已上线", "已落地", "已交付", "投入生产", "主导完成")
FORBIDDEN_EXTERNAL_ACTORS = ("客户", "甲方", "生产用户")
```

专用验证必须同时满足：处于独立章节、audit 为 `in_progress`、gap plan 一一对应、至少一个进行中标记、没有完成态/外部客户/生产结果、没有业务指标。它不能调用普通 `FactAnswer` 路径解锁。

- [ ] **Step 4: 让 pipeline 在写 PDF 前使用 gate 返回的完整 Markdown**

Pipeline 保持单次模型结果，但只有 gate 通过后才写 `resume.md` 和渲染 PDF。`IN_PROGRESS` 失败与其他事实失败一样进入 `NEEDS_FACTS`，不留下 PDF。

- [ ] **Step 5: 运行 fact gate 与 pipeline 全文件并确认 GREEN**

Run: `python -m unittest test_resume_fact_gate.py test_resume_rewrite_pipeline.py -v`

Expected: 全部通过，现有既有事实门禁不放宽。

- [ ] **Step 6: 提交**

```powershell
git add -- resume_agent/rewrite_fact_gate.py resume_agent/rewrite_pipeline.py test_resume_fact_gate.py test_resume_rewrite_pipeline.py
git commit -m "feat: gate pending resume projects"
```

---

### Task 9: 缺口验收记录与后续升级

**Files:**
- Modify: `resume_agent/schema.sql`
- Modify: `resume_agent/repository.py`
- Modify: `resume_agent/rewrite_schemas.py`
- Modify: `resume_agent/rewrite_prompt.py`
- Modify: `resume_agent/rewrite_pipeline.py`
- Create: `test_resume_gap_acceptance.py`
- Modify: `test_resume_rewrite_repository.py`

**Interfaces:**
- Consumes: `GapAcceptancePlan`, `candidate_id`, 已有 run/gap ID。
- Produces: `GapAcceptanceRequest`, Repository `create_gap_acceptance()/get_gap_acceptance()/list_gap_acceptances()`，pipeline 在 snapshot/prompt 中加载已验收证据。

- [ ] **Step 1: 写确认配对、证据 round-trip 和未验收不升级的失败测试**

```python
def test_gap_acceptance_requires_utc_confirmation_and_test_records(self):
    with self.assertRaises(ValidationError):
        GapAcceptanceRequest.model_validate({
            "gap_id": "gap-123456789abc",
            "project_path": "C:/work/agent-eval",
            "verified_claims": ["实现本地Agent评测集"],
            "artifact_hashes": {},
            "test_records": [],
            "completed_at": "2026-08-26T03:00:00Z",
            "user_confirmed": True,
            "confirmed_at": "2026-08-26T03:01:00Z",
        })

def test_accepted_gap_round_trips_and_enters_snapshot(self):
    accepted = repository.create_gap_acceptance(candidate_id, request.model_dump(mode="json"))
    run = pipeline.run(rewrite_request())
    snapshot = json.loads((Path(run["artifact_dir"]) / "input-snapshot.json").read_text("utf-8"))
    self.assertEqual(snapshot["accepted_gap_ids"], [accepted["id"]])
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest test_resume_gap_acceptance.py test_resume_rewrite_repository.py -v`

Expected: schema、表和 Repository 方法不存在。

- [ ] **Step 3: 增加 gap_acceptances 表和严格证据模型**

```sql
CREATE TABLE IF NOT EXISTS gap_acceptances (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    gap_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id, gap_id)
);
```

```python
class GapAcceptanceRequest(StrictModel):
    gap_id: str = Field(pattern=r"^gap-[0-9a-f]{12}$")
    project_path: str = Field(min_length=1, max_length=2000)
    verified_claims: list[str] = Field(min_length=1, max_length=50)
    artifact_hashes: dict[str, str] = Field(min_length=1, max_length=100)
    test_records: list[TestRecord] = Field(min_length=1, max_length=50)
    completed_at: datetime
    user_confirmed: Literal[True]
    confirmed_at: datetime
```

`TestRecord` 至少包含 `command`、`exit_code`、`output_sha256`；hash 必须是 64 位十六进制，所有时间必须 UTC。
`accept-gap` 只验证并保存用户提供的测试记录，不执行 `command` 字段；真正运行测试由用户或受控开发流程完成，避免把模型文本变成 shell 输入。

- [ ] **Step 4: 将验收证据加入 prompt、snapshot 和 gate**

模型只能把 `verified_claims` 中的内容作为已验收个人项目事实，并在 `ClaimAudit.acceptance_id` 引用该记录。Gate 要求 claim 与一条 `verified_claims` 完整断言匹配；不允许通过验收记录引入未列出的指标。

- [ ] **Step 5: 运行验收、事实门禁和 pipeline 回归并确认 GREEN**

Run: `python -m unittest test_resume_gap_acceptance.py test_resume_rewrite_repository.py test_resume_fact_gate.py test_resume_rewrite_pipeline.py -v`

Expected: 全部通过。

- [ ] **Step 6: 提交**

```powershell
git add -- resume_agent/schema.sql resume_agent/repository.py resume_agent/rewrite_schemas.py resume_agent/rewrite_prompt.py resume_agent/rewrite_pipeline.py test_resume_gap_acceptance.py test_resume_rewrite_repository.py test_resume_fact_gate.py test_resume_rewrite_pipeline.py
git commit -m "feat: promote accepted gap projects"
```

---

### Task 10: Claim ledger、缺口路线图与 ATS 报告

**Files:**
- Create: `resume_agent/rewrite_reports.py`
- Modify: `resume_agent/rewrite_artifacts.py`
- Modify: `resume_agent/rewrite_pipeline.py`
- Create: `test_resume_rewrite_reports.py`
- Modify: `test_resume_rewrite_support.py`
- Modify: `test_resume_rewrite_pipeline.py`

**Interfaces:**
- Consumes: `CodexRewriteResult`, jobs, source resumes, keyword counts, accepted gaps。
- Produces: `build_claim_ledger()`, `build_gap_roadmap()`, `build_ats_report()`；三个新受限产物。

- [ ] **Step 1: 写报告内容、状态一致性和 PII 禁止测试**

```python
def test_reports_keep_claim_status_and_keyword_coverage(self):
    ledger = build_claim_ledger(result, allowed_resume_ids, allowed_job_ids)
    roadmap = build_gap_roadmap(result.gap_acceptance_plans)
    ats = build_ats_report(jobs, resumes, result.rewritten_resume_markdown)
    self.assertEqual(ledger["claims"][0]["claim_status"], "in_progress")
    self.assertIn("待验收", roadmap)
    self.assertIn("修改后覆盖", ats)

def test_report_writers_never_include_phone_email_or_raw_source_metadata(self):
    for text in generated_text_reports():
        self.assertNotRegex(text, r"1[3-9]\d{9}")
        self.assertNotIn("candidate@example.com", text)
        self.assertNotIn("cookie", text.lower())
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest test_resume_rewrite_reports.py test_resume_rewrite_support.py -v`

Expected: 报告 builders 或白名单产物不存在。

- [ ] **Step 3: 实现三个确定性报告 builder**

`claim-ledger.json` 只包含 claim、状态、稳定来源 ID、gap/acceptance ID 和 change type；不复制原始简历全文。`gap-roadmap.md` 从结构化 plan 渲染。`ats-report.md` 复用 `KEYWORD_ALIASES`，分别计算 JD 样本、原简历和最终 Markdown 的确定性关键词集合及覆盖率，并明确“不是语义评分或通过承诺”。

- [ ] **Step 4: 扩展产物白名单并在 READY 前原子写入**

```python
ALLOWED_ARTIFACTS |= {"claim-ledger.json", "gap-roadmap.md", "ats-report.md"}
```

三个报告只在模型 Schema 与事实门禁通过后写入；最终状态 CAS 失败时与 PDF 一起清理。`FAILED` 不保留 PDF、claim ledger、路线图或 ATS 报告，只保留现有安全 audit 与输入哈希快照。

- [ ] **Step 5: 运行报告、support 和 pipeline 测试并确认 GREEN**

Run: `python -m unittest test_resume_rewrite_reports.py test_resume_rewrite_support.py test_resume_rewrite_pipeline.py -v`

Expected: 全部通过。

- [ ] **Step 6: 提交**

```powershell
git add -- resume_agent/rewrite_reports.py resume_agent/rewrite_artifacts.py resume_agent/rewrite_pipeline.py test_resume_rewrite_reports.py test_resume_rewrite_support.py test_resume_rewrite_pipeline.py
git commit -m "feat: audit ATS and gap planning outputs"
```

---

### Task 11: Factory、CLI 与 FastAPI 联动入口

**Files:**
- Modify: `resume_agent/settings.py`
- Create: `resume_agent/discovery_factory.py`
- Modify: `resume_agent/rewrite_cli.py`
- Modify: `resume_agent/router.py`
- Modify: `resume_agent/schemas.py`
- Modify: `resume_agent.env.example`
- Modify: `test_resume_rewrite_cli.py`
- Modify: `test_resume_rewrite_api.py`
- Create: `test_resume_discovery_api.py`

**Interfaces:**
- Consumes: Discovery orchestrator、target resolver、rewrite pipeline、gap acceptance Repository。
- Produces: CLI `discover`, `poll-discovery`, `broad`, `targeted`, `accept-gap`；FastAPI discovery/target/accept endpoints。

- [ ] **Step 1: 写 CLI 参数、退出码、递归脱敏和 API 路由失败测试**

```python
def test_discover_command_starts_boss_and_nowcoder_without_jd_path(self):
    exit_code, payload = run_cli([
        "discover", "--candidate-id", self.candidate_id,
        "--sources", "boss", "nowcoder",
        "--keyword", "AI Agent",
        "--boss-project-id", "boss-project",
        "--city", "上海", "--limit", "10",
    ])
    self.assertEqual(exit_code, 3)
    self.assertEqual(payload["status"], "COLLECTING")

def test_broad_command_hands_imported_jobs_to_one_rewrite(self):
    exit_code, payload = run_cli([
        "broad", "--discovery-run-id", self.discovery_run_id,
        "--candidate-id", self.candidate_id,
        "--source-resume-id", self.resume_id,
    ])
    self.assertEqual(self.pipeline.request.job_ids, self.imported_job_ids)
    self.assertEqual(payload["id"], self.pipeline.result["id"])

def test_targeted_command_accepts_url_or_path_but_not_both(self):
    exit_code, payload = run_cli([
        "targeted", "--candidate-id", self.candidate_id,
        "--source-resume-id", self.resume_id,
        "--jd-url", "https://www.nowcoder.com/jobs/detail/7",
        "--jd-path", str(self.root / "target.md"),
    ])
    self.assertEqual(exit_code, 1)
    self.assertEqual(payload["status"], "FAILED")

def test_accept_gap_command_never_echoes_project_path_or_contact_data(self):
    exit_code, payload, stdout = run_cli_with_stdout([
        "accept-gap", "--candidate-id", self.candidate_id,
        "--evidence-json", str(self.evidence_path),
    ])
    self.assertEqual(exit_code, 0)
    self.assertNotIn("candidate@example.com", stdout)
    self.assertNotIn(str(self.private_project_path), stdout)

def test_discovery_routes_are_local_and_share_core_orchestrator(self):
    response = self.client.post("/resume-agent/v1/discovery-runs", json=self.request_payload)
    self.assertEqual(response.status_code, 201)
    self.assertEqual(self.orchestrator.start_calls, 1)
    remote = self.remote_client.post("/resume-agent/v1/discovery-runs", json=self.request_payload)
    self.assertEqual(remote.status_code, 403)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest test_resume_rewrite_cli.py test_resume_rewrite_api.py test_resume_discovery_api.py -v`

Expected: 新 subcommand 和路由不存在。

- [ ] **Step 3: 增加 endpoint settings 与 factory**

```python
@dataclass(frozen=True)
class ResumeAgentSettings:
    database_path: Path
    legacy_action_log_path: Path
    legacy_decision_log_path: Path
    allow_remote: bool
    runs_root: Path
    codex_timeout_seconds: int
    boss_research_endpoint: str
    nowcoder_research_endpoint: str
    discovery_timeout_seconds: int
```

默认 endpoint 分别为 `http://127.0.0.1:8011` 与 `http://127.0.0.1:8012`；timeout 限制 1～120 秒。Factory 创建两个 `LocalJsonClient`、适配器和唯一 orchestrator。

- [ ] **Step 4: 接入 CLI 子命令与稳定退出码**

```text
discover/poll-discovery:
  READY_FOR_IMPORT, PARTIAL, IMPORTED -> 0
  COLLECTING -> 3
  FAILED -> 1

broad/targeted:
  READY -> 0
  NEEDS_FACTS -> 2
  FAILED -> 1
```

`broad` 必须使用 discovery run，不接受任意 JD 路径；`targeted` 必须只接受一个 `--job-id`、`--jd-path` 或 `--jd-url`。所有输出继续走 `_redact_nested()`。

- [ ] **Step 5: 接入本机 FastAPI 路由**

```text
POST /resume-agent/v1/discovery-runs
GET  /resume-agent/v1/discovery-runs/{run_id}
POST /resume-agent/v1/discovery-runs/{run_id}/poll
POST /resume-agent/v1/discovery-runs/{run_id}/broad-rewrite
POST /resume-agent/v1/targeted-rewrite
POST /resume-agent/v1/gap-acceptances
```

继续复用 `require_local_request`，不得为来源联动放宽远程访问。

- [ ] **Step 6: 运行 CLI/API 全文件并确认 GREEN**

Run: `python -m unittest test_resume_rewrite_cli.py test_resume_rewrite_api.py test_resume_discovery_api.py -v`

Expected: 全部通过。

- [ ] **Step 7: 提交**

```powershell
git add -- resume_agent/settings.py resume_agent/discovery_factory.py resume_agent/rewrite_cli.py resume_agent/router.py resume_agent/schemas.py resume_agent.env.example test_resume_rewrite_cli.py test_resume_rewrite_api.py test_resume_discovery_api.py
git commit -m "feat: expose linked JD discovery workflows"
```

---

### Task 12: 文档、分支运行准备、真实来源 smoke 与最终验证

**Files:**
- Modify: `readme.md`
- Modify: `.gitignore`
- Create: `docs/boss-nowcoder-resume-workflow.md`
- Modify: `test_resume_agent_p0.py`

**Interfaces:**
- Consumes: Tasks 1～11 的 CLI/API。
- Produces: 可复现的 Boss/Nowcoder 本机运行指南、真实 smoke 证据和最终回归报告。

- [ ] **Step 1: 写文档静态契约失败测试**

```python
def test_readme_documents_linked_broad_and_targeted_modes(self):
    text = Path("readme.md").read_text("utf-8")
    self.assertIn("rewrite_resume.bat discover", text)
    self.assertIn("rewrite_resume.bat broad", text)
    self.assertIn("rewrite_resume.bat targeted", text)
    self.assertIn("在建项目（待验收）", text)
    self.assertNotIn("必须先创建 target-jd.md", text)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest test_resume_agent_p0.ResumeAgentP0Tests.test_readme_documents_linked_broad_and_targeted_modes -v`

Expected: README 尚未包含新命令。

- [ ] **Step 3: 写运行文档和能力边界**

文档必须给出：两个独立 worktree/端口、Boss 调研按钮、Nowcoder public research、discovery run 恢复、综合版生成、定向版生成、gap 验收、Boss `resumeIndex` 手工绑定，以及 Nowcoder 首期不自动投递。

- [ ] **Step 4: 准备独立来源 worktree，不共享数据库**

```powershell
Set-Location 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt'
git worktree add --detach 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-boss-source' origin/codex/boss-dom-resilience
git worktree add --detach 'C:\Users\Administrator\Desktop\ai-helper\jd-prompt-nowcoder-source' origin/nowcoder
```

为两个服务分别设置不同的 `RESUME_AGENT_DB_PATH` 和端口；启动命令写入本地运行报告，不提交数据库、日志、checkpoint 或真实 JD。

- [ ] **Step 5: 运行 Nowcoder 公开来源 smoke**

使用关键词 `AI Agent`、上限 10、research mode 创建任务并轮询。验证返回字段能映射为 `DiscoveredJob`；如果上游网络、验证页或公开接口阻塞，记录安全错误码和降级状态，不伪造成功。

- [ ] **Step 6: 运行 Boss 登录态 smoke**

启动 Boss 分支服务后，由用户在已登录页面选择调研模式并采集至少 1 条完整 JD。主线 `discover`/`poll-discovery` 必须看到该记录且不读取 Cookie。若浏览器需要用户操作，保持 `COLLECTING` 并输出 run ID，不把等待当作失败。

- [ ] **Step 7: 用至少 3 条同族真实 JD 运行综合版；不足时停止**

运行 broad 命令并检查：选用 JD 均可追踪、唯一 PDF 不覆盖旧文件、`claim-ledger.json` 与 PDF 状态一致、在建项目明确标注、路线图有验收步骤。若真实完整样本不足 3 条，本步骤的正确结果是“未生成综合版”，不得使用测试 JD 补足。

- [ ] **Step 8: 运行 fresh 全量验证**

Run:

```powershell
python -m unittest discover -p "test*.py" -v
$pyFiles = Get-ChildItem -Path resume_agent -Recurse -Filter *.py | ForEach-Object { $_.FullName }
python -m py_compile $pyFiles
git diff --check origin/main..HEAD
git ls-files -- analysis .superpowers "*.pdf" "*facts*.json" "*.db"
git status --short
```

Expected: 所有测试通过；编译与 diff check exit 0；Git 不跟踪运行数据库、PDF、facts、`analysis/runs` 或来源 checkpoint；工作区除用户原有未跟踪 `analysis/` 外干净。

- [ ] **Step 9: 提交文档**

```powershell
git add -- readme.md .gitignore docs/boss-nowcoder-resume-workflow.md test_resume_agent_p0.py
git commit -m "docs: explain linked JD resume workflow"
```

- [ ] **Step 10: 请求最终代码审查**

审查必须重点重放：非 loopback 拒绝、partial JD 拒绝、少于 3 条不出 PDF、跨族阻断、`IN_PROGRESS` 禁止完成态和无证据指标、验收升级、CLI PII 脱敏、失败运行无可投递 PDF。

- [ ] **Step 11: 审查通过后合入 main、重跑并推送**

在功能 worktree 全绿且最终审查明确 `Ready to merge: Yes` 后，从主工作区 fast-forward 合并功能分支；保留用户未跟踪的 `analysis/`。在合并后的 `main` 再运行 Step 8 的完整验证，成功后执行 `git push origin main`，确认 `git rev-list --left-right --count main...origin/main` 为 `0 0`，最后移除隔离 worktree 和已合并功能分支。
