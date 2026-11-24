# Configuration

Fixloop has two kinds of configuration:

* **Process settings**: environment variables (or a `.env` file in development), validated at
  start-up. A missing or malformed value stops the process with a clear message.
* **Behaviour configuration**: reviewed YAML files in `config/`, versioned with the code. They
  change what the agent does, so they go through pull requests like code.

## Environment variables

Every variable starts with `FIXLOOP_` and nests with a double underscore: the setting
`runner.backend` is `FIXLOOP_RUNNER__BACKEND`. Map and list settings take JSON, for example
`FIXLOOP_RUNNER__IMAGE_OVERRIDES={"python-3.12":"my-registry/sbx-python:3.12"}`.

Clients (Gemini, S3, Redis) are always built from these values, never from ambient variables
such as `GOOGLE_API_KEY`, so what you read here is what runs.

| variable | default | meaning |
|---|---|---|
| `FIXLOOP_ENV` | `local` | local, test, staging or production. Production refuses unsafe settings at start-up. |
| `FIXLOOP_CONFIG_DIR` | `(found automatically)` | Folder with models.yaml, policies.yaml, command_templates.yaml, limits.yaml, allowlist.txt. |
| `FIXLOOP_PROMPTS_DIR` | `(found automatically)` | Folder with the versioned prompt templates. |
| `FIXLOOP_DATABASE__URL` | `postgresql+asyncpg://fixloop:fixloop@localhost:5432/fixloop` | SQLAlchemy URL (asyncpg). |
| `FIXLOOP_DATABASE__POOL_SIZE` | `10` | Connections per process. |
| `FIXLOOP_DATABASE__ECHO` | `False` | Log SQL (debugging only). |
| `FIXLOOP_REDIS__QUEUE_URL` | `redis://localhost:6379/0` | Queues, budgets, locks, events. Must not evict keys. |
| `FIXLOOP_REDIS__CACHE_URL` | `redis://localhost:6380/0` | Caches only (LRU is fine). May be the same server in development. |
| `FIXLOOP_STORAGE__BACKEND` | `s3` | s3 (MinIO, S3, GCS interoperability) or local (a folder). |
| `FIXLOOP_STORAGE__BUCKET` | `fixloop-dev` | Bucket name. |
| `FIXLOOP_STORAGE__ENDPOINT_URL` | `http://localhost:9000` | S3 endpoint the services use. |
| `FIXLOOP_STORAGE__PUBLIC_ENDPOINT_URL` |  | Endpoint clients use for presigned URLs (if different). |
| `FIXLOOP_STORAGE__REGION` | `us-east-1` | S3 region. |
| `FIXLOOP_STORAGE__ACCESS_KEY_ID` |  | S3 access key. |
| `FIXLOOP_STORAGE__SECRET_ACCESS_KEY` |  | S3 secret key. |
| `FIXLOOP_STORAGE__LOCAL_ROOT` | `.fixloop-data/objects` | Folder for the local backend. |
| `FIXLOOP_GEMINI__BACKEND` | `gemini` | gemini, or scripted (tests and demos; no network, no cost). |
| `FIXLOOP_GEMINI__API_KEY` |  | Gemini Developer API key. |
| `FIXLOOP_GEMINI__VERTEXAI` | `False` | Use Vertex AI instead of the Developer API. |
| `FIXLOOP_GEMINI__PROJECT` |  | Vertex AI project. |
| `FIXLOOP_GEMINI__LOCATION` | `us-central1` | Vertex AI location. |
| `FIXLOOP_GEMINI__SCRIPTED_PATH` |  | YAML script for the scripted backend. |
| `FIXLOOP_GITHUB__ENABLED` | `False` | Turn on the GitHub App integration. |
| `FIXLOOP_GITHUB__APP_ID` |  | GitHub App id. |
| `FIXLOOP_GITHUB__PRIVATE_KEY` |  | The App's private key (PEM). Prefer the path setting. |
| `FIXLOOP_GITHUB__PRIVATE_KEY_PATH` |  | Path to the App's private key file. |
| `FIXLOOP_GITHUB__WEBHOOK_SECRET` |  | Secret used to verify GitHub webhook signatures. |
| `FIXLOOP_GITHUB__API_URL` | `https://api.github.com` | GitHub API base URL (GitHub Enterprise: https://HOST/api/v3). |
| `FIXLOOP_GITHUB__BOT_LOGIN` | `fixloop[bot]` | The App's bot login (to ignore its own comments). |
| `FIXLOOP_RUNNER__BACKEND` | `docker` | docker (isolated) or local (no isolation; tests and demos only). |
| `FIXLOOP_RUNNER__ALLOW_UNSAFE_LOCAL` | `False` | Second switch required for the local backend. |
| `FIXLOOP_RUNNER__ISOLATION` | `dev` | dev (hardened runc) or production (requires gVisor). |
| `FIXLOOP_RUNNER__RUNTIME` |  | Container runtime name, runsc in production. |
| `FIXLOOP_RUNNER__SLOTS` | `2` | Concurrent sandboxes per runner. |
| `FIXLOOP_RUNNER__RUNNER_ID` | `hostname` | Stable id for this runner (hostname). |
| `FIXLOOP_RUNNER__INDEX_URL` | `http://pkgproxy:8080/simple` | Package index for prepare (the proxy), or a folder of wheels. |
| `FIXLOOP_RUNNER__PROXY_NETWORK` | `fixloop-sandbox-proxy` | Docker network that reaches only the package proxy. |
| `FIXLOOP_RUNNER__REQUIRE_PINNED_IMAGES` | `False` | Refuse images without a digest in config/images.lock. |
| `FIXLOOP_RUNNER__IMAGE_OVERRIDES` |  | JSON map of logical image name to image reference. |
| `FIXLOOP_RUNNER__WORKDIR` | `.fixloop-data/runner` | Runner state (environment index, local backend workspaces). |
| `FIXLOOP_RUNNER__LOCAL_PYTHONS` |  | Local backend: JSON map of Python version (or sql/default) to interpreter path. |
| `FIXLOOP_RUNNER__LOCAL_UID` | `65534` | Local backend: user to switch to when started as root. |
| `FIXLOOP_RUNNER__PG_BIN_DIR` |  | Local backend: PostgreSQL bin folder for SQL tasks. |
| `FIXLOOP_RUNNER__SECCOMP_PROFILE` |  | A stricter seccomp profile file (see deploy/runner-host). |
| `FIXLOOP_RUNNER__CONFORMANCE_INTERVAL_S` | `900` | How often the isolation self-test repeats. |
| `FIXLOOP_RUNNER__DOCKER_HOST` |  | Docker daemon URL if not the default socket. |
| `FIXLOOP_RUNNER__ENV_TTL_DAYS` | `7` | Delete prepared environments unused for this long. |
| `FIXLOOP_RUNNER__KEEP_EXEC_DIRS` | `False` | Local backend: keep workspaces after runs (debugging). |
| `FIXLOOP_RUNNER__DRAIN_TIMEOUT_S` | `120` | On shutdown, wait this long for running sandboxes. |
| `FIXLOOP_WORKER__CONCURRENCY` | `4` | Tasks one worker runs at the same time. |
| `FIXLOOP_WORKER__WORKER_ID` | `hostname` | Stable id for this worker. |
| `FIXLOOP_WORKER__LEASE_SECONDS` | `60` | Task lease length. |
| `FIXLOOP_WORKER__HEARTBEAT_SECONDS` | `20` | Lease renewal interval. |
| `FIXLOOP_WORKER__RECONCILE_INTERVAL_S` | `60` | How often lost tasks are re-enqueued. |
| `FIXLOOP_WORKER__EXEC_WAIT_SLACK_S` | `120` | Extra wait beyond an execution's deadline. |
| `FIXLOOP_API__HOST` | `0.0.0.0` | Listen address. |
| `FIXLOOP_API__PORT` | `8080` | Listen port. |
| `FIXLOOP_API__PUBLIC_URL` | `http://localhost:8080` | External URL (links, local blob URLs). |
| `FIXLOOP_API__KEY_CACHE_TTL_S` | `60` | How long a verified API key is cached (revocation delay). |
| `FIXLOOP_API__UPLOAD_TOKEN_SECRET` | `(secret)` | HMAC secret for local blob URLs. Must be changed outside local development. |
| `FIXLOOP_API__CORS_ORIGINS` |  | JSON list of allowed browser origins. |
| `FIXLOOP_PUBLISHER__POLL_INTERVAL_S` | `1.0` | Outbox polling interval. |
| `FIXLOOP_PUBLISHER__WEBHOOK_TIMEOUT_S` | `10.0` | Customer webhook timeout. |
| `FIXLOOP_PUBLISHER__ALLOW_PRIVATE_CALLBACKS` | `False` | Tests only: allow callbacks to private addresses. |
| `FIXLOOP_OBSERVABILITY__LOG_LEVEL` | `INFO` | Log level. |
| `FIXLOOP_OBSERVABILITY__LOG_JSON` | `True` | JSON logs (true) or console logs (false). |
| `FIXLOOP_OBSERVABILITY__OTLP_ENDPOINT` |  | OpenTelemetry collector endpoint (traces). |
| `FIXLOOP_OBSERVABILITY__METRICS_PORT` |  | Prometheus metrics port for worker, runner and publisher. |

The package proxy has its own small set: `PKGPROXY_UPSTREAM` (default `https://pypi.org`),
`PKGPROXY_ALLOWLIST` (`config/allowlist.txt`), `PKGPROXY_CACHE_DIR`, `PKGPROXY_HOST`,
`PKGPROXY_PORT` (`8080`), `PKGPROXY_LOG_LEVEL`.

The CLI reads `FIXLOOP_API_URL` and `FIXLOOP_API_KEY`, then `~/.fixloop/config.json` (written by
`fixloop login`), then `.fixloop-data/local-api-key.txt`.

Tests read `FIXLOOP_TEST_DATABASE_URL`, `FIXLOOP_TEST_REDIS_URL`, `FIXLOOP_TEST_REDIS_CACHE_URL`
(otherwise they start their own servers when the binaries exist), `FIXLOOP_TEST_INDEX_URL`,
`FIXLOOP_TEST_PYTHON_IMAGE` and `FIXLOOP_TEST_PROXY_NETWORK`.

## config/ files

| file | what it controls |
|---|---|
| `models.yaml` | prices per model (with effective dates), the model, fallback, thinking level, output cap, timeout and input budget for every role, profiles (`economy`, `default`, `quality`), retries, circuit breaker, shared rate limits |
| `policies.yaml` | patch policy: protected globs, infrastructure files, protected `pyproject.toml`/`setup.cfg` sections, size limits, test-gaming rules (skips, exits, monkeypatching the oracle), risky modules and calls; SQL policy: denied functions per dialect, time functions |
| `command_templates.yaml` | the only commands a sandbox ever runs, with typed, validated parameters, and the logical image names |
| `limits.yaml` | per-plan task budgets and tenant ceilings, the global daily cost cap, sandbox limits, intake size limits |
| `allowlist.txt` | the packages the proxy will serve (one per line, PEP 503 normalized) |
| `images.lock` | pinned image digests, written by the release workflow; required in production |

### Changing a model or price

Edit `models.yaml`, add a price row with an `effective` date for any new model, and run
`uv run poe test` (a test fails if a role points at a model without a price). Prompt and schema
versions are part of every cache key, so changing a model never serves stale answers: the model
name is in the key too.

### Allowing a new dependency

Add the project name to `config/allowlist.txt` in a pull request, with one line on why. The
proxy picks it up on restart. Only wheels are ever served, so a project without wheels for the
sandbox's Python version still cannot be installed.
