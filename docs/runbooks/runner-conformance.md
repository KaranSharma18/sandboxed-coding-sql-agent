# A runner fails its isolation self-test

**Signals:** `fixloop_conformance_failures_total{check=...}`; log line
`sandbox_conformance_failed` with the failing checks. The runner stops taking work by design.

1. Run it by hand on the host: `fixloop-runner conformance`. The JSON names each failing check.
2. `gvisor_runtime` failing: runsc is missing or not registered (`docker info | grep -i runtime`);
   reinstall with `deploy/runner-host/install-gvisor.sh`.
3. `no_network` failing: a sandbox could connect out. Check that runs use `network_mode=none`
   and that the proxy network is `internal: true`.
4. `read_only_root`, `no_capabilities`, `non_root`: the daemon or image changed. Compare the
   image digest with `config/images.lock`.
5. Do not override the check. Drain the host, fix it, and the runner starts accepting again at
   the next self-test (or restart it).
