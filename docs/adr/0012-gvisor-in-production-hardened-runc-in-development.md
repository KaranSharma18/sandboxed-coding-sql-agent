# ADR-0012: gVisor in production, hardened runc in development

Status: accepted

## Context

Containers share the host kernel; one kernel bug can be an escape.

## Decision

Production runner hosts run sandboxes under gVisor (`runsc`), a user-space kernel. Development uses runc with the same hardening flags: no network, read-only root, all capabilities dropped, no-new-privileges, non-root user, memory, CPU, PID, file-size and open-file limits, a small tmpfs.

## Why

gVisor removes most of the host kernel attack surface for a modest performance cost on test workloads. Keeping the flags identical in development means tests exercise the real configuration.

## Alternatives considered

- Firecracker microVMs: stronger isolation, much more operational work.
- runc only: one kernel bug away from an escape.

## Consequences

Production settings (`ISOLATION=production`) refuse to start without `runsc`.
