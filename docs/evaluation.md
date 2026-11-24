# Evaluation

`fixloop-eval` runs a suite of tasks against a running Fixloop API, the same way a user would,
and reports how often the outcome matched what we expected, what it cost and how long it took.

```powershell
uv run fixloop-eval run --suite fixtures --limit 10 --max-cost-usd 2
uv run fixloop-eval report eval/results/fixtures-<timestamp>.jsonl
```

A suite is a folder of JSON files under `eval/suites/`. Each case is a task spec plus the
expected outcome, optional tags and optional *hidden tests*: a folder uploaded as the task's
hidden tests, which acts as a gold check the model never sees (a patch that only satisfies the
visible test fails it). Sources can point at example folders in the repository.

What to look at:

| metric | why it matters |
|---|---|
| as expected | the headline number; includes the guard cases (`nothing_to_fix`) that must not be "fixed" |
| verified | share of tasks that ended with a proven change |
| mean cost | dominated by escalations; compare profiles on the same suite |
| p50 / p95 seconds | p95 shows slow suites and escalations |
| mean attempts | creeping attempts usually mean a prompt or context regression |

Compare runs only on the same suite, model configuration and prompt version. Model and prompt
versions are part of every call record, so a regression can be traced to the change that
caused it. The nightly workflow runs the live contract tests; run the smoke suite before
changing prompts or model routing.
