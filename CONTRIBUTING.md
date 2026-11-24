# Contributing

Thanks for helping. The short version:

1. `uv sync --all-packages` and `uv run pre-commit install`.
2. Make the change with tests. A bug fix starts with a test that fails without it.
3. `uv run poe lint`, `uv run poe typecheck`, `uv run poe test`, and `poe test-int` / `poe e2e`
   when you touched services or the loop.
4. Open a pull request with what changed and why. If a design decision changed, add an ADR in
   `docs/adr/` that supersedes the old one.

Ground rules that reviews will check:

* No model output may decide correctness; verification stays execution-based.
* The loop and the pure modules stay free of infrastructure imports (import-linter enforces it).
* Anything a sandbox runs comes from `config/command_templates.yaml`.
* Every external side effect goes through the outbox.
* Never commit secrets, real customer code or `.env` files.

Commit messages follow Conventional Commits (`fix:`, `feat:`, `docs:` ...).
