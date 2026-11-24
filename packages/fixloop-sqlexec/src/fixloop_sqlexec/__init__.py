"""Runs inside SQL sandboxes: builds snapshot databases and executes guarded, read-only queries.

It only executes and serializes. Comparing results against the oracle happens in the worker
(`fixloop_core.verify_sql`), so the verification logic stays in one tested place.
"""
