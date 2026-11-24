# Example: a date-parsing bug

`parse_date` swaps day and month, so `tests/test_dates.py::test_parse_dates` fails.

```powershell
uv run fixloop fix --repo examples/buggy-dates --tests tests/test_dates.py::test_parse_dates
```

Fixloop runs the whole suite first (two tests already pass and must keep passing), asks the
model for a plan and a patch, runs the candidate in a sandbox, reruns the target in a shuffled
order, and only then reports `verified` with the diff.
