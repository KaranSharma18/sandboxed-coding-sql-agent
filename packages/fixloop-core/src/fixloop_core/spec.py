"""The task spec: the immutable input contract of a task.

A spec is validated once at intake and frozen (its hash is stored with the task). Anything that
can be checked without the files is checked here, so a bad request is a 4xx and never a task.
Checks that need the files (does the target test exist?) happen after the snapshot is built.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

TaskKind = Literal["python.fix", "python.implement", "sql.write", "sql.fix"]
PYTHON_KINDS = ("python.fix", "python.implement")
SQL_KINDS = ("sql.write", "sql.fix")

# A pytest node id: a relative .py path, optional ::Class::name parts and an optional
# parametrize suffix. It may never start with "-" (that would turn it into a pytest flag) and
# may never contain "..", NUL or a newline.
NODE_ID_RE = re.compile(
    r"^(?!-)[A-Za-z0-9_][A-Za-z0-9_./-]*\.py(::[A-Za-z_][A-Za-z0-9_]*)*(\[[^\]\n\x00]{0,300}\])?$"
)
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def validate_repo_path(path: str) -> str:
    """Repository-relative POSIX path: no absolute paths, no '..', no backslashes, no NUL."""
    if not path or len(path) > 512:
        raise ValueError("path must be 1-512 characters")
    if "\x00" in path or "\\" in path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ValueError(f"path must be relative and use forward slashes: {path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"path may not contain empty, '.' or '..' segments: {path!r}")
    return path


def validate_node_id(node_id: str) -> str:
    if not NODE_ID_RE.match(node_id) or ".." in node_id.split("::")[0]:
        raise ValueError(f"not a valid pytest node id: {node_id!r}")
    return node_id


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


# ------------------------------------------------------------------------------------ sources
class InlineSource(_Strict):
    """Files sent in the request body. Meant for small tasks sent by other agents or the CLI."""

    type: Literal["inline"] = "inline"
    files: dict[str, str] = Field(min_length=1)

    @field_validator("files")
    @classmethod
    def _check_files(cls, files: dict[str, str]) -> dict[str, str]:
        for path in files:
            validate_repo_path(path)
        return files


class UploadSource(_Strict):
    """A tarball (.tar, .tar.gz or .tar.zst) uploaded through a presigned URL."""

    type: Literal["upload"] = "upload"
    upload_id: str = Field(min_length=3, max_length=80)


class GitHubSource(_Strict):
    """A repository reached through an installation of the Fixloop GitHub App.

    `ref` may be a branch; intake resolves it to a commit SHA and stores it in `resolved_sha`, so a
    moving branch can never change what is being fixed.
    """

    type: Literal["github"] = "github"
    installation_id: int = Field(gt=0)
    repo: str
    ref: str = Field(min_length=1, max_length=255)
    resolved_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, repo: str) -> str:
        if not _REPO_NAME_RE.match(repo):
            raise ValueError("repo must look like 'owner/name'")
        return repo


class SeedRef(_Strict):
    """Seed data for one table: an uploaded CSV/Parquet file, or small inline CSV text."""

    table: str
    format: Literal["csv", "parquet"] = "csv"
    upload_id: str | None = None
    inline_csv: str | None = Field(default=None, max_length=2_000_000)

    @field_validator("table")
    @classmethod
    def _check_table(cls, table: str) -> str:
        if not _IDENT_RE.match(table):
            raise ValueError(f"table name must be a simple identifier: {table!r}")
        return table

    @model_validator(mode="after")
    def _one_source(self) -> SeedRef:
        if (self.upload_id is None) == (self.inline_csv is None):
            raise ValueError("a seed needs exactly one of upload_id or inline_csv")
        if self.inline_csv is not None and self.format != "csv":
            raise ValueError("inline seeds must be CSV")
        return self


class SqlSnapshotSource(_Strict):
    """A database snapshot the customer provides: DDL plus seed data. Never a live database."""

    type: Literal["sql_snapshot"] = "sql_snapshot"
    ddl: str = Field(min_length=1, max_length=1_000_000)
    seeds: list[SeedRef] = Field(default_factory=list, max_length=200)


Source = Annotated[
    InlineSource | UploadSource | GitHubSource | SqlSnapshotSource, Field(discriminator="type")
]


# ------------------------------------------------------------------------------------ oracles
class PythonOracle(_Strict):
    type: Literal["pytest"] = "pytest"
    target_tests: list[str] = Field(min_length=1, max_length=50)
    regression: Literal["full", "related", "none"] = "full"
    hidden_tests: str | None = Field(default=None, description="upload id of a tarball of hidden test files")
    protected_paths: list[str] = Field(default_factory=list, max_length=50)
    python_version: Literal["3.10", "3.11", "3.12", "3.13"] = "3.12"
    dependency_file: str | None = None

    @field_validator("target_tests")
    @classmethod
    def _check_targets(cls, targets: list[str]) -> list[str]:
        for node_id in targets:
            validate_node_id(node_id)
        if len(set(targets)) != len(targets):
            raise ValueError("target_tests contains duplicates")
        return targets

    @field_validator("dependency_file")
    @classmethod
    def _check_dep_file(cls, value: str | None) -> str | None:
        return validate_repo_path(value) if value else value


class _Property(_Strict):
    @property
    def visible(self) -> bool:
        """Shape requirements are shown to the model; value checks stay hidden (they are answers)."""
        return False


class ColumnsProperty(_Property):
    kind: Literal["columns"] = "columns"
    names: list[str] = Field(min_length=1)
    types: list[str] | None = None

    @property
    def visible(self) -> bool:
        return True


class RowCountProperty(_Property):
    kind: Literal["row_count"] = "row_count"
    op: Literal["eq", "ge", "le", "between"] = "eq"
    value: int = Field(ge=0)
    value2: int | None = Field(default=None, ge=0)


class UniqueProperty(_Property):
    kind: Literal["unique"] = "unique"
    columns: list[str] = Field(min_length=1)

    @property
    def visible(self) -> bool:
        return True


class NotNullProperty(_Property):
    kind: Literal["not_null"] = "not_null"
    column: str

    @property
    def visible(self) -> bool:
        return True


class ValuesInProperty(_Property):
    kind: Literal["values_in"] = "values_in"
    column: str
    values: list[str | int | float | bool | None]


class SortedByProperty(_Property):
    kind: Literal["sorted_by"] = "sorted_by"
    columns: list[str] = Field(min_length=1)
    direction: Literal["asc", "desc"] = "asc"

    @property
    def visible(self) -> bool:
        return True


class SumEqualsProperty(_Property):
    kind: Literal["sum_equals"] = "sum_equals"
    column: str
    value: float
    tolerance: float = Field(default=1e-6, ge=0)


class AssertionProperty(_Property):
    """A customer-written query over the temporary table `candidate_result` that must return no rows."""

    kind: Literal["assertion"] = "assertion"
    sql: str = Field(min_length=1, max_length=20_000)


SqlProperty = Annotated[
    ColumnsProperty
    | RowCountProperty
    | UniqueProperty
    | NotNullProperty
    | ValuesInProperty
    | SortedByProperty
    | SumEqualsProperty
    | AssertionProperty,
    Field(discriminator="kind"),
]


class ExpectedResult(_Strict):
    columns: list[str] = Field(min_length=1, max_length=200)
    rows: list[list[Any]] = Field(max_length=10_000)

    @model_validator(mode="after")
    def _row_width(self) -> ExpectedResult:
        width = len(self.columns)
        for row in self.rows:
            if len(row) != width:
                raise ValueError("every expected row must have one value per column")
        return self


class SqlOracle(_Strict):
    type: Literal["sql"] = "sql"
    dialect: Literal["postgres", "duckdb"]
    expected: ExpectedResult | None = None
    expected_rows: str | None = Field(default=None, description="upload id of a CSV or Parquet file")
    reference_query: str | None = Field(default=None, max_length=20_000)
    properties: list[SqlProperty] = Field(default_factory=list, max_length=50)
    order_matters: bool = False
    sort_key: list[str] = Field(default_factory=list)
    float_tolerance: float = Field(default=1e-6, ge=0)
    relative_tolerance: float = Field(default=0.0, ge=0)
    allow_extra_columns: bool = False
    match_columns_by: Literal["name", "position"] = "name"
    trim_strings: bool = False
    casefold_strings: bool = False
    broken_query: str | None = Field(default=None, max_length=20_000)
    as_of: date | None = None
    differential: bool = True

    @model_validator(mode="after")
    def _one_expected(self) -> SqlOracle:
        if self.expected is not None and self.expected_rows is not None:
            raise ValueError("give expected rows inline or as an upload, not both")
        return self

    @property
    def has_row_oracle(self) -> bool:
        return self.expected is not None or self.expected_rows is not None or self.reference_query is not None

    @property
    def has_any_oracle(self) -> bool:
        return self.has_row_oracle or bool(self.properties)


Oracle = Annotated[PythonOracle | SqlOracle, Field(discriminator="type")]


# ------------------------------------------------------------------------------------ options
class TaskOptions(_Strict):
    max_repairs: int = Field(default=3, ge=0, le=3)
    model_profile: Literal["economy", "default", "quality"] = "default"
    open_pr: bool = False
    allow_dependency_changes: bool = False
    callback_url: HttpUrl | None = None
    priority: Literal["interactive", "batch"] = "interactive"
    cache: Literal["default", "no_llm_cache"] = "default"

    @field_validator("callback_url")
    @classmethod
    def _https_only(cls, url: HttpUrl | None) -> HttpUrl | None:
        if url is not None and url.scheme != "https":
            raise ValueError("callback_url must use https")
        return url


class BudgetOverride(_Strict):
    """Optional per-task limits. They may only lower the tenant's limits, never raise them."""

    max_model_calls: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1000)
    max_output_tokens: int | None = Field(default=None, ge=500)
    max_cost_usd: float | None = Field(default=None, gt=0)
    max_sandbox_seconds: int | None = Field(default=None, ge=10)
    wall_clock_seconds: int | None = Field(default=None, ge=30)


# ------------------------------------------------------------------------------------ the spec
class TaskSpec(_Strict):
    schema_version: Literal["2026-11"] = "2026-11"
    kind: TaskKind
    description: str = Field(default="", max_length=20_000)
    source: Source
    oracle: Oracle
    options: TaskOptions = Field(default_factory=TaskOptions)
    budget: BudgetOverride | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _check_metadata(cls, metadata: dict[str, str]) -> dict[str, str]:
        if len(metadata) > 20:
            raise ValueError("at most 20 metadata keys")
        for key, value in metadata.items():
            if len(key) > 64 or len(value) > 256:
                raise ValueError("metadata keys are at most 64 characters, values at most 256")
        return metadata

    @model_validator(mode="after")
    def _kind_matches(self) -> TaskSpec:
        if self.kind in PYTHON_KINDS:
            if not isinstance(self.oracle, PythonOracle):
                raise ValueError(f"{self.kind} needs a pytest oracle")
            if isinstance(self.source, SqlSnapshotSource):
                raise ValueError(f"{self.kind} needs a code source, not a SQL snapshot")
        else:
            if not isinstance(self.oracle, SqlOracle):
                raise ValueError(f"{self.kind} needs a sql oracle")
            if not isinstance(self.source, SqlSnapshotSource):
                raise ValueError(f"{self.kind} needs a sql_snapshot source")
            if self.kind == "sql.fix" and not self.oracle.broken_query:
                raise ValueError("sql.fix needs oracle.broken_query")
        return self

    @property
    def is_python(self) -> bool:
        return self.kind in PYTHON_KINDS

    @property
    def is_sql(self) -> bool:
        return self.kind in SQL_KINDS
