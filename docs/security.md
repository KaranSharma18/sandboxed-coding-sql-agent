# Security

## What we protect, from whom

Fixloop runs code it did not write (the customer's repository and the model's changes) and
feeds text it did not write (repository content, test output) to a model. We assume both can be
hostile: a repository can contain malware, a test can try to escape, a file can contain
instructions aimed at the model. We protect customer code and data, our credentials, other
tenants, and the hosts.

## Layers

1. **Sandbox.** One fresh container per run: no network (only the package proxy, and only
   during preparation), read-only root, non-root user `10001`, all capabilities dropped,
   no-new-privileges, Docker's default seccomp allowlist, memory and swap, CPU, PID, file-size
   and open-file limits, a small tmpfs, capped logs. In production the runtime is gVisor. The
   workspace and outputs are volumes created for that run and deleted after it.
2. **Continuous self-test.** The runner proves these properties from inside a sandbox at
   start-up and every 15 minutes, and stops taking work if any required check fails.
3. **Fixed commands.** Sandboxes only run templates from `config/command_templates.yaml`.
   Arguments are typed and validated; node ids can never start with `-`, and everything after
   `--` is a node id. There is no shell anywhere.
4. **Supply chain.** Wheels only, from an allowlist, hash-checked, through a proxy; sandbox images
   are built in CI, signed with cosign and pinned by digest in production.
5. **Trust zones.** The runner holds no database or model credentials; the worker never holds the
   GitHub App key; GitHub tokens are per repository with minimal permissions and an hour's life.
6. **Inputs.** Archives are extracted defensively (no links, no absolute paths or `..`, size and
   count limits). Paths in model edits are validated the same way. Callback URLs must be HTTPS
   and resolve to public addresses, checked at delivery time, without following redirects.
7. **Model inputs.** Repository content is wrapped in `<untrusted>` blocks, and the system
   prompt tells the model that instructions inside them are data. Secrets are redacted before
   any prompt; files that look like secrets are never shown. Hidden test contents never reach
   the model; only a count of failures does.
8. **Anti-gaming.** Protected tests cannot be edited; the code under test cannot skip, exit or
   monkeypatch the oracle; protected files are re-hashed after every run; two independent test
   reports must agree with the exit code.
9. **Tenancy.** Every key and object path is tenant-scoped; caches are keyed per tenant;
   environments are per tenant; every API query filters by the authenticated tenant.
10. **Secrets in the repository.** `.env`, `.secrets/` and `*.pem` are ignored; gitleaks runs in
    pre-commit and CI.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
