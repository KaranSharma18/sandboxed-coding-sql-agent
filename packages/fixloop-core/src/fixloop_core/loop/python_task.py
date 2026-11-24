"""The Python loop (design section 5): snapshot, environment, baseline, plan, patch, verify, repair.

    intake ─► prepare env ─► baseline (+ confirm) ─► [L2/L4 cache] ─► plan ─► patch ─► verify
                                                                        ▲                 │ fail
                                                                        └──── repair ◄────┘

Rules the code below implements, in the order they matter:

* Nothing is fixed before the baseline proves the target tests fail on the untouched code, and
  fail the same way twice (otherwise: nothing_to_fix or flaky_oracle).
* One first attempt plus at most `max_repairs` repairs. Re-asks for unusable output (schema,
  apply errors, policy violations, duplicates) do not count as attempts, but they do spend budget.
* Every repair starts from the *best* state so far, not the last one: a repair that made things
  worse is never built upon.
* The last repair always runs on the stronger model, and so does the repair after a failure
  signature first repeats. A second repeat ends the task (no_progress).
* "Verified" means: targets pass, nothing that passed before fails, no protected file changed,
  the two independent reports agree, hidden tests pass, and the targets pass again in a shuffled
  rerun. No model ever decides whether a candidate is correct.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

import structlog

from fixloop_core.context import (
    FILE_BLOCK_TOKENS,
    baseline_failure_block,
    current_state_block,
    estimate_tokens,
    failure_block,
    focus_lines,
    history_block,
    pack_files,
    rank_files,
    task_frame_python,
    tests_block,
    untrusted,
)
from fixloop_core.errors import EnvFailed, FlakyOracle, FormatError, InfraError, InvalidInput, ProviderRefusal
from fixloop_core.hashing import hash_json, sha256_bytes
from fixloop_core.loop import intake
from fixloop_core.loop.memo import fix_key, memo_key, overlay_to_edits
from fixloop_core.loop.runtime import TaskRun
from fixloop_core.loop.stop import register_failure, should_escalate, text_signature
from fixloop_core.models import (
    AttemptKind,
    AttemptRecord,
    CommandSpec,
    ExecPurpose,
    ExecRequest,
    ExecResult,
    ExecStatus,
    Outcome,
    OverlayFile,
    Phase,
    PrepareSpec,
    PythonPrepare,
    TaskResult,
    TraceSummary,
    Verdict,
    VerificationLevel,
)
from fixloop_core.observability import POLICY_VIOLATIONS
from fixloop_core.outputs import Edit, FailureClass, Patch, Plan, Repair, TraceDigest
from fixloop_core.patching import AppliedPatch, PatchApplyFailed, WorkingTree, apply_edits, unified_diff
from fixloop_core.policy import PolicyResult, check_patch
from fixloop_core.prompts import RenderedPrompt
from fixloop_core.repomap import build_repo_map, import_graph, importers_of
from fixloop_core.scoring import better, python_score
from fixloop_core.spec import PythonOracle
from fixloop_core.traces import (
    ParsedReport,
    build_summary,
    compact_trace,
    is_test_path,
    parse_junit_counts,
    parse_report,
)
from fixloop_core.verify_python import (
    PASSING,
    Baseline,
    changed_line_coverage,
    compute_verdict,
    covers,
    expand_targets,
    hidden_verdict,
    stable_across,
    targeted_selection,
    targets_outcome,
)

log = structlog.get_logger(__name__)
NO_BASELINE_ATTEMPT = -1  # exec ids for work that belongs to no attempt (prepare, baseline, cache checks)


@dataclass
class Context:
    """The deterministic prompt blocks of one task. Rebuilt identically after a resume."""

    system: str
    frame: str
    repo_map: str
    tests: str
    files: str
    included: list[str]
    baseline_failure: str
    more_files: str = ""

    def stable(self) -> tuple[str, ...]:
        parts = (self.frame, self.repo_map, self.tests, self.files)
        return (*parts, self.more_files) if self.more_files else parts


@dataclass
class Produced:
    ok: bool
    edits: list[Edit] = field(default_factory=list)
    model: str = ""
    summary: str = ""
    diagnosis: str | None = None
    failure_class: FailureClass | None = None
    message: str = ""
    policy: PolicyResult | None = None


def _overlay_files(overlay: dict[str, str | None]) -> list[OverlayFile]:
    files = []
    for path in sorted(overlay):
        text = overlay[path]
        if text is None:
            files.append(OverlayFile(path=path, delete=True))
        else:
            data = text.encode("utf-8")
            files.append(
                OverlayFile(path=path, sha256=sha256_bytes(data), content_b64=base64.b64encode(data).decode())
            )
    return files


def _reask(problem: str, number: int) -> str:
    return (
        f'<reask n="{number}">\nYour previous answer could not be used.\n{problem}\n'
        "Return a corrected JSON object that fixes this.\n</reask>"
    )


class PythonLoop:
    def __init__(self, run: TaskRun) -> None:
        self.run = run
        self.spec = run.spec
        assert isinstance(run.spec.oracle, PythonOracle)
        self.oracle: PythonOracle = run.spec.oracle
        self.config = run.config
        self.policy = run.config.policies.patch
        self.sandbox = run.config.limits.sandbox
        self.targets = list(self.oracle.target_tests)
        self.target_files = sorted({t.split("::", 1)[0] for t in self.targets})
        self._baseline_blob: dict[str, Any] | None = None
        self._best_overlay: dict[str, str | None] | None = None

    @property
    def state(self):
        return self.run.state

    @property
    def max_repairs(self) -> int:
        return min(self.spec.options.max_repairs, self.run.limits.max_repairs)

    # ================================================================== entry
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

    # ================================================================== intake
    async def intake(self) -> None:
        run, state = self.run, self.state
        await run.enter(Phase.snapshot)
        build = await intake.build_python_snapshot(run)
        intake.check_targets_exist(build, self.spec)
        state.snapshot = await intake.store_snapshot(run, build)
        run.remember_snapshot(build)
        state.manifest_key = run.key("manifest.json")
        await run.put_json(state.manifest_key, build.manifest.model_dump(mode="json"))
        lines, origin, warnings = intake.detect_requirements(build.files, self.oracle.dependency_file)
        intake.check_allowlist(lines, self.config.allowlist)
        state.requirements = "".join(f"{line}\n" for line in lines)
        state.protected_globs, state.protected_hashes = intake.protected_files(build, self.spec, self.config)
        if self.oracle.hidden_tests:
            state.hidden_tests, state.hidden_files, state.hidden_hashes = await intake.load_hidden_tests(
                run, self.oracle.hidden_tests
            )
        state.warnings += warnings + build.notes[:5]
        if build.manifest.secret_files:
            state.warnings.append(
                "files that look like secrets were never shown to a model: "
                + ", ".join(build.manifest.secret_files[:5])
            )
        image = f"python-{self.oracle.python_version}"
        state.prepare = PrepareSpec(
            family="python",
            image=image,
            env_hash=intake.python_env_hash(
                self.config, image, self.oracle.python_version, state.requirements
            ),
            python=PythonPrepare(python_version=self.oracle.python_version, requirements=state.requirements),
        )
        await run.emit(
            "snapshot.ready",
            files=len(build.files),
            sha256=build.sha256,
            dependencies_from=origin,
            dependencies=len(lines),
        )
        await run.checkpoint(Phase.prepare_env)

    async def prepare_env(self, prepare: PrepareSpec | None = None) -> None:
        run, state = self.run, self.state
        await run.enter(Phase.prepare_env)
        spec = prepare or state.prepare
        assert spec is not None
        timeout = self.sandbox.prepare_timeout_s
        request = ExecRequest(
            exec_id=run.exec_id(NO_BASELINE_ATTEMPT, "prepare", spec.env_hash),
            tenant_id=str(run.tenant_id),
            task_id=str(run.task_id),
            purpose=ExecPurpose.prepare,
            prepare=spec,
            command=CommandSpec(template="python-prepare"),
            limits=self.sandbox.limits(timeout),
            deadline_ts=run.deadline(timeout),
        )
        result = await run.execute_retrying(request)
        if result.env_error:
            raise EnvFailed(result.env_error)
        if result.env is None:
            raise InfraError(f"the environment was not prepared ({result.status.value})", retryable=False)
        state.env = result.env
        state.prepare = spec
        await run.emit("env.ready", cache_hit=result.cache_hit, seconds=round(result.sandbox_seconds, 1))
        await run.checkpoint(Phase.baseline)

    # ================================================================== executions
    def pytest_request(
        self,
        *,
        attempt_no: int,
        purpose: ExecPurpose,
        fingerprint: str,
        node_ids: list[str],
        overlay: dict[str, str | None] | None = None,
        changed: list[str] | None = None,
        deep_trace: str | None = None,
        shuffle_seed: int | None = None,
        rerun_no: int = 0,
        hidden: bool = False,
        timeout: int | None = None,
    ) -> ExecRequest:
        run, state = self.run, self.state
        args: dict[str, Any] = {"node_ids": node_ids, "test_timeout": self.sandbox.per_test_timeout_s}
        if changed:
            args["changed_files"] = changed
        if deep_trace:
            args["deep_trace"] = deep_trace
        if shuffle_seed is not None:
            args["shuffle_seed"] = shuffle_seed
        timeout = timeout or self.sandbox.timeout_s_default
        protected = dict(state.protected_hashes)
        if hidden:
            protected.update(state.hidden_hashes)
        assert state.prepare is not None
        return ExecRequest(
            exec_id=run.exec_id(attempt_no, purpose.value, fingerprint, rerun_no),
            tenant_id=str(run.tenant_id),
            task_id=str(run.task_id),
            purpose=purpose,
            prepare=state.prepare,
            snapshot=state.snapshot,
            overlay=_overlay_files(overlay or {}),
            hidden_tests=state.hidden_tests if hidden else None,
            command=CommandSpec(template="pytest-default", args=args),
            limits=self.sandbox.limits(timeout),
            protected_hashes=protected,
            deadline_ts=run.deadline(timeout),
        )

    async def report_of(self, result: ExecResult) -> tuple[ParsedReport, Any]:
        text = await self.run.artifact_text(result, "report.jsonl")
        junit = await self.run.artifact_text(result, "junit.xml")
        return parse_report(text or ""), parse_junit_counts(junit) if junit else None

    # ================================================================== baseline
    async def baseline(self) -> TaskResult | None:
        run, state = self.run, self.state
        await run.enter(Phase.baseline)
        full = self.oracle.regression != "none"
        timeout = self.sandbox.timeout_s_max if full else self.sandbox.timeout_s_default
        result = await run.execute_retrying(
            self.pytest_request(
                attempt_no=NO_BASELINE_ATTEMPT,
                purpose=ExecPurpose.baseline,
                fingerprint="all" if full else "targets",
                node_ids=[] if full else self.targets,
                timeout=timeout,
            )
        )
        strategy = self.oracle.regression
        if full and result.status is ExecStatus.timeout:
            state.warnings.append(
                f"the full test suite takes longer than {timeout}s; regressions are checked on "
                "a targeted selection instead"
            )
            result = await run.execute_retrying(
                self.pytest_request(
                    attempt_no=NO_BASELINE_ATTEMPT,
                    purpose=ExecPurpose.baseline,
                    fingerprint="targets",
                    node_ids=self.targets,
                )
            )
            strategy = "related"
        report, _ = await self.report_of(result)
        self._check_runnable(result, report)
        ok, failing = targets_outcome(report, self.targets)
        if ok:
            await run.emit("baseline.done", failing_targets=0, collected=len(report.collected))
            return TaskResult(
                outcome=Outcome.nothing_to_fix,
                reason="the target tests already pass on the untouched code, so there is nothing to fix",
                verification=Verdict(
                    passed=True,
                    level=VerificationLevel.executed,
                    message="target tests pass at baseline",
                    counts=report.counts(),
                ),
                warnings=state.warnings,
            )
        confirm = await run.execute_retrying(
            self.pytest_request(
                attempt_no=NO_BASELINE_ATTEMPT,
                purpose=ExecPurpose.baseline_confirm,
                fingerprint="confirm",
                node_ids=sorted(set(failing))[:500],
                shuffle_seed=1,
            )
        )
        confirm_report, _ = await self.report_of(confirm)
        flipped = sorted(n for n in failing if confirm_report.outcome(n) in PASSING)
        if flipped:
            raise FlakyOracle(
                f"{len(flipped)} target test(s) pass on some runs and fail on others without any "
                f"change, so a fix could not be verified: {', '.join(flipped[:5])}"
            )
        duration = result.duration_ms / 1000
        if strategy == "full" and duration > self.sandbox.regression_budget_s:
            strategy = "related"
            state.warnings.append(
                f"the full suite takes {duration:.0f}s, over the {self.sandbox.regression_budget_s}s "
                "regression budget: regressions are checked on a targeted selection"
            )
        state.regression_strategy = {"full": "full", "related": "targeted", "none": "none"}[strategy]
        state.baseline_status = {node: test.outcome for node, test in report.tests.items()}
        state.baseline_collected = list(report.collected)
        state.baseline_collection_errors = sorted(
            {str(e.get("nodeid", "")) for e in report.collection_errors}
        )
        state.baseline_duration_s = duration
        if not report.present:
            state.regression_strategy = "none"
            state.warnings.append(
                "the test suite could not start before the change, so regressions could not be measured"
            )
        summary = build_summary(result, report, focus=self.targets, only=failing)
        state.baseline_signature = summary.signature
        snapshot = await run.snapshot()
        trace = compact_trace(
            summary,
            read_file=snapshot.text,
            budget_tokens=3000,
            raw_stdout=result.stdout_tail,
            raw_stderr=result.stderr_tail,
        )
        durations = {node: test.duration for node, test in report.tests.items()}
        state.baseline_summary_key = run.key("baseline.json")
        await run.put_json(
            state.baseline_summary_key,
            {"summary": summary.model_dump(mode="json"), "trace": trace, "durations": durations},
        )
        await run.emit(
            "baseline.done",
            failing_targets=len(failing),
            collected=len(report.collected),
            strategy=state.regression_strategy,
            seconds=round(duration, 1),
        )
        await run.checkpoint(Phase.context)
        return None

    def _check_runnable(self, result: ExecResult, report: ParsedReport) -> None:
        output = (result.stdout_tail + "\n" + result.stderr_tail)[-3000:]
        if result.status is ExecStatus.timeout:
            raise InvalidInput("the target tests did not finish within the time limit on the untouched code")
        if not report.present:
            if result.exit_code in (4, 5) and ("not found" in output or "no tests ran" in output):
                raise InvalidInput("pytest could not find the target tests:\n" + output[-1500:])
            return  # crashed before reporting (a broken conftest, say): that can be the bug itself
        broken_files = {str(e.get("nodeid", "")).split("::", 1)[0] for e in report.collection_errors}
        for target, nodes in expand_targets(self.targets, report.collected).items():
            if not nodes and target.split("::", 1)[0] not in broken_files:
                raise InvalidInput(f"target test {target} was not collected by pytest (check the node id)")

    async def baseline_blob(self) -> dict[str, Any]:
        if self._baseline_blob is None:
            assert self.state.baseline_summary_key
            self._baseline_blob = dict(await self.run.get_json(self.state.baseline_summary_key))
        return self._baseline_blob

    def baseline_view(self, selection: list[str]) -> Baseline:
        state = self.state
        statuses, collected = dict(state.baseline_status), list(state.baseline_collected)
        if selection:

            def keep(node: str) -> bool:
                return any(covers(s, node) for s in selection)

            statuses = {n: o for n, o in statuses.items() if keep(n)}
            collected = [n for n in collected if keep(n)]
        return Baseline(
            statuses=statuses,
            collected=collected,
            strategy=state.regression_strategy or "none",
            selection=selection,
            share=state.regression_share,
            duration_s=state.baseline_duration_s,
            collection_errors=list(state.baseline_collection_errors),
        )

    async def selection_for(self, overlay: dict[str, str | None]) -> list[str]:
        """Which tests a candidate runs: everything, the targets only, or a targeted selection."""
        strategy = self.state.regression_strategy
        if strategy == "full":
            return []
        if strategy == "none":
            return list(self.targets)
        snapshot = await self.run.snapshot()
        texts = {p: snapshot.text(p) or "" for p in snapshot.files if p.endswith(".py")}
        graph = import_graph(texts, cache_key=snapshot.sha256)
        related = {p for p in importers_of(set(overlay), graph) if is_test_path(p)}
        durations = (await self.baseline_blob()).get("durations", {})
        chosen, share = targeted_selection(
            collected=self.state.baseline_collected,
            targets=self.targets,
            related_files=related,
            durations=durations,
            budget_s=self.sandbox.regression_budget_s,
            seed=str(self.run.task_id),
        )
        self.state.regression_share = share
        return sorted(set(self.targets) | set(chosen))

    # ================================================================== context
    async def tree(self, best: bool = True) -> WorkingTree:
        snapshot = await self.run.snapshot()
        eol = {entry.path: entry.eol for entry in snapshot.manifest.entries}
        return WorkingTree(snapshot.files, await self.best_overlay() if best else {}, eol)

    async def best_overlay(self) -> dict[str, str | None]:
        if self._best_overlay is None:
            key = self.state.best_overlay_key
            self._best_overlay = dict(await self.run.get_json(key)) if key else {}
        return dict(self._best_overlay)

    async def context(self) -> Context:
        run, state = self.run, self.state
        snapshot = await run.snapshot()
        manifest = {entry.path: entry for entry in snapshot.manifest.entries}
        blob = await self.baseline_blob()
        summary = TraceSummary.model_validate(blob["summary"])
        read = snapshot.text
        paths = sorted(snapshot.files)
        texts = {p: read(p) or "" for p in paths if p.endswith(".py")}
        graph = import_graph(texts, cache_key=snapshot.sha256)
        ranked = rank_files(
            paths=paths,
            manifest=manifest,
            read=read,
            target_tests=self.targets,
            summary=summary,
            description=self.spec.description,
            graph=graph,
            max_file_bytes=self.config.limits.intake.max_model_file_bytes,
            requested=(),
        )
        exclude = set(self.target_files) | set(snapshot.manifest.secret_files)
        files_text, included = pack_files(
            ranked, read, FILE_BLOCK_TOKENS, focus_lines(summary), manifest, exclude
        )
        if not state.included_files:
            state.included_files = included
        more = ""
        extra = [p for p in state.extra_files if p not in included and p in snapshot.files]
        if extra:
            more_text, _ = pack_files([(p, "requested") for p in extra], read, 8000, {}, manifest, exclude)
            more = "<more_files>\n" + more_text + "\n</more_files>"
        return Context(
            system=run.prompts.shared("python", limits=self.policy.limits),
            frame=task_frame_python(self.spec, state.protected_globs),
            repo_map=build_repo_map(paths, read),
            tests=tests_block(read, self.targets),
            files=files_text,
            included=included,
            baseline_failure=baseline_failure_block(summary, blob.get("trace", "")),
            more_files=more,
        )

    def prompt_plan(self, ctx: Context) -> RenderedPrompt:
        return RenderedPrompt(
            "plan", ctx.system, (*ctx.stable(), ctx.baseline_failure, self.run.prompts.render("plan"))
        )

    def prompt_patch(self, ctx: Context, plan: Plan) -> RenderedPrompt:
        return RenderedPrompt(
            "patch",
            ctx.system,
            (*ctx.stable(), ctx.baseline_failure, self.run.prompts.render("patch", plan=plan)),
        )

    async def prompt_repair(self, ctx: Context, attempt_no: int, escalated: bool) -> RenderedPrompt:
        run, state = self.run, self.state
        last_diff = (
            (await run.deps.blobs.get_bytes(state.last_diff_key)).decode() if state.last_diff_key else None
        )
        best_diff = (
            (await run.deps.blobs.get_bytes(state.best_diff_key)).decode() if state.best_diff_key else None
        )
        if state.last_trace_key:
            trace = (await run.deps.blobs.get_bytes(state.last_trace_key)).decode()
        else:  # no candidate has run yet (the first attempt produced no usable patch): show the baseline
            trace = (await self.baseline_blob()).get("trace", "")
        digest = await self.digest(ctx, attempt_no, trace)
        failure = failure_block(trace or "(no trace)", digest)
        if escalated and state.deep_trace_key:
            deep = (await run.deps.blobs.get_bytes(state.deep_trace_key)).decode()[:8000]
            failure += "\nDeep trace of the first failing target (local variables per frame):\n" + untrusted(
                "deep_trace", deep
            )
        history = history_block(
            [a for a in run.attempts if a.attempt_no < attempt_no], last_diff, best_diff, state.refused
        )
        instruction = run.prompts.render("repair", escalated=escalated, hidden_feedback=state.hidden_feedback)
        parts = (*ctx.stable(), history, current_state_block(await self.best_overlay()), failure, instruction)
        return RenderedPrompt("repair", ctx.system, tuple(p for p in parts if p))

    async def digest(self, ctx: Context, attempt_no: int, trace: str) -> str | None:
        """Summarize a long failure with the cheap model; short traces go to the repair model as they are."""
        pending = self.state.pending
        if "digest" in pending:
            return pending["digest"]
        if estimate_tokens(trace) < 1800:
            return None
        prompt = RenderedPrompt(
            "digest", ctx.system, (ctx.frame, failure_block(trace), self.run.prompts.render("digest"))
        )
        try:
            result = await self.run.llm("digest", prompt, TraceDigest, attempt_no=attempt_no)
        except (FormatError, ProviderRefusal):
            return None
        locations = ", ".join(f"{loc.path}:{loc.line}" for loc in result.value.suspected_locations[:5])
        text = result.value.summary + (f" Suspected locations: {locations}." if locations else "")
        pending["digest"] = text
        await self.run.checkpoint()
        return text

    # ================================================================== caches (L2 / L4)
    def _cache_keys(self) -> tuple[str, str]:
        state = self.state
        assert state.snapshot
        assert state.env
        oracle = intake.oracle_hash(self.spec)
        tenant = str(self.run.tenant_id)
        return (
            fix_key(tenant, self.spec.kind, state.snapshot.sha256, oracle, state.env.env_hash),
            memo_key(tenant, self.spec.kind, state.baseline_signature, oracle, state.env.env_hash),
        )

    async def try_caches(self) -> TaskResult | None:
        run = self.run
        fix, memo = self._cache_keys()
        cached = await run.deps.cache.get("fix", fix)
        if cached and isinstance(cached.get("overlay"), dict):
            outcome = await self._verify_cached("l2", dict(cached["overlay"]), cached.get("summary", ""))
            if outcome is not None:
                return outcome
            await run.deps.cache.delete(fix)
        remembered = await run.deps.cache.get("memo", memo)
        if remembered and isinstance(remembered.get("edits"), list):
            try:
                edits = [Edit.model_validate(e) for e in remembered["edits"]]
                applied = apply_edits(await self.tree(best=False), edits)
            except (PatchApplyFailed, ValueError):
                return None  # the precondition does not hold for this snapshot
            outcome = await self._verify_cached("l4", applied.files_after, remembered.get("summary", ""))
            if outcome is not None:
                return outcome
            await run.deps.cache.delete(memo)
        return None

    async def _verify_cached(
        self, layer: str, overlay: dict[str, str | None], summary: str
    ) -> TaskResult | None:
        tree = await self.tree(best=False)
        changes = {p: t for p, t in overlay.items() if tree.read_base(p) != t}
        if not changes:
            return None
        fingerprint = hash_json(sorted((p, t or "") for p, t in changes.items()))
        verdict, _, _, _ = await self.verify_overlay(
            NO_BASELINE_ATTEMPT, f"{layer}-{fingerprint[:16]}", changes
        )
        await self.run.emit("cache.checked", layer=layer, passed=verdict.passed)
        if not verdict.passed:
            return None
        self.state.cached = layer
        return await self.finish_verified(
            -1, changes, verdict, summary or f"cached fix ({layer}), re-verified"
        )

    async def remember(self, overlay: dict[str, str | None], summary: str) -> None:
        if not self.run.l1_enabled:
            return
        fix, memo = self._cache_keys()
        try:
            await self.run.deps.cache.put("fix", fix, {"overlay": overlay, "summary": summary})
            tree = await self.tree(best=False)
            await self.run.deps.cache.put(
                "memo", memo, {"edits": overlay_to_edits(tree.read_base, overlay), "summary": summary}
            )
        except (ValueError, OSError) as exc:
            log.info("cache_store_skipped", reason=str(exc)[:200])

    # ================================================================== attempts
    async def attempts_loop(self) -> TaskResult:
        state = self.state
        ctx = await self.context()
        if state.next_attempt_no == 0 and not state.pending and self.run.l1_enabled and state.cached is None:
            cached = await self.try_caches()
            if cached is not None:
                return cached
            state.cached = "miss"
            await self.run.checkpoint()
        while state.next_attempt_no <= self.max_repairs:
            finished = await self.attempt(state.next_attempt_no, ctx)
            if finished is not None:
                return finished
            if state.extra_files and not ctx.more_files:
                ctx = await self.context()
        return await self.finish_failed(
            Outcome.exhausted_repairs, f"no verified fix after {self.max_repairs + 1} attempt(s)"
        )

    async def attempt(self, number: int, ctx: Context) -> TaskResult | None:
        run, state = self.run, self.state
        escalation = run.deps.escalation_enabled and self.spec.options.model_profile != "economy"
        escalated = should_escalate(state, number, self.max_repairs, escalation)
        kind = (
            AttemptKind.initial
            if number == 0
            else (AttemptKind.escalated_repair if escalated else AttemptKind.repair)
        )
        if state.pending.get("attempt_no") != number:
            state.pending = {"attempt_no": number, "escalated": escalated, "cost_start": run.usage.cost_usd}
            await run.emit("attempt.started", attempt_no=number, kind=kind.value, escalated=escalated)
            if escalated:
                await self.deep_trace(number)
            await run.checkpoint()
        escalated = bool(state.pending.get("escalated"))
        kind = (
            AttemptKind.initial
            if number == 0
            else (AttemptKind.escalated_repair if escalated else AttemptKind.repair)
        )
        if "edits" not in state.pending:
            produced = await self.produce(number, escalated, ctx)
            if not produced.ok:
                record = AttemptRecord(
                    attempt_no=number,
                    kind=kind,
                    model=produced.model,
                    plan=state.pending.get("plan"),
                    diagnosis=produced.diagnosis,
                    edits=[e.model_dump(exclude_none=True) for e in produced.edits],
                    verdict=Verdict(
                        passed=False, failure_class=produced.failure_class, message=produced.message
                    ),
                    failure_class=produced.failure_class,
                    failure_signature=text_signature(produced.failure_class, produced.message),
                )
                return await self.after_failure(record)
            policy = produced.policy
            state.pending.update(
                edits=[e.model_dump(exclude_none=True) for e in produced.edits],
                model=produced.model,
                summary=produced.summary,
                diagnosis=produced.diagnosis,
                warnings=policy.warnings if policy else [],
                approved=[f"{d.package}=={d.version}" for d in policy.approved_dependencies]
                if policy
                else [],
                refused=[f"{d.package}=={d.version} ({why})" for d, why in policy.refused_dependencies]
                if policy
                else [],
            )
            await run.checkpoint(Phase.apply)
        return await self.run_candidate(number, kind)

    async def produce(self, number: int, escalated: bool, ctx: Context) -> Produced:
        run, state = self.run, self.state
        pending = state.pending
        if number == 0:
            if "plan" not in pending:
                await run.enter(Phase.plan)
                try:
                    plan_result = await run.llm("plan", self.prompt_plan(ctx), Plan, attempt_no=0)
                except (FormatError, ProviderRefusal) as exc:
                    return Produced(
                        False,
                        failure_class=FailureClass.format_error
                        if isinstance(exc, FormatError)
                        else FailureClass.provider_refusal,
                        message=str(exc),
                    )
                pending["plan"] = plan_result.value.model_dump(mode="json")
                pending["plan_model"] = plan_result.model
                state.plan = pending["plan"]
                snapshot = await run.snapshot()
                wanted = list(plan_result.value.files_to_read) + list(plan_result.value.files_to_change)
                state.extra_files += [
                    p
                    for p in wanted
                    if p in snapshot.files
                    and p not in ctx.included
                    and p not in state.extra_files
                    and p not in snapshot.manifest.secret_files
                ][:8]
                await run.checkpoint()
                if state.extra_files and not ctx.more_files:
                    ctx = await self.context()
            plan = Plan.model_validate(pending["plan"])
            await run.enter(Phase.patch)
            prompt, role, schema = self.prompt_patch(ctx, plan), "patch", Patch
            dependency_requests = list(plan.dependency_requests)
        else:
            await run.enter(Phase.repair)
            prompt, role, schema = await self.prompt_repair(ctx, number, escalated), "repair", Repair
            dependency_requests = []

        tree = await self.tree(best=True)
        problem, failure_class, model, last_edits = "", FailureClass.patch_apply_error, "", []
        for reask in range(self.run.limits.format_reasks_per_step + 1):
            current = prompt if reask == 0 else prompt.with_parts(_reask(problem, reask))
            try:
                result = await run.llm(role, current, schema, attempt_no=number, escalated=escalated)
            except FormatError as exc:
                return Produced(False, model=model, failure_class=FailureClass.format_error, message=str(exc))
            except ProviderRefusal as exc:
                return Produced(
                    False, model=model, failure_class=FailureClass.provider_refusal, message=str(exc)
                )
            value, model = result.value, result.model
            edits = list(value.edits)
            last_edits = edits
            requests = dependency_requests if number == 0 else list(getattr(value, "dependency_requests", []))
            try:
                applied = apply_edits(tree, edits)
            except PatchApplyFailed as exc:
                problem = "The edits could not be applied:\n" + "\n".join(e.render() for e in exc.errors[:10])
                failure_class = FailureClass.patch_apply_error
                continue
            if not applied.files_after:
                problem, failure_class = "The edits do not change any file.", FailureClass.patch_apply_error
                continue
            if applied.fingerprint in state.fingerprints:
                problem = (
                    "This exact change was already tried and it failed. Make a genuinely different change."
                )
                failure_class = FailureClass.patch_apply_error
                continue
            policy = check_patch(
                tree=tree,
                edits=edits,
                applied=applied,
                policy=self.policy,
                protected=state.protected_globs,
                task_kind=self.spec.kind,
                allow_dependency_changes=self.spec.options.allow_dependency_changes,
                dependency_requests=requests,
                allowlist=self.config.allowlist,
                target_test_files=self.target_files,
            )
            if not policy.ok:
                for violation in policy.violations:
                    POLICY_VIOLATIONS.labels(violation.code).inc()
                problem, failure_class = policy.feedback(), FailureClass.policy_violation
                if any(v.code == "patch_too_large" for v in policy.violations):
                    failure_class = FailureClass.patch_too_large
                continue
            return Produced(
                True,
                edits=edits,
                model=model,
                summary=getattr(value, "summary", ""),
                diagnosis=getattr(value, "diagnosis", None),
                policy=policy,
            )
        return Produced(
            False, edits=last_edits, model=model, failure_class=failure_class, message=problem, diagnosis=None
        )

    async def deep_trace(self, number: int) -> None:
        """Before an escalated repair: re-run the first failing target with local variables recorded."""
        last = next((a for a in reversed(self.run.attempts) if a.verdict and a.verdict.targets_failed), None)
        if last is None or not last.verdict:
            return
        node = last.verdict.targets_failed[0]
        overlay = await self.best_overlay()
        result = await self.run.execute(
            self.pytest_request(
                attempt_no=number,
                purpose=ExecPurpose.deep_trace,
                fingerprint=hash_json(sorted(overlay.items()))[:16],
                node_ids=[node],
                overlay=overlay,
                deep_trace=node,
            ),
            number,
        )
        text = await self.run.artifact_text(result, "deeptrace.json")
        if text:
            key = self.run.key("attempts", f"{number}-deeptrace.json")
            await self.run.deps.blobs.put_bytes(key, text.encode(), "application/json")
            self.state.deep_trace_key = key

    async def run_candidate(self, number: int, kind: AttemptKind) -> TaskResult | None:
        run, state = self.run, self.state
        pending = state.pending
        edits = [Edit.model_validate(e) for e in pending["edits"]]
        tree = await self.tree(best=True)
        applied: AppliedPatch = apply_edits(tree, edits)
        overlay = tree.overlay
        overlay.update(applied.files_after)
        approved = list(pending.get("approved") or [])
        if approved:
            await self.add_dependencies(approved)
        state.refused = (state.refused + list(pending.get("refused") or []))[-10:]
        state.warnings += [w for w in pending.get("warnings", []) if w not in state.warnings][:5]
        if applied.fuzzy_edits:
            state.warnings.append(
                f"attempt {number}: {len(applied.fuzzy_edits)} edit(s) matched with tolerant whitespace"
            )
        state.fingerprints.append(applied.fingerprint)
        verdict, report, exec_ids, signature = await self.verify_overlay(number, applied.fingerprint, overlay)
        diff = unified_diff(tree, overlay)
        diff_key = run.key("attempts", f"{number}.diff")
        await run.deps.blobs.put_bytes(diff_key, diff.encode(), "text/x-diff")
        state.last_diff_key = diff_key
        score = python_score(verdict, report, self.targets, applied.changed_lines)
        record = AttemptRecord(
            attempt_no=number,
            kind=kind,
            model=pending.get("model", ""),
            plan=pending.get("plan") if number == 0 else None,
            diagnosis=pending.get("diagnosis"),
            edits=pending["edits"],
            fingerprint=applied.fingerprint,
            files_after={
                p: sha256_bytes(t.encode()) if t is not None else "deleted" for p, t in overlay.items()
            },
            verdict=verdict,
            failure_class=verdict.failure_class,
            failure_signature=signature,
            progress_score=score,
            exec_ids=exec_ids,
            diff_key=diff_key,
        )
        if better(score, state.best_score or None):
            state.best_score, state.best_attempt_no = score, number
            state.best_files = dict(record.files_after)
            state.best_overlay_key = run.key("best_overlay.json")
            await run.put_json(state.best_overlay_key, overlay)
            self._best_overlay = dict(overlay)
            state.best_diff_key = diff_key
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
            summary = pending.get("summary") or pending.get("diagnosis") or ""
            return await self.finish_verified(number, overlay, verdict, summary)
        return await self.after_failure(record)

    async def add_dependencies(self, approved: list[str]) -> None:
        state = self.state
        new = [line for line in approved if line not in state.approved_requirements]
        if not new:
            return
        state.approved_requirements += new
        requirements = state.requirements + "".join(f"{line}\n" for line in new)
        assert state.prepare is not None
        image = state.prepare.image
        prepare = PrepareSpec(
            family="python",
            image=image,
            env_hash=intake.python_env_hash(self.config, image, self.oracle.python_version, requirements),
            python=PythonPrepare(python_version=self.oracle.python_version, requirements=requirements),
        )
        await self.prepare_env(prepare)
        state.requirements = requirements
        state.warnings.append("added dependencies: " + ", ".join(new))
        await self.run.checkpoint(Phase.execute)

    async def verify_overlay(
        self, number: int, fingerprint: str, overlay: dict[str, str | None]
    ) -> tuple[Verdict, ParsedReport, list[str], str]:
        """Run a candidate through every check that applies: (verdict, main report, exec ids, signature)."""
        run, state = self.run, self.state
        await run.enter(Phase.execute)
        selection = await self.selection_for(overlay)
        changed = [
            p for p, t in overlay.items() if t is not None and p.endswith(".py") and not is_test_path(p)
        ]
        request = self.pytest_request(
            attempt_no=number,
            purpose=ExecPurpose.candidate,
            fingerprint=fingerprint,
            node_ids=selection,
            overlay=overlay,
            changed=changed[:50],
            timeout=self.sandbox.timeout_s_max if not selection else None,
        )
        result = await run.execute_retrying(request, number)
        exec_ids = [request.exec_id]
        signature = ""
        await run.enter(Phase.verify)
        report, junit = await self.report_of(result)
        verdict = compute_verdict(
            targets=self.targets,
            baseline=self.baseline_view(selection),
            report=report,
            result=result,
            junit=junit,
            regression=state.regression_strategy or "none",
        )
        if verdict.passed and state.hidden_tests is not None:
            hidden = self.pytest_request(
                attempt_no=number,
                purpose=ExecPurpose.hidden,
                fingerprint=fingerprint,
                node_ids=list(state.hidden_files),
                overlay=overlay,
                hidden=True,
            )
            hidden_result = await run.execute_retrying(hidden, number)
            exec_ids.append(hidden.exec_id)
            hidden_report, _ = await self.report_of(hidden_result)
            ok, failing_count, message = hidden_verdict(hidden_report, hidden_result)
            if ok:
                verdict.level = VerificationLevel.hidden_passed
                state.hidden_feedback = None
            else:
                state.hidden_feedback = (
                    f"{failing_count} hidden test(s) that check the same behaviour more broadly "
                    "still fail. Make the fix general rather than tailored to the visible tests."
                )
                verdict = Verdict(
                    passed=False,
                    failure_class=FailureClass.hidden_failed,
                    message=message,
                    hidden_failed_count=failing_count,
                    counts=verdict.counts,
                )
                signature = text_signature(FailureClass.hidden_failed, str(failing_count))
        if verdict.passed:
            rerun = self.pytest_request(
                attempt_no=number,
                purpose=ExecPurpose.rerun,
                fingerprint=fingerprint,
                node_ids=list(self.targets),
                overlay=overlay,
                shuffle_seed=number + 11,
                rerun_no=1,
            )
            rerun_result = await run.execute_retrying(rerun, number)
            exec_ids.append(rerun.exec_id)
            rerun_report, _ = await self.report_of(rerun_result)
            stable, why = stable_across([(rerun_report, rerun_result)], self.targets)
            if stable:
                verdict.stable = True
            else:
                verdict = Verdict(
                    passed=False,
                    failure_class=FailureClass.flaky_fix,
                    message=f"the fix does not pass reliably: {why}",
                    counts=verdict.counts,
                )
                signature = text_signature(FailureClass.flaky_fix, "")
        if verdict.passed:
            tree = await self.tree(best=False)
            changes = {p: (tree.read_base(p) or "", t) for p, t in overlay.items() if t is not None}
            coverage = changed_line_coverage(changes, report, self.targets)
            verdict.coverage = coverage
            if coverage.get("share") == 0:
                verdict.warnings.append(
                    "none of the changed lines run during the target tests; the fix may work "
                    "for a different reason than intended"
                )
        elif not signature:
            failing = list(verdict.targets_failed) + list(verdict.regressions)
            summary = build_summary(result, report, focus=self.targets, only=failing or None)
            signature = summary.signature or text_signature(verdict.failure_class, verdict.message)
            snapshot = await run.snapshot()
            reader = WorkingTree(snapshot.files, overlay).read
            trace = compact_trace(
                summary,
                read_file=reader,
                budget_tokens=3000,
                raw_stdout=result.stdout_tail,
                raw_stderr=result.stderr_tail,
            )
            header = f"Verdict: {verdict.message}\n"
            if verdict.regressions:
                header += (
                    "Tests that passed before the change and fail now: "
                    + ", ".join(verdict.regressions[:10])
                    + "\n"
                )
            key = run.key("attempts", f"{number}-trace.txt")
            await run.deps.blobs.put_bytes(key, (header + trace).encode(), "text/plain")
            state.last_trace_key = key
        else:
            key = run.key("attempts", f"{number}-trace.txt")
            await run.deps.blobs.put_bytes(key, f"Verdict: {verdict.message}\n".encode(), "text/plain")
            state.last_trace_key = key
        return verdict, report, exec_ids, signature

    async def after_failure(self, record: AttemptRecord) -> TaskResult | None:
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
            message=record.verdict.message[:300] if record.verdict else "",
            cost_usd=record.cost_usd,
            repeated=record.signature_repeated,
        )
        state.next_attempt_no = record.attempt_no + 1
        state.pending = {}
        await run.checkpoint(Phase.diagnose)
        if decision is not None:
            return await self.finish_failed(
                decision.outcome, decision.reason, repeated=record.failure_signature
            )
        if record.attempt_no >= self.max_repairs:
            return await self.finish_failed(
                Outcome.exhausted_repairs, f"no verified fix after {record.attempt_no + 1} attempt(s)"
            )
        return None

    # ================================================================== results
    async def finish_verified(
        self, number: int, overlay: dict[str, str | None], verdict: Verdict, summary: str
    ) -> TaskResult:
        run, state = self.run, self.state
        tree = await self.tree(best=False)
        diff = unified_diff(tree, overlay)
        key = run.key("patch.diff")
        ref = await run.deps.blobs.put_bytes(key, diff.encode(), "text/x-diff")
        await run.deps.store.record_artifact(
            tenant_id=run.tenant_id,
            task_id=run.task_id,
            kind="patch",
            key=key,
            sha256=ref.sha256,
            size=ref.size,
            content_type="text/x-diff",
        )
        await run.put_json(run.key("final_overlay.json"), overlay)  # exact file contents, for the publisher
        await self.remember(overlay, summary)
        warnings = list(dict.fromkeys(state.warnings + verdict.warnings))
        return TaskResult(
            outcome=Outcome.verified,
            reason=verdict.message,
            verification=verdict,
            final_attempt_no=number if number >= 0 else None,
            best_attempt_no=number if number >= 0 else None,
            attempts=len(run.attempts),
            patch_key=key,
            patch_summary=summary[:600],
            warnings=warnings,
            cache=state.cached if state.cached not in (None, "miss") else None,
        )

    async def finish_failed(self, outcome: Outcome, reason: str, repeated: str | None = None) -> TaskResult:
        return await best_effort_result(self.run, outcome, reason, repeated=repeated)


async def best_effort_result(
    run: TaskRun, outcome: Outcome, reason: str, *, repeated: str | None = None, dimension: str | None = None
) -> TaskResult:
    """A failed task still returns its best attempt's diff, clearly marked as not verified."""
    state = run.state
    best = next((a for a in run.attempts if a.attempt_no == state.best_attempt_no), None)
    patch_key = None
    if state.best_diff_key:
        data = await run.deps.blobs.get_bytes(state.best_diff_key)
        if data.strip():
            patch_key = run.key("best-unverified.diff")
            await run.deps.blobs.put_bytes(patch_key, data, "text/x-diff")
    return TaskResult(
        outcome=outcome,
        reason=reason,
        verification=best.verdict if best else None,
        best_attempt_no=state.best_attempt_no,
        attempts=len(run.attempts),
        patch_key=patch_key,
        unverified=patch_key is not None,
        warnings=list(dict.fromkeys(state.warnings)),
        repeated_signature=repeated,
        budget_dimension=dimension,
    )


def describe(result: TaskResult) -> str:
    return json.dumps(result.model_dump(mode="json"))[:2000]
