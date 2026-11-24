"""The SQL loop (design section 11): write or fix a query, prove it on a snapshot database.

    intake (seeds, expected rows) ─► prepare database (+ mutated copies) ─► baseline (broken query)
        ─► [L2 cache] ─► candidate query ─► static checks ─► read-only run ─► verdict ─► repair ...

The model only ever writes the query text. Everything that decides whether the query is right
happens without it: sqlglot checks (parse, known identifiers, deny lists, time functions), a
plan-cost guard, execution in a read-only role on a copy of the customer's snapshot, result
comparison with tolerances, properties and assertions, and differential runs on mutated copies
of the data that catch queries which are right only by accident (a wrong join that happens to
match, a missing DISTINCT, hard-coded values).
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Literal

import structlog

from fixloop_core.context import (
    DIALECT_NOTES,
    schema_block,
    sql_frame,
    sql_history_block,
    sql_requirements,
    untrusted,
)
from fixloop_core.errors import EnvFailed, FormatError, InfraError, InvalidInput, ProviderRefusal
from fixloop_core.hashing import sha256_bytes, sha256_text
from fixloop_core.loop import intake
from fixloop_core.loop.memo import sql_fix_key
from fixloop_core.loop.runtime import TaskRun
from fixloop_core.loop.stop import register_failure, should_escalate, text_signature
from fixloop_core.models import (
    AttemptKind,
    AttemptRecord,
    CommandSpec,
    ExecPurpose,
    ExecRequest,
    ExecStatus,
    Outcome,
    OverlayFile,
    Phase,
    PrepareSpec,
    SeedFile,
    SqlPrepare,
    TaskResult,
    Verdict,
    VerificationLevel,
)
from fixloop_core.outputs import FailureClass, SqlCandidate, SqlRepair
from fixloop_core.prompts import RenderedPrompt
from fixloop_core.scoring import better, sql_score
from fixloop_core.spec import ExpectedResult, SqlOracle, SqlSnapshotSource
from fixloop_core.verify_sql import SqlOutcome, schema_columns, sql_verdict, static_check

log = structlog.get_logger(__name__)
NO_ATTEMPT = -1
MUTATION_SEED = 7
STATEMENT_TIMEOUT_MS = 10_000
EXPLAIN_MAX_ROWS = 50_000_000


def clean_query(query: str) -> str:
    """Strip whitespace and trailing semicolons: the query is wrapped in a CTE for assertions."""
    return re.sub(r"[;\s]+$", "", query.strip())


def query_fingerprint(query: str) -> str:
    return sha256_text(re.sub(r"\s+", " ", query.strip().lower()))[:24]


class SqlLoop:
    def __init__(self, run: TaskRun) -> None:
        self.run = run
        self.spec = run.spec
        assert isinstance(run.spec.oracle, SqlOracle)
        assert isinstance(run.spec.source, SqlSnapshotSource)
        self.oracle: SqlOracle = run.spec.oracle
        self.source: SqlSnapshotSource = run.spec.source
        self.dialect: Literal["postgres", "duckdb"] = self.oracle.dialect
        self.sandbox = run.config.limits.sandbox

    @property
    def state(self):
        return self.run.state

    @property
    def max_repairs(self) -> int:
        return min(self.spec.options.max_repairs, self.run.limits.max_repairs)

    async def execute(self) -> TaskResult:
        if self.state.phase in (Phase.queued, Phase.snapshot):
            await self.intake()
        if self.state.phase is Phase.prepare_env:
            await self.prepare_env()
        if self.state.phase is Phase.baseline:
            finished = await self.baseline()
            if finished is not None:
                return finished
        return await self.attempts_loop()

    # ================================================================== intake and environment
    async def intake(self) -> None:
        run, state = self.run, self.state
        await run.enter(Phase.snapshot)
        limits = run.config.limits.intake
        seeds: list[SeedFile] = []
        for seed in self.source.seeds:
            if seed.inline_csv is not None:
                data = seed.inline_csv.encode("utf-8")
                key = run.key("seeds", f"{seed.table}.csv")
                await run.deps.blobs.put_bytes(key, data, "text/csv")
            else:
                assert seed.upload_id is not None
                upload = await run.deps.store.upload(seed.upload_id, run.tenant_id)
                if upload is None:
                    raise InvalidInput(f"seed upload {seed.upload_id} for table {seed.table} does not exist")
                data = await run.deps.blobs.get_bytes(upload.key, max_bytes=limits.sql_seed_max_bytes)
                key = upload.key
            seeds.append(SeedFile(table=seed.table, key=key, sha256=sha256_bytes(data), format=seed.format))
        state.seeds = seeds
        if self.oracle.expected is not None:
            state.expected = self.oracle.expected.model_dump(mode="json")
        elif self.oracle.expected_rows:
            upload = await run.deps.store.upload(self.oracle.expected_rows, run.tenant_id)
            if upload is None:
                raise InvalidInput(f"expected rows upload {self.oracle.expected_rows} does not exist")
            if upload.kind == "parquet":
                raise InvalidInput(
                    "expected rows must be uploaded as CSV (Parquet is supported for seed data only)"
                )
            data = await run.deps.blobs.get_bytes(upload.key, max_bytes=20 * 1024 * 1024)
            state.expected = intake.parse_expected_csv(data).model_dump(mode="json")
        if not self.oracle.has_any_oracle:
            state.warnings.append(
                "no oracle was given: the query can be checked for safety, but not proven right"
            )
        mutations = 3 if (self.oracle.differential and self.oracle.reference_query) else 0
        image = f"sql-{self.dialect}"
        env_hash = intake.sql_env_hash(
            self.dialect, self.source.ddl, [s.sha256 for s in seeds], mutations, MUTATION_SEED
        )
        state.prepare = PrepareSpec(
            family="sql",
            image=image,
            env_hash=env_hash,
            sql=SqlPrepare(
                dialect=self.dialect,
                ddl=self.source.ddl,
                seeds=seeds,
                mutations=mutations,
                mutation_seed=MUTATION_SEED,
                reference_query=self.oracle.reference_query,
            ),
        )
        await run.emit("snapshot.ready", tables=len(seeds), dialect=self.dialect, mutations=mutations)
        await run.checkpoint(Phase.prepare_env)

    async def prepare_env(self) -> None:
        run, state = self.run, self.state
        await run.enter(Phase.prepare_env)
        assert state.prepare is not None
        timeout = self.sandbox.prepare_timeout_s
        request = ExecRequest(
            exec_id=run.exec_id(NO_ATTEMPT, "prepare", state.prepare.env_hash),
            tenant_id=str(run.tenant_id),
            task_id=str(run.task_id),
            purpose=ExecPurpose.prepare,
            prepare=state.prepare,
            command=CommandSpec(template="sql-prepare"),
            limits=self.sandbox.limits(timeout),
            deadline_ts=run.deadline(timeout),
        )
        result = await run.execute_retrying(request)
        if result.env_error:
            raise EnvFailed("the snapshot database could not be built: " + result.env_error)
        if result.env is None:
            raise InfraError(f"the database was not prepared ({result.status.value})", retryable=False)
        state.env = result.env
        state.sql_schema = result.env.metadata.get("schema", {})
        await run.emit("env.ready", cache_hit=result.cache_hit, seconds=round(result.sandbox_seconds, 1))
        await run.checkpoint(Phase.baseline)

    # ================================================================== checking one query
    def run_spec(self, query: str) -> dict[str, Any]:
        state = self.state
        datasets = list((state.env.metadata if state.env else {}).get("datasets", ["base"]))
        if not (self.oracle.differential and self.oracle.reference_query):
            datasets = ["base"]
        expected_rows = len((state.expected or {}).get("rows", []))
        return {
            "dialect": self.dialect,
            "candidate_query": query,
            "reference_query": self.oracle.reference_query,
            "datasets": datasets,
            "max_rows": min(20_000, max(1_000, expected_rows + 100)),
            "statement_timeout_ms": STATEMENT_TIMEOUT_MS,
            "explain_max_rows": EXPLAIN_MAX_ROWS,
            "assertions": [p.sql for p in self.oracle.properties if p.kind == "assertion"],
        }  # type: ignore[union-attr]

    async def check_query(
        self, number: int, query: str, fingerprint: str
    ) -> tuple[Verdict, SqlOutcome | None, list[str], str]:
        """(verdict, full outcome, exec ids, feedback for the model)."""
        run = self.run
        static = static_check(
            query,
            self.dialect,
            schema_columns(self.state.sql_schema),
            run.config.policies.sql,
            self.oracle.as_of,
        )
        if not static.ok:
            verdict = Verdict(passed=False, failure_class=static.failure_class, message=static.message)
            return verdict, None, [], static.message
        spec = json.dumps(self.run_spec(query)).encode()
        assert self.state.prepare is not None
        request = ExecRequest(
            exec_id=run.exec_id(number, "sql", fingerprint),
            tenant_id=str(run.tenant_id),
            task_id=str(run.task_id),
            purpose=ExecPurpose.sql,
            prepare=self.state.prepare,
            extra_files=[
                OverlayFile(path=".fixloop/sql_run.json", content_b64=base64.b64encode(spec).decode())
            ],
            command=CommandSpec(template="sql-run"),
            limits=self.sandbox.limits(90),
            deadline_ts=run.deadline(90),
        )
        result = await run.execute_retrying(request, None if number < 0 else number)
        if result.status is ExecStatus.timeout:
            message = "running the query hit the sandbox time limit; simplify it or filter earlier"
            return (
                Verdict(passed=False, failure_class=FailureClass.sql_timeout, message=message),
                None,
                [request.exec_id],
                message,
            )
        text = await run.artifact_text(result, "sql_result.json")
        if not text:
            message = "the sandbox produced no result: " + (result.stderr_tail or result.error or "")[-600:]
            return (
                Verdict(passed=False, failure_class=FailureClass.sql_runtime_error, message=message),
                None,
                [request.exec_id],
                message,
            )
        expected = ExpectedResult.model_validate(self.state.expected) if self.state.expected else None
        try:
            outcome = sql_verdict(self.oracle, json.loads(text), query, expected)
        except ValueError as exc:  # the customer's reference query itself is broken
            raise InvalidInput(str(exc)) from exc
        return outcome.verdict, outcome, [request.exec_id], outcome.feedback

    # ================================================================== baseline
    async def baseline(self) -> TaskResult | None:
        run, state = self.run, self.state
        await run.enter(Phase.baseline)
        broken = self.oracle.broken_query
        if broken and self.oracle.has_any_oracle:
            query = clean_query(broken)
            verdict, outcome, _, feedback = await self.check_query(NO_ATTEMPT, query, "baseline")
            if verdict.passed and verdict.level is not VerificationLevel.executed:
                return TaskResult(
                    outcome=Outcome.nothing_to_fix,
                    reason="the current query already passes the oracle",
                    verification=verdict,
                    query=query,
                    preview=outcome.preview if outcome else None,
                    warnings=state.warnings,
                )
            state.baseline_feedback = feedback
            await run.emit(
                "baseline.done", failure_class=verdict.failure_class.value if verdict.failure_class else None
            )
        await run.checkpoint(Phase.context)
        return None

    # ================================================================== prompts
    def system(self) -> str:
        return self.run.prompts.shared(
            "sql",
            as_of=str(self.oracle.as_of or ""),
            dialect=self.dialect,
            dialect_notes=DIALECT_NOTES[self.dialect],
        )

    def stable(self) -> tuple[str, ...]:
        return (sql_frame(self.spec), schema_block(self.state.sql_schema, self.source.ddl))

    def prompt_candidate(self) -> RenderedPrompt:
        requirements, columns = sql_requirements(self.oracle)
        parts = list(self.stable())
        if self.state.baseline_feedback:
            parts.append(
                "<failure>\nWhy the current query is wrong:\n"
                + untrusted("feedback", self.state.baseline_feedback)
                + "\n</failure>"
            )
        parts.append(
            self.run.prompts.render(
                "sql_candidate", dialect=self.dialect, requirements=requirements, expected_columns=columns
            )
        )
        return RenderedPrompt("sql_candidate", self.system(), tuple(parts))

    def prompt_repair(self, number: int, escalated: bool) -> RenderedPrompt:
        history = sql_history_block([a for a in self.run.attempts if a.attempt_no < number])
        feedback = self.state.pending.get("last_feedback") or self.state.hidden_feedback or ""
        parts = [
            *self.stable(),
            history,
            "<failure>\n" + untrusted("feedback", feedback) + "\n</failure>",
            self.run.prompts.render("sql_repair", escalated=escalated),
        ]
        return RenderedPrompt("sql_repair", self.system(), tuple(p for p in parts if p))

    # ================================================================== attempts
    async def attempts_loop(self) -> TaskResult:
        state = self.state
        if state.next_attempt_no == 0 and not state.pending and self.run.l1_enabled and state.cached is None:
            cached = await self.try_cache()
            if cached is not None:
                return cached
            state.cached = "miss"
            await self.run.checkpoint()
        while state.next_attempt_no <= self.max_repairs:
            finished = await self.attempt(state.next_attempt_no)
            if finished is not None:
                return finished
        return await self.finish_failed(
            Outcome.exhausted_repairs, f"no verified query after {self.max_repairs + 1} attempt(s)"
        )

    def _fix_key(self) -> str:
        assert self.state.env is not None
        question = sha256_text(self.spec.description + "\x00" + (self.oracle.broken_query or ""))
        return sql_fix_key(
            str(self.run.tenant_id),
            self.spec.kind,
            self.state.env.env_hash,
            intake.oracle_hash(self.spec),
            question,
        )

    async def try_cache(self) -> TaskResult | None:
        key = self._fix_key()
        cached = await self.run.deps.cache.get("fix", key)
        if not cached or not isinstance(cached.get("query"), str):
            return None
        query = clean_query(cached["query"])
        verdict, outcome, _, _ = await self.check_query(NO_ATTEMPT, query, "l2-" + query_fingerprint(query))
        await self.run.emit("cache.checked", layer="l2", passed=verdict.passed)
        if not verdict.passed or verdict.level is VerificationLevel.executed:
            await self.run.deps.cache.delete(key)
            return None
        self.state.cached = "l2"
        return TaskResult(
            outcome=Outcome.verified,
            reason=verdict.message,
            verification=verdict,
            query=query,
            preview=outcome.preview if outcome else None,
            attempts=0,
            warnings=self.state.warnings,
            cache="l2",
        )

    async def attempt(self, number: int) -> TaskResult | None:
        run, state = self.run, self.state
        escalation = run.deps.escalation_enabled and self.spec.options.model_profile != "economy"
        if state.pending.get("attempt_no") != number:
            escalated = should_escalate(state, number, self.max_repairs, escalation)
            last_feedback = state.pending.get("next_feedback") if state.pending else None
            state.pending = {
                "attempt_no": number,
                "escalated": escalated,
                "cost_start": run.usage.cost_usd,
                "last_feedback": last_feedback,
            }
            await run.emit("attempt.started", attempt_no=number, escalated=escalated)
            await run.checkpoint()
        escalated = bool(state.pending.get("escalated"))
        kind = (
            AttemptKind.initial
            if number == 0
            else (AttemptKind.escalated_repair if escalated else AttemptKind.repair)
        )
        pending = state.pending
        if "query" not in pending:
            await run.enter(Phase.plan if number == 0 else Phase.repair)
            prompt = self.prompt_candidate() if number == 0 else self.prompt_repair(number, escalated)
            role, schema = ("sql_candidate", SqlCandidate) if number == 0 else ("sql_repair", SqlRepair)
            problem = ""
            for reask in range(run.limits.format_reasks_per_step + 1):
                current = (
                    prompt if reask == 0 else prompt.with_parts(f'<reask n="{reask}">\n{problem}\n</reask>')
                )
                try:
                    result = await run.llm(role, current, schema, attempt_no=number, escalated=escalated)
                except (FormatError, ProviderRefusal) as exc:
                    cls = (
                        FailureClass.format_error
                        if isinstance(exc, FormatError)
                        else FailureClass.provider_refusal
                    )
                    return await self.after_failure(
                        AttemptRecord(
                            attempt_no=number,
                            kind=kind,
                            verdict=Verdict(passed=False, failure_class=cls, message=str(exc)),
                            failure_class=cls,
                            failure_signature=text_signature(cls, str(exc)),
                        ),
                        str(exc),
                    )
                query = clean_query(result.value.query)
                if query_fingerprint(query) in state.fingerprints:
                    problem = (
                        "This exact query was already tried and it failed. Write a genuinely different query."
                    )
                    continue
                pending.update(
                    query=query,
                    model=result.model,
                    diagnosis=getattr(result.value, "diagnosis", None)
                    or getattr(result.value, "reasoning", None),
                )
                break
            else:
                cls = FailureClass.format_error
                return await self.after_failure(
                    AttemptRecord(
                        attempt_no=number,
                        kind=kind,
                        verdict=Verdict(passed=False, failure_class=cls, message=problem),
                        failure_class=cls,
                        failure_signature=text_signature(cls, "duplicate query"),
                    ),
                    problem,
                )
            await run.checkpoint(Phase.execute)

        query = pending["query"]
        fingerprint = query_fingerprint(query)
        state.fingerprints.append(fingerprint)
        await run.enter(Phase.verify)
        verdict, outcome, exec_ids, feedback = await self.check_query(number, query, fingerprint)
        score = sql_score(verdict, query)
        signature = text_signature(
            verdict.failure_class,
            outcome.signature_basis if outcome and outcome.signature_basis else verdict.message,
        )
        record = AttemptRecord(
            attempt_no=number,
            kind=kind,
            model=pending.get("model", ""),
            diagnosis=pending.get("diagnosis"),
            query=query,
            fingerprint=fingerprint,
            verdict=verdict,
            failure_class=verdict.failure_class,
            failure_signature="" if verdict.passed else signature,
            progress_score=score,
            exec_ids=exec_ids,
        )
        if better(score, state.best_score or None):
            state.best_score, state.best_attempt_no = score, number
            state.best_files = {"query": query}
        if verdict.passed:
            record.cost_usd = round(run.usage.cost_usd - float(pending.get("cost_start", 0.0)), 6)
            await run.record_attempt(record)
            await run.emit(
                "attempt.finished",
                attempt_no=number,
                passed=True,
                message=verdict.message,
                cost_usd=record.cost_usd,
            )
            outcome_kind = (
                Outcome.executed_only if verdict.level is VerificationLevel.executed else Outcome.verified
            )
            if outcome_kind is Outcome.verified and run.l1_enabled:
                await run.deps.cache.put("fix", self._fix_key(), {"query": query})
            return TaskResult(
                outcome=outcome_kind,
                reason=verdict.message,
                verification=verdict,
                query=query,
                preview=outcome.preview if outcome else None,
                final_attempt_no=number,
                best_attempt_no=number,
                attempts=len(run.attempts),
                warnings=list(dict.fromkeys(state.warnings + verdict.warnings)),
            )
        return await self.after_failure(record, feedback)

    async def after_failure(self, record: AttemptRecord, feedback: str) -> TaskResult | None:
        run, state = self.run, self.state
        if state.pending.get("escalated"):
            state.escalate_next = False
        decision = register_failure(state, record)
        record.cost_usd = round(run.usage.cost_usd - float(state.pending.get("cost_start", 0.0)), 6)
        await run.record_attempt(record)
        await run.emit(
            "attempt.finished",
            attempt_no=record.attempt_no,
            passed=False,
            failure_class=record.failure_class.value if record.failure_class else None,
            message=(record.verdict.message if record.verdict else "")[:300],
            cost_usd=record.cost_usd,
            repeated=record.signature_repeated,
        )
        state.next_attempt_no = record.attempt_no + 1
        state.pending = {"next_feedback": feedback[:6000]}
        await run.checkpoint(Phase.diagnose)
        if decision is not None:
            return await self.finish_failed(decision.outcome, decision.reason, record.failure_signature)
        if record.attempt_no >= self.max_repairs:
            return await self.finish_failed(
                Outcome.exhausted_repairs, f"no verified query after {record.attempt_no + 1} attempt(s)"
            )
        return None

    async def finish_failed(self, outcome: Outcome, reason: str, repeated: str | None = None) -> TaskResult:
        return sql_best_effort(self.run, outcome, reason, repeated=repeated)


def sql_best_effort(
    run: TaskRun, outcome: Outcome, reason: str, *, repeated: str | None = None, dimension: str | None = None
) -> TaskResult:
    state = run.state
    best = next((a for a in run.attempts if a.attempt_no == state.best_attempt_no), None)
    query = state.best_files.get("query") if state.best_files else None
    return TaskResult(
        outcome=outcome,
        reason=reason,
        verification=best.verdict if best else None,
        best_attempt_no=state.best_attempt_no,
        attempts=len(run.attempts),
        query=query,
        unverified=query is not None,
        warnings=list(dict.fromkeys(state.warnings)),
        repeated_signature=repeated,
        budget_dimension=dimension,
    )
