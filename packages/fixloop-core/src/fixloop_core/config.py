"""Typed loading of the versioned config files in `config/`.

Config files hold the things that change without a code change: model ids and prices, policy
rules, command templates and plan limits. They are loaded once, validated, and fail loudly at
start if something is inconsistent (for example a routed model without a price).
"""

from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from fixloop_core.models import BudgetLimits, Limits


class _C(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------------------ models.yaml
class PriceRow(_C):
    model: str
    effective: date
    input: float
    cached_input: float
    output: float

    def cost(self, prompt_tokens: int, cached_tokens: int, output_tokens: int, thinking_tokens: int) -> float:
        uncached = max(prompt_tokens - cached_tokens, 0)
        total = (
            uncached * self.input
            + cached_tokens * self.cached_input
            + (output_tokens + thinking_tokens) * self.output
        )
        return round(total / 1_000_000, 6)


class RoleConfig(_C):
    model: str
    fallback: str | None = None
    thinking: Literal["minimal", "low", "medium", "high"] = "medium"
    max_output_tokens: int
    timeout_s: int
    input_budget: int


class ProfileConfig(_C):
    escalation: bool = True
    max_repairs: int = 3
    cost_multiplier: float = 1.0
    role_overrides: dict[str, str] = Field(default_factory=dict)


class RetryConfig(_C):
    attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 20.0


class BreakerConfig(_C):
    consecutive_failures: int = 5
    failure_rate: float = 0.5
    window: int = 20
    open_seconds: int = 60


class RateLimit(_C):
    rpm: int
    tpm: int


class ModelsConfig(_C):
    prices: list[PriceRow]
    roles: dict[str, RoleConfig]
    profiles: dict[str, ProfileConfig]
    retries: RetryConfig = Field(default_factory=RetryConfig)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    rate_limits: dict[str, RateLimit] = Field(default_factory=dict)

    def price_for(self, model: str, on: date | None = None) -> PriceRow:
        on = on or date.today()
        rows = sorted(
            (p for p in self.prices if p.model == model and p.effective <= on), key=lambda p: p.effective
        )
        if not rows:
            raise KeyError(f"no price configured for model {model!r}")
        return rows[-1]

    def role(self, role: str, profile: str = "default") -> RoleConfig:
        base = self.roles[role]
        override = self.profiles.get(profile, ProfileConfig()).role_overrides.get(role)
        if override:
            return base.model_copy(update={"model": override, "fallback": base.model})
        return base

    def validate_prices(self) -> None:
        for name, role in self.roles.items():
            for model in filter(None, [role.model, role.fallback]):
                try:
                    self.price_for(model)
                except KeyError as exc:
                    raise ValueError(f"role {name!r}: {exc}") from exc
        for profile in self.profiles.values():
            for model in profile.role_overrides.values():
                self.price_for(model)


# ------------------------------------------------------------------------------------ policies.yaml
class PatchLimits(_C):
    max_files: int = 10
    max_changed_lines: int = 400
    max_changed_lines_implement: int = 1000
    max_deletes: int = 3
    max_edits: int = 20


class TestGamingRules(_C):
    __test__ = False

    forbidden_calls: list[str]
    forbidden_decorators: list[str]
    forbidden_imports_from: dict[str, list[str]]
    forbidden_strings: list[str]
    forbidden_modules: list[str]


class PatchPolicy(_C):
    protected_globs: list[str]
    protected_infra_globs: list[str]
    protected_pyproject_tables: list[str]
    protected_setup_cfg_sections: list[str]
    limits: PatchLimits
    test_gaming: TestGamingRules
    risky_modules: list[str]
    risky_calls: list[str]


class SqlPolicy(_C):
    max_query_chars: int = 20000
    max_rows: int = 10000
    statement_timeout_ms: int = 10000
    explain_max_rows_estimate: int = 50_000_000
    deny_functions: dict[str, list[str]]
    time_functions: list[str]

    def denied_for(self, dialect: str) -> set[str]:
        return {
            f.lower() for f in self.deny_functions.get("common", []) + self.deny_functions.get(dialect, [])
        }


class PoliciesConfig(_C):
    patch: PatchPolicy
    sql: SqlPolicy


# ------------------------------------------------------------------------------------ command_templates.yaml
class TemplateParam(_C):
    flag: str | None = None
    positional: bool = False
    type: Literal["int", "path_list", "nodeid", "nodeid_list", "str"]
    min: int | None = None
    max: int | None = None
    default: Any = None
    max_items: int | None = None


class CommandTemplate(_C):
    image_family: Literal["python", "sql"]
    network: Literal["none", "proxy"] = "none"
    argv: list[str]
    params: dict[str, TemplateParam] = Field(default_factory=dict)


class ImageEntry(_C):
    family: Literal["python", "sql"]
    ref: str


class TemplatesConfig(_C):
    templates: dict[str, CommandTemplate]
    images: dict[str, ImageEntry]


# ------------------------------------------------------------------------------------ limits.yaml
class TenantLimits(_C):
    running_tasks: int
    queued_tasks: int
    daily_cost_usd: float
    tasks_per_day: int
    api_requests_per_minute: int
    sandbox_seconds_per_day: int


class TaskPlanLimits(_C):
    max_repairs: int
    max_model_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_cost_usd_python: float
    max_cost_usd_sql: float
    max_sandbox_seconds: int
    max_executions: int
    wall_clock_seconds: int
    format_reasks_per_step: int

    def budget_for(self, is_sql: bool) -> BudgetLimits:
        return BudgetLimits(
            max_repairs=self.max_repairs,
            max_model_calls=self.max_model_calls,
            max_input_tokens=self.max_input_tokens,
            max_output_tokens=self.max_output_tokens,
            max_cost_usd=self.max_cost_usd_sql if is_sql else self.max_cost_usd_python,
            max_sandbox_seconds=self.max_sandbox_seconds,
            max_executions=self.max_executions,
            wall_clock_seconds=self.wall_clock_seconds,
            format_reasks_per_step=self.format_reasks_per_step,
        )


class PlanLimits(_C):
    task: TaskPlanLimits
    tenant: TenantLimits


class GlobalLimits(_C):
    daily_cost_usd: float
    alert_fraction: float = 0.8


class SandboxLimits(_C):
    timeout_s_default: int = 120
    timeout_s_max: int = 300
    prepare_timeout_s: int = 600
    memory_mb: int = 2048
    cpus: float = 2.0
    pids: int = 256
    nofile: int = 1024
    fsize_mb: int = 100
    tmpfs_mb: int = 256
    stdout_bytes: int = 1_048_576
    artifacts_bytes: int = 20_971_520
    regression_budget_s: int = 180
    per_test_timeout_s: int = 30

    def limits(self, timeout_s: int | None = None) -> Limits:
        return Limits(
            timeout_s=min(
                timeout_s or self.timeout_s_default, max(self.timeout_s_max, self.prepare_timeout_s)
            ),
            memory_mb=self.memory_mb,
            cpus=self.cpus,
            pids=self.pids,
            nofile=self.nofile,
            fsize_mb=self.fsize_mb,
            tmpfs_mb=self.tmpfs_mb,
            stdout_bytes=self.stdout_bytes,
            artifacts_bytes=self.artifacts_bytes,
        )


class IntakeLimits(_C):
    inline_max_bytes: int
    inline_max_files: int
    upload_max_bytes: int
    unpacked_max_bytes: int
    max_files: int
    description_max_chars: int
    sql_seed_max_bytes: int
    max_model_file_bytes: int


class LimitsConfig(_C):
    plans: dict[str, PlanLimits]
    global_: GlobalLimits = Field(alias="global")
    sandbox: SandboxLimits
    intake: IntakeLimits

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ------------------------------------------------------------------------------------ aggregate
_NAME_NORMALIZE = re.compile(r"[-_.]+")


def normalize_project_name(name: str) -> str:
    """PEP 503 normalization: lower case, runs of -_. become a single dash."""
    return _NAME_NORMALIZE.sub("-", name).lower().strip()


class AppConfig(_C):
    models: ModelsConfig
    policies: PoliciesConfig
    templates: TemplatesConfig
    limits: LimitsConfig
    allowlist: frozenset[str]
    pinned_images: dict[str, str]
    config_dir: Path

    def image_ref(self, logical: str, overrides: dict[str, str] | None = None) -> str:
        if overrides and logical in overrides:
            return overrides[logical]
        if logical in self.pinned_images:
            return self.pinned_images[logical]
        return self.templates.images[logical].ref


def _load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def read_allowlist(path: Path) -> frozenset[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(normalize_project_name(line))
    return frozenset(names)


def read_images_lock(path: Path) -> dict[str, str]:
    pinned: dict[str, str] = {}
    if not path.exists():
        return pinned
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            logical, ref = line.split(None, 1)
            pinned[logical] = ref.strip()
    return pinned


@lru_cache(maxsize=4)
def load_config(config_dir: Path) -> AppConfig:
    config_dir = Path(config_dir)
    models = ModelsConfig.model_validate(_load_yaml(config_dir / "models.yaml"))
    models.validate_prices()
    config = AppConfig(
        models=models,
        policies=PoliciesConfig.model_validate(_load_yaml(config_dir / "policies.yaml")),
        templates=TemplatesConfig.model_validate(_load_yaml(config_dir / "command_templates.yaml")),
        limits=LimitsConfig.model_validate(_load_yaml(config_dir / "limits.yaml")),
        allowlist=read_allowlist(config_dir / "allowlist.txt"),
        pinned_images=read_images_lock(config_dir / "images.lock"),
        config_dir=config_dir,
    )
    return config
