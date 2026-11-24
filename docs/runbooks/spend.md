# Spend is higher than expected

1. `GET /v1/usage` per tenant, or `fixloop_llm_cost_usd_total` by model and role.
2. A rising share of `repair` on the Pro model means more escalations: look for a prompt or
   context regression (compare prompt versions in `llm_calls`).
3. A falling `fixloop_llm_calls_avoided_total` means caches stopped hitting: a changed prompt
   version, schema version or model invalidates L1 by design.
4. Hard caps: per task (`config/limits.yaml`), per tenant per day, and the global daily cap.
   Lower them before anything else if spend is out of control; tasks then end as
   `exhausted_budget` with the dimension named.
