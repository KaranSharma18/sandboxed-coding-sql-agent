# fixloop-sqlexec

Runs inside the SQL sandboxes. `prepare` builds the snapshot database (DuckDB files, or a
Postgres data directory with an admin and a read-only role) from DDL and seeds, computes column
statistics for the prompt, and makes mutated copies for differential testing. `run` executes the
candidate (and the reference query) read-only with a statement timeout and an `EXPLAIN` cost
guard on every dataset, evaluates assertions, and writes `sql_result.json`. Comparison happens in
the worker, not here.

```text
python -m fixloop_sqlexec prepare --spec sql_prepare.json --env /env --out /out
python -m fixloop_sqlexec run --spec sql_run.json --env /env --out /out --work /workspace/.fixloop/pgrun
```
