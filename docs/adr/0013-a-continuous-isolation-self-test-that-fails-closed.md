# ADR-0013: A continuous isolation self-test that fails closed

Status: accepted

## Context

Isolation can change silently: a daemon upgrade, a different default, a misconfigured node.

## Decision

A probe runs inside a sandbox exactly like candidate code and checks: non-root, no network, no capabilities, no-new-privileges, read-only root, writable tmp and workspace, no Docker socket, no secrets in the environment, cgroup limits, and gVisor in production. It runs at start-up and every 15 minutes; a runner that fails it stops taking work.

## Why

We test what the sandbox *is*, not what we configured it to be.

## Alternatives considered

- Trusting configuration: misses drift.

## Consequences

`fixloop-runner conformance` prints the report and exits non-zero on failure, for CI and on-call.
