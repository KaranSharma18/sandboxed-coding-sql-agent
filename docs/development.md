# Development

## Setup

```powershell
uv sync --all-packages          # every package and app into one .venv
uv run pre-commit install        # optional: ruff, gitleaks and friends before each commit
```

## Everyday commands

| command | does |
|---|---|
| `uv run poe dev` / `poe down` / `poe logs` | build sandbox images, start / stop / follow the Docker stack |
| `uv run poe test` | unit tests, no services |
| `uv run poe test-int` | integration tests (Postgres + Redis) |
| `uv run poe e2e` | end to end with the scripted model |
| `uv run poe test-docker` | Docker backend tests (needs `poe images`) |
| `uv run poe lint` / `poe fmt` / `poe typecheck` | ruff + import-linter, format, pyright |
| `uv run poe migrate` / `poe seed` | migrations, local tenant and key (stack in Docker) |
| `uv run poe conformance` | the sandbox isolation self-test |

## Running services on your machine

The Docker stack is the normal way. To debug one service in your IDE, keep the rest in Docker
and run that service on the host with `.env` pointing at `localhost` (the commented lines in
`.env.example`), for example `uv run fixloop-worker` after `docker compose stop worker`.

For the whole system without Docker sandboxes (Linux or macOS, your own code only):
`FIXLOOP_RUNNER__BACKEND=local`, `FIXLOOP_RUNNER__ALLOW_UNSAFE_LOCAL=true`,
`FIXLOOP_RUNNER__INDEX_URL=https://pypi.org/simple`, then `uv run fixloop-runner`.

## Layering rules

Enforced by `lint-imports` (configured in `pyproject.toml`):

* `fixloop_core` never imports an app; apps never import each other.
* The pure modules (`patching`, `policy`, `traces`, `verify_*`, `repomap`, `context`, `scoring`)
  and the loop (`fixloop_core.loop`) never import Redis, SQLAlchemy, Docker, boto3, FastAPI or the
  Gemini SDK. The loop talks to `fixloop_core.ports`.

## Common changes

* **A new prompt version**: copy `prompts/<role>/v1.j2` to `v2.j2`, change `PROMPT_VERSION` when
  you are ready, compare with `fixloop-eval` on the same suite. Old answers are never served for
  new prompts (the version is in every cache key).
* **A new policy rule**: `config/policies.yaml` for globs and lists; `fixloop_core/policy.py` for
  AST checks. Add a test in `tests/unit/test_policy.py` with a patch that must be refused.
* **A new command template**: `config/command_templates.yaml`. Parameters are typed (`int`,
  `nodeid`, `nodeid_list`, `path_list`, `str`) and validated by the runner; a model never
  chooses a command.

## Debugging tips

* Every model call's exact prompt and response is stored: see `llm_calls.request_ref` and
  `response_ref` (object keys) for a task.
* `fixloop status <task>` lists attempts with verdicts; the attempt diffs and traces are under
  `tenants/<tenant>/tasks/<task>/attempts/` in the object store.
* The scripted backend (`FIXLOOP_GEMINI__BACKEND=scripted`) replays YAML answers, which makes
  any loop behaviour reproducible without a key.
* `FIXLOOP_RUNNER__KEEP_EXEC_DIRS=true` keeps local-backend workspaces for inspection.
