"""A scripted model for tests and offline demos.

It implements the same backend interface as Gemini and answers from a list of steps keyed by
role (or from a Python responder). Steps can return valid JSON, raw text, a truncated output, a
refusal, or raise transient errors, which is how re-asks, fallbacks, escalation and stop rules are
tested deterministically and for free.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson
import yaml

from fixloop_core.errors import ProviderError
from fixloop_core.llm.types import ModelRequest, ModelResponse


@dataclass
class ScriptStep:
    role: str = "*"
    output: dict[str, Any] | None = None
    raw: str | None = None
    error: str | None = None  # timeout | 429 | 500 | refusal | max_tokens | fatal
    when_contains: str | None = None
    times: int = 1  # 0 = unlimited
    cached_tokens: int = 0


@dataclass
class ScriptedBackend:
    steps: list[ScriptStep] = field(default_factory=list)
    responder: Callable[[ModelRequest], Any] | None = None
    requests: list[ModelRequest] = field(default_factory=list)
    name: str = "scripted"

    @classmethod
    def from_yaml(cls, path: Path) -> ScriptedBackend:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(steps=[ScriptStep(**step) for step in data.get("steps", [])])

    def _match(self, request: ModelRequest) -> ScriptStep | None:
        text = request.system + "\n".join(request.parts)
        for step in self.steps:
            if step.role not in ("*", request.role):
                continue
            if step.when_contains and step.when_contains not in text:
                continue
            if step.times == 0:
                return step
            step.times -= 1
            if step.times == 0:
                self.steps.remove(step)
            return step
        return None

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        prompt_tokens = max(1, sum(len(p) for p in request.parts) // 4 + len(request.system) // 4)
        if self.responder is not None:
            produced = self.responder(request)
            if isinstance(produced, ModelResponse):
                return produced
            step = (
                ScriptStep(output=produced) if isinstance(produced, dict) else ScriptStep(raw=str(produced))
            )
        else:
            step = self._match(request)
            if step is None:
                raise ProviderError(
                    f"the scripted model has no response for role {request.role!r}", transient=False
                )
        if step.error in ("timeout", "429", "500"):
            raise ProviderError(
                f"scripted {step.error}",
                status=None if step.error == "timeout" else int(step.error),
                transient=True,
            )
        if step.error == "fatal":
            raise ProviderError("scripted fatal error", status=400, transient=False)
        finish = {"refusal": "SAFETY", "max_tokens": "MAX_TOKENS"}.get(step.error or "", "STOP")
        text = step.raw if step.raw is not None else orjson.dumps(step.output or {}).decode()
        if finish == "MAX_TOKENS":
            text = text[: max(1, len(text) // 2)]
        return ModelResponse(
            text=text,
            finish_reason=finish,
            prompt_tokens=prompt_tokens,
            cached_tokens=min(step.cached_tokens, prompt_tokens),
            output_tokens=max(1, len(text) // 4),
            thinking_tokens=0,
            latency_ms=5,
            model=request.model,
        )
