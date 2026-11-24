# Example: write a SQL query and prove it (DuckDB)

Question: *revenue from paid orders and the number of paying customers per country, highest
revenue first.* `reference.sql` is a trusted query whose result is the answer; Fixloop compares
the model's query with it on the original data and on mutated copies (rows deleted, duplicated,
NULLs added), which catches queries that are right only by accident.

```powershell
uv run fixloop sql --dialect duckdb --ddl examples/sql-duckdb/schema.sql `
  --seed customers=examples/sql-duckdb/customers.csv --seed orders=examples/sql-duckdb/orders.csv `
  --question "Revenue from paid orders and the number of paying customers per country, highest revenue first." `
  --reference-query examples/sql-duckdb/reference.sql --order-matters --sort-key revenue
```
(On Linux and macOS use `\` instead of the PowerShell backtick for line continuations.)
