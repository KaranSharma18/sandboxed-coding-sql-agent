# ADR-0014: Wheels only, from an allowlist, through a proxy

Status: accepted

## Context

Installing dependencies is the one moment sandbox code needs the network, and an sdist runs arbitrary code at install time.

## Decision

Preparation runs `uv pip install --only-binary :all:` against our package proxy, on a network that reaches nothing else. The proxy serves only allowlisted projects, only wheels, only files whose SHA-256 matches the index, and logs every download. Executions have no network at all.

## Why

Supply-chain risk becomes a reviewed list, and data exfiltration has no route.

## Alternatives considered

- Open internet during prepare: exfiltration and malicious sdists.
- Pre-baked images per project: does not scale to arbitrary dependency sets.

## Consequences

Projects with sdist-only dependencies end as `env_failed` with a clear message.
