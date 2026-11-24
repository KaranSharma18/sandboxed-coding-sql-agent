"""Gemini backend over the official `google-genai` SDK (Developer API or Vertex AI).

Every call requests JSON constrained by the role's schema. Temperature is left at the provider
default (1.0), as Google recommends for Gemini 3 models (ADR-10).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from fixloop_core.errors import ProviderError
from fixloop_core.llm.types import ModelRequest, ModelResponse
from fixloop_core.settings import GeminiSettings

TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504}


class GeminiBackend:
    name = "gemini"

    def __init__(self, settings: GeminiSettings, client: Any | None = None) -> None:
        if client is not None:
            self.client = client
        else:
            from google import genai

            if settings.vertexai:
                self.client = genai.Client(
                    vertexai=True, project=settings.project, location=settings.location
                )
            else:
                if settings.api_key is None:
                    raise ValueError(
                        "FIXLOOP_GEMINI__API_KEY is required for the Gemini Developer API "
                        "(or set FIXLOOP_GEMINI__VERTEXAI=true with a project)"
                    )
                self.client = genai.Client(api_key=settings.api_key.get_secret_value())
        self._no_thinking_level: set[str] = set()

    def _config(self, request: ModelRequest, with_thinking: bool) -> Any:
        from google.genai import types

        kwargs: dict[str, Any] = {
            "system_instruction": request.system,
            "response_mime_type": "application/json",
            "response_json_schema": request.json_schema,
            "max_output_tokens": request.max_output_tokens,
            "candidate_count": 1,
            "http_options": types.HttpOptions(timeout=int(request.timeout_s * 1000)),
        }
        if with_thinking:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=types.ThinkingLevel[request.thinking.upper()]
            )
        return types.GenerateContentConfig(**kwargs)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        from google.genai import errors, types

        contents = [types.Content(role="user", parts=[types.Part(text=part) for part in request.parts])]
        with_thinking = request.model not in self._no_thinking_level
        started = time.monotonic()
        for _ in range(2):
            try:
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=request.model, contents=contents, config=self._config(request, with_thinking)
                    ),
                    timeout=request.timeout_s + 5,
                )
                break
            except errors.APIError as exc:
                status = getattr(exc, "code", None)
                message = str(exc)
                if status == 400 and with_thinking and "thinking" in message.lower():
                    # This model does not support the configured thinking level: use its default.
                    self._no_thinking_level.add(request.model)
                    with_thinking = False
                    continue
                raise ProviderError(
                    message[:500], status=status, transient=status is None or status in TRANSIENT_STATUS
                ) from exc
            except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
                raise ProviderError(f"{type(exc).__name__}: {exc}"[:300], transient=True) from exc
        else:  # pragma: no cover - loop always breaks or raises
            raise ProviderError("no response", transient=True)
        latency = int((time.monotonic() - started) * 1000)
        candidates = getattr(response, "candidates", None) or []
        candidate = candidates[0] if candidates else None
        finish = "STOP"
        if candidate is not None and candidate.finish_reason is not None:
            finish = getattr(candidate.finish_reason, "name", str(candidate.finish_reason))
        elif getattr(getattr(response, "prompt_feedback", None), "block_reason", None):
            finish = "BLOCKLIST"
        text = ""
        if candidate is not None and candidate.content is not None and candidate.content.parts:
            text = "".join(
                part.text or "" for part in candidate.content.parts if not getattr(part, "thought", False)
            )
        usage = response.usage_metadata
        return ModelResponse(
            text=text,
            finish_reason=finish,
            prompt_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            cached_tokens=int(getattr(usage, "cached_content_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
            thinking_tokens=int(getattr(usage, "thoughts_token_count", 0) or 0),
            latency_ms=latency,
            model=request.model,
        )
