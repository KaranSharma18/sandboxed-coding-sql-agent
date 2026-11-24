# ADR-0015: Environments cached per runner and per tenant

Status: accepted

## Context

Preparing an environment takes seconds to minutes; every candidate needs it.

## Decision

An environment is built once per (tenant, environment hash) on a runner and mounted read-only afterwards. The hash covers the image, the pinned digest, the Python version and the requirement lines.

## Why

Per-tenant environments mean one tenant's preparation can never leave anything for another. Per-runner caching needs no shared storage; a miss costs one prepare.

## Alternatives considered

- A shared, global environment cache: cross-tenant poisoning risk and shared-storage complexity.

## Consequences

Unused environments are removed after `env_ttl_days` (7).
