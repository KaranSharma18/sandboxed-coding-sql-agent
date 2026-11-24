# Runner hosts

A runner host is a Linux machine (or a dedicated Kubernetes node) that runs Docker with gVisor
and the `fixloop-runner` process. Nothing else runs there: if a sandbox ever escaped, the most
it could reach is a host that holds no customer secrets and no credentials except a scoped
object-store key and the Redis queue.

## Setup (Ubuntu 24.04)

1. Install Docker Engine: https://docs.docker.com/engine/install/ubuntu/
2. Install gVisor and register it with Docker: `sudo ./install-gvisor.sh`
3. Merge `daemon.json` into `/etc/docker/daemon.json`, then `sudo systemctl restart docker`.
4. Check: `docker run --rm --runtime=runsc hello-world`.
5. Run the runner with `FIXLOOP_RUNNER__ISOLATION=production` and `FIXLOOP_RUNNER__RUNTIME=runsc`,
   then `fixloop-runner conformance` must print `"passed": true` with `"isolation": "gvisor"`.

The runner refuses to take work while its self-test fails, and repeats the test every 15 minutes
(`FIXLOOP_RUNNER__CONFORMANCE_INTERVAL_S`).

## Seccomp

Sandboxes use Docker's default seccomp profile, which is an allowlist. Under gVisor the
untrusted code also never talks to the host kernel directly: gVisor's Sentry implements the
system calls and is itself confined by its own seccomp filter. `FIXLOOP_RUNNER__SECCOMP_PROFILE`
exists for teams that maintain a stricter profile; never point it at a hand-written deny list,
because a custom profile *replaces* Docker's default rather than adding to it.
