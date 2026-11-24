"""Prompt templates: versioned Jinja2 files under `prompts/`, reviewed like code.

The template version is part of every L1 cache key and every call record, so editing a prompt
never serves a stale cached answer and can be compared in evaluations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import jinja2

from fixloop_core.hashing import stable_hash

PROMPT_VERSION = "v1"


@dataclass(frozen=True)
class RenderedPrompt:
    """A prompt ready to send. `parts` are ordered stable-first so providers can reuse the prefix."""

    role: str
    system: str
    parts: tuple[str, ...]
    prompt_version: str = PROMPT_VERSION
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def text(self) -> str:
        return self.system + "\n\n" + "\n\n".join(self.parts)

    def digest(self) -> str:
        return stable_hash("prompt", self.role, self.prompt_version, self.system, list(self.parts))

    def with_parts(self, *more: str) -> RenderedPrompt:
        return RenderedPrompt(self.role, self.system, (*self.parts, *more), self.prompt_version, self.extra)


class PromptLibrary:
    def __init__(self, root: Path, version: str = PROMPT_VERSION) -> None:
        self.root = Path(root)
        self.version = version
        self._env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(self.root)),
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False,  # noqa: S701 - plain-text prompts, never HTML
        )

    def render(self, name: str, **variables: Any) -> str:
        template = self._env.get_template(f"{name}/{self.version}.j2")
        return template.render(**variables).strip()

    def shared(self, family: str, **variables: Any) -> str:
        template = self._env.get_template(f"shared/{family}_{self.version}.j2")
        return template.render(**variables).strip()

    def check(self) -> None:
        """Fail fast at startup if a template is missing."""
        for name in ("plan", "patch", "repair", "digest", "sql_candidate", "sql_repair"):
            self._env.get_template(f"{name}/{self.version}.j2")
        for family in ("python", "sql"):
            self._env.get_template(f"shared/{family}_{self.version}.j2")


@lru_cache(maxsize=4)
def get_library(root: Path) -> PromptLibrary:
    library = PromptLibrary(root)
    library.check()
    return library
