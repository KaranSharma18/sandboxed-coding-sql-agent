"""Domain models shared by every service: task state, executions, traces, verdicts and usage.

These are plain Pydantic models with no infrastructure attached. They are the contracts on the
wire (Redis messages between worker and runner), in the database (JSON columns) and in the API.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from fixloop_core.outputs import FailureClass


def utcnow() -> datetime:
    return datetime.now(UTC)


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------------------ enums
class TaskStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (TaskStatus.succeeded, TaskStatus.failed, TaskStatus.cancelled)


class Phase(StrEnum):
    queued = "queued"
    snapshot = "snapshot"
    prepare_env = "prepare_env"
    baseline = "baseline"
    context = "context"
    plan = "plan"
    patch = "patch"
    apply = "apply"
    execute = "execute"
    verify = "verify"
    diagnose = "diagnose"
    repair = "repair"
    finalize = "finalize"
    publish = "publish"
    done = "done"


class Outcome(StrEnum):
    verified = "verified"
    nothing_to_fix = "nothing_to_fix"
    executed_only = "executed_only"
    exhausted_repairs = "exhausted_repairs"
    exhausted_budget = "exhausted_budget"
    no_progress = "no_progress"
    policy_violation = "policy_violation"
    env_failed = "env_failed"
    flaky_oracle = "flaky_oracle"
    invalid_input = "invalid_input"
    infra_error = "infra_error"
    cancelled = "cancelled"

    @property
    def status(self) -> TaskStatus:
        if self in (Outcome.verified, Outcome.nothing_to_fix, Outcome.executed_only):
            return TaskStatus.succeeded
        if self is Outcome.cancelled:
            return TaskStatus.cancelled
        return TaskStatus.failed


class ExecPurpose(StrEnum):
    prepare = "prepare"
    baseline = "baseline"
    baseline_confirm = "baseline_confirm"
    candidate = "candidate"
    hidden = "hidden"
    rerun = "rerun"
    deep_trace = "deep_trace"
    sql = "sql"


class ExecStatus(StrEnum):
    completed = "completed"
    timeout = "timeout"
    oom = "oom"
    killed = "killed"
    infra_error = "infra_error"
    cancelled = "cancelled"


class AttemptKind(StrEnum):
    initial = "initial"
    repair = "repair"
    escalated_repair = "escalated_repair"


class VerificationLevel(StrEnum):
    none = "none"
    executed = "executed"
    # python
    targets_passed = "targets_passed"
    no_regressions = "no_regressions"
    hidden_passed = "hidden_passed"
    # sql
    properties_passed = "properties_passed"
    rows_matched = "rows_matched"
    differential_passed = "differential_passed"


# ------------------------------------------------------------------------------------ storage refs
class ArtifactRef(_M):
    key: str
    sha256: str
    size: int
    content_type: str = "application/octet-stream"


class SnapshotRef(_M):
    key: str
    sha256: str
    size: int
    files: int


class ManifestEntry(_M):
    path: str
    size: int
    sha256: str
    eol: Literal["lf", "crlf", "mixed", "none"] = "none"
    binary: bool = False
    executable: bool = False


class RedactionNote(_M):
    path: str
    kind: str
    line: int


class SnapshotManifest(_M):
    snapshot_sha256: str
    entries: list[ManifestEntry]
    excluded: list[str] = Field(default_factory=list)
    redactions: list[RedactionNote] = Field(default_factory=list)
    secret_files: list[str] = Field(default_factory=list)

    def by_path(self) -> dict[str, ManifestEntry]:
        return {entry.path: entry for entry in self.entries}


# ------------------------------------------------------------------------------------ execution contract
class OverlayFile(_M):
    """A file written over the snapshot for one execution (a candidate's changed file)."""

    path: str
    sha256: str = ""
    content_b64: str = ""
    delete: bool = False


class SeedFile(_M):
    table: str
    key: str
    sha256: str
    format: Literal["csv", "parquet"] = "csv"


class PythonPrepare(_M):
    python_version: str
    requirements: str


class SqlPrepare(_M):
    dialect: Literal["postgres", "duckdb"]
    ddl: str
    seeds: list[SeedFile] = Field(default_factory=list)
    mutations: int = Field(default=0, ge=0, le=8)
    mutation_seed: int = 0
    reference_query: str | None = None


class PrepareSpec(_M):
    family: Literal["python", "sql"]
    image: str
    env_hash: str
    python: PythonPrepare | None = None
    sql: SqlPrepare | None = None


class EnvRef(_M):
    env_hash: str
    image: str
    family: Literal["python", "sql"]
    metadata: dict[str, Any] = Field(default_factory=dict)


class CommandSpec(_M):
    template: str
    args: dict[str, Any] = Field(default_factory=dict)


class Limits(_M):
    timeout_s: int = 120
    memory_mb: int = 2048
    cpus: float = 2.0
    pids: int = 256
    nofile: int = 1024
    fsize_mb: int = 100
    tmpfs_mb: int = 256
    stdout_bytes: int = 1_048_576
    artifacts_bytes: int = 20_971_520


class ExecRequest(_M):
    exec_id: str
    tenant_id: str
    task_id: str
    purpose: ExecPurpose
    prepare: PrepareSpec
    snapshot: SnapshotRef | None = None
    overlay: list[OverlayFile] = Field(default_factory=list)
    extra_files: list[OverlayFile] = Field(default_factory=list)
    hidden_tests: ArtifactRef | None = None
    command: CommandSpec
    limits: Limits = Field(default_factory=Limits)
    protected_hashes: dict[str, str] = Field(default_factory=dict)
    deadline_ts: float = 0.0
    trace_context: dict[str, str] = Field(default_factory=dict)


class ExecResult(_M):
    exec_id: str
    status: ExecStatus
    exit_code: int | None = None
    duration_ms: int = 0
    stdout_tail: str = ""
    stderr_tail: str = ""
    artifacts: dict[str, ArtifactRef] = Field(default_factory=dict)
    truncated: dict[str, bool] = Field(default_factory=dict)
    protected_modified: list[str] = Field(default_factory=list)
    image: str = ""
    image_digest: str = ""
    runner_id: str = ""
    backend: str = ""
    error: str | None = None
    env: EnvRef | None = None
    env_error: str | None = None  # set when the environment could not be prepared (an env_failed outcome)
    cache_hit: bool = False
    sandbox_seconds: float = 0.0


# ------------------------------------------------------------------------------------ traces
class Frame(_M):
    path: str
    line: int
    function: str
    source: str = ""
    in_repo: bool = False


class FailureRecord(_M):
    test_id: str
    phase: Literal["collect", "setup", "call", "teardown"] = "call"
    exc_type: str = ""
    message: str = ""
    assertion_diff: str | None = None
    frames: list[Frame] = Field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""


class TestCounts(_M):
    __test__ = False  # not a pytest test class

    collected: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0


class TraceSummary(_M):
    exec_id: str
    status: ExecStatus
    counts: TestCounts = Field(default_factory=TestCounts)
    failures: list[FailureRecord] = Field(default_factory=list)
    failure_class: FailureClass = FailureClass.unknown
    signature: str = ""
    duration_ms: int = 0
    notes: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------------------------ verdicts
class Verdict(_M):
    passed: bool
    level: VerificationLevel = VerificationLevel.none
    failure_class: FailureClass | None = None
    message: str = ""
    targets_failed: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    preexisting_failures: list[str] = Field(default_factory=list)
    flaky_tests: list[str] = Field(default_factory=list)
    hidden_failed_count: int = 0
    stable: bool = False
    warnings: list[str] = Field(default_factory=list)
    regression_strategy: str | None = None
    regression_share: float | None = None
    coverage: dict[str, Any] | None = None
    sql: dict[str, Any] | None = None
    counts: TestCounts | None = None


# ------------------------------------------------------------------------------------ usage and budgets
class Usage(_M):
    model_calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cost_usd: float = 0.0
    sandbox_seconds: float = 0.0
    executions: int = 0

    def add(self, other: Usage) -> Usage:
        return Usage(
            model_calls=self.model_calls + other.model_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            thinking_tokens=self.thinking_tokens + other.thinking_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 6),
            sandbox_seconds=round(self.sandbox_seconds + other.sandbox_seconds, 3),
            executions=self.executions + other.executions,
        )


class BudgetLimits(_M):
    max_repairs: int = 3
    max_model_calls: int = 12
    max_input_tokens: int = 300_000
    max_output_tokens: int = 40_000
    max_cost_usd: float = 0.75
    max_sandbox_seconds: int = 600
    max_executions: int = 10
    wall_clock_seconds: int = 900
    format_reasks_per_step: int = 2


# ------------------------------------------------------------------------------------ attempts and results
class AttemptRecord(_M):
    attempt_no: int
    kind: AttemptKind
    model: str = ""
    plan: dict[str, Any] | None = None
    diagnosis: str | None = None
    edits: list[dict[str, Any]] = Field(default_factory=list)
    fingerprint: str = ""
    files_after: dict[str, str] = Field(default_factory=dict)
    verdict: Verdict | None = None
    failure_class: FailureClass | None = None
    failure_signature: str = ""
    signature_repeated: bool = False
    progress_score: list[float] = Field(default_factory=list)
    cost_usd: float = 0.0
    exec_ids: list[str] = Field(default_factory=list)
    diff_key: str | None = None
    query: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    def summary_line(self) -> str:
        verdict = self.verdict.message if self.verdict else "not executed"
        return f"attempt {self.attempt_no} ({self.kind.value}): {self.failure_class or 'ok'} - {verdict}"


class TaskResult(_M):
    outcome: Outcome
    reason: str = ""
    verification: Verdict | None = None
    final_attempt_no: int | None = None
    best_attempt_no: int | None = None
    attempts: int = 0
    patch_key: str | None = None
    patch_summary: str = ""
    query: str | None = None
    preview: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    budget_dimension: str | None = None
    repeated_signature: str | None = None
    unverified: bool = False
    cache: str | None = None


class OutboxItem(_M):
    kind: Literal["github_publish", "github_comment", "github_command", "customer_webhook"]
    payload: dict[str, Any]
    idempotency_key: str


class TaskEvent(_M):
    task_id: str
    seq: int
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


# ------------------------------------------------------------------------------------ loop checkpoint
class LoopState(_M):
    """Everything the driver needs to resume a task on another worker after a crash."""

    phase: Phase = Phase.snapshot
    snapshot: SnapshotRef | None = None
    manifest_key: str | None = None
    env: EnvRef | None = None
    prepare: PrepareSpec | None = None
    baseline_exec_ids: list[str] = Field(default_factory=list)
    regression_strategy: str | None = None
    regression_node_ids: list[str] = Field(default_factory=list)
    baseline_status: dict[str, str] = Field(default_factory=dict)
    baseline_collected: list[str] = Field(default_factory=list)
    baseline_collection_errors: list[str] = Field(default_factory=list)
    baseline_duration_s: float = 0.0
    regression_share: float | None = None
    baseline_signature: str = ""
    baseline_summary_key: str | None = None
    next_attempt_no: int = 0
    best_attempt_no: int | None = None
    best_files: dict[str, str] = Field(
        default_factory=dict
    )  # path -> sha256 of the best state's changed files
    best_overlay_key: str | None = None  # object-store key of those files' contents
    plan: dict[str, Any] | None = None
    extra_files: list[str] = Field(default_factory=list)
    signatures: list[str] = Field(default_factory=list)
    fingerprints: list[str] = Field(default_factory=list)
    policy_streak: int = 0
    escalate_next: bool = False
    deep_trace_key: str | None = None
    started_at: float = 0.0
    hidden_feedback: str | None = None
    sql_schema: dict[str, Any] | None = None
    # ---- resume bookkeeping
    exec_done: list[str] = Field(
        default_factory=list
    )  # exec ids already billed (a resumed run is not re-billed)
    pending: dict[str, Any] = Field(
        default_factory=dict
    )  # the current attempt's model outputs, saved before running
    # ---- python inputs fixed at intake
    requirements: str = ""
    protected_globs: list[str] = Field(default_factory=list)
    protected_hashes: dict[str, str] = Field(default_factory=dict)
    hidden_tests: ArtifactRef | None = None
    hidden_files: list[str] = Field(default_factory=list)
    hidden_hashes: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    included_files: list[str] = Field(default_factory=list)  # the first file selection (stable prompt prefix)
    # ---- progress
    best_score: list[float] = Field(default_factory=list)
    best_diff_key: str | None = None
    last_diff_key: str | None = None
    last_trace_key: str | None = None
    refused: list[str] = Field(default_factory=list)
    approved_requirements: list[str] = Field(default_factory=list)
    cached: str | None = None
    # ---- sql inputs fixed at intake
    seeds: list[SeedFile] = Field(default_factory=list)
    expected: dict[str, Any] | None = None
    baseline_feedback: str | None = None
