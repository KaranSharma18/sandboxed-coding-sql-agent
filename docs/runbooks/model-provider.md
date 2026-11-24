# The model provider is failing

**Signals:** `fixloop_llm_calls_total{result="error"}` rising; logs `models_unavailable_waiting`.

1. Transient errors (429, 5xx, timeouts) are retried with full jitter; a model whose errors pass
   the breaker threshold is skipped for `open_seconds` and its fallback is used.
2. If both primary and fallback are down, tasks wait up to 10 minutes and are then handed back
   to the queue; they resume later from their checkpoints without repeating paid steps.
3. 400-class rejections are not retried: check `llm_calls.error` for the message (a model name
   that no longer exists, a schema the API rejects after an SDK upgrade).
4. To route around a bad model, change its role in `config/models.yaml` and roll out.
