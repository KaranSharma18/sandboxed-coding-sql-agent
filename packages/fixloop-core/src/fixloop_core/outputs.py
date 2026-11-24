"""Schemas for everything a model may return.

Rules that apply to every model here:
- `extra="forbid"` and every field typed. No `dict[str, Any]`: provider schema converters can
  flatten untyped objects into strings, which then fails validation in confusing ways.
- Closed sets are enums; strings and lists have maximum lengths, so a runaway output fails fast
  instead of eating the token budget.
- `SCHEMA_VERSION` is part of every L1 cache key, so changing a schema never serves a stale answer.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from fixloop_core.spec import validate_repo_path

SCHEMA_VERSION = "2026-11.1"

RepoPath = Annotated[str, Field(min_length=1, max_length=512), AfterValidator(validate_repo_path)]


class FailureClass(StrEnum):
    # python execution
    syntax_error = "syntax_error"
    import_error = "import_error"
    collection_error = "collection_error"
    assertion_mismatch = "assertion_mismatch"
    exception_in_code = "exception_in_code"
    exception_in_test = "exception_in_test"
    timeout = "timeout"
    oom = "oom"
    recursion = "recursion"
    env_error = "env_error"
    # verification
    regression = "regression"
    tests_removed = "tests_removed"
    targets_failed = "targets_failed"
    test_gaming = "test_gaming"
    report_mismatch = "report_mismatch"
    hidden_failed = "hidden_failed"
    flaky_fix = "flaky_fix"
    # before execution
    patch_apply_error = "patch_apply_error"
    policy_violation = "policy_violation"
    patch_too_large = "patch_too_large"
    format_error = "format_error"
    provider_refusal = "provider_refusal"
    # sql
    sql_parse_error = "sql_parse_error"
    sql_unknown_identifier = "sql_unknown_identifier"
    sql_policy = "sql_policy"
    sql_runtime_error = "sql_runtime_error"
    sql_timeout = "sql_timeout"
    sql_too_expensive = "sql_too_expensive"
    sql_wrong_columns = "sql_wrong_columns"
    sql_wrong_rows = "sql_wrong_rows"
    sql_property_failed = "sql_property_failed"
    sql_differential_failed = "sql_differential_failed"
    hardcoded_result = "hardcoded_result"
    unknown = "unknown"


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DependencyRequest(_Out):
    package: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    version: str = Field(pattern=r"^[0-9][0-9A-Za-z.+!-]{0,40}$", description="exact version, pinned with ==")
    reason: str = Field(max_length=300)


class Plan(_Out):
    hypothesis: str = Field(max_length=1200)
    approach: str = Field(max_length=1200)
    files_to_change: list[RepoPath] = Field(default_factory=list, max_length=8)
    files_to_read: list[RepoPath] = Field(default_factory=list, max_length=8)
    tests_expected_to_pass: list[str] = Field(default_factory=list, max_length=50)
    risk: Literal["low", "medium", "high"] = "medium"
    dependency_requests: list[DependencyRequest] = Field(default_factory=list, max_length=3)


class Edit(_Out):
    path: RepoPath
    action: Literal["replace", "insert_before", "insert_after", "create", "delete"]
    search: str | None = Field(default=None, max_length=8000, description="exact text that occurs once")
    replace: str | None = Field(default=None, max_length=20000)
    reason: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def _fields_for_action(self) -> Edit:
        if self.action in ("replace", "insert_before", "insert_after"):
            if not self.search:
                raise ValueError(f"{self.action} needs a non-empty `search` anchor")
            if self.replace is None:
                raise ValueError(f"{self.action} needs `replace` text")
        elif self.action == "create" and self.replace is None:
            raise ValueError("create needs `replace` (the new file's content)")
        return self


class Patch(_Out):
    summary: str = Field(max_length=600)
    edits: list[Edit] = Field(min_length=1, max_length=20)


class Repair(_Out):
    diagnosis: str = Field(max_length=1500)
    what_is_different: str = Field(max_length=800)
    edits: list[Edit] = Field(min_length=1, max_length=20)
    dependency_requests: list[DependencyRequest] = Field(default_factory=list, max_length=3)


class KeyFailure(_Out):
    test_id: str = Field(max_length=400)
    error: str = Field(max_length=400)


class CodeLocation(_Out):
    path: str = Field(max_length=512)
    line: int = Field(ge=0)
    function: str = Field(default="", max_length=200)


class TraceDigest(_Out):
    summary: str = Field(max_length=1500)
    failure_class: FailureClass
    key_failures: list[KeyFailure] = Field(default_factory=list, max_length=5)
    suspected_locations: list[CodeLocation] = Field(default_factory=list, max_length=5)


class SqlCandidate(_Out):
    reasoning: str = Field(max_length=1500)
    query: str = Field(min_length=1, max_length=20000)
    output_columns: list[str] = Field(default_factory=list, max_length=50)
    assumptions: list[str] = Field(default_factory=list, max_length=5)


class SqlRepair(_Out):
    diagnosis: str = Field(max_length=1500)
    query: str = Field(min_length=1, max_length=20000)


ROLE_SCHEMAS: dict[str, type[BaseModel]] = {
    "plan": Plan,
    "patch": Patch,
    "repair": Repair,
    "repair_escalated": Repair,
    "digest": TraceDigest,
    "sql_candidate": SqlCandidate,
    "sql_repair": SqlRepair,
}
