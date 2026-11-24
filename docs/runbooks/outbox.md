# Draft PRs or webhooks are not arriving

1. Find the item: `SELECT kind, status, attempts, last_error, next_attempt_at FROM outbox WHERE task_id = '<task>';`
2. `pending` with a future `next_attempt_at`: it is backing off after errors; read `last_error`.
3. `failed`: a permanent error (403 from GitHub means the App lacks permission on that repository;
   `UnsafeCallback` means the callback URL resolves to a private address).
4. `working` for more than five minutes: a publisher died mid-item; it is put back to `pending`
   automatically.
5. After fixing the cause, set the item back: `UPDATE outbox SET status = 'pending', next_attempt_at = now() WHERE id = '<id>';`
   Every handler is idempotent, so retrying never duplicates a PR or a comment.
