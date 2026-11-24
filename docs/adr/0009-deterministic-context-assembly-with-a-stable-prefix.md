# ADR-0009: Deterministic context assembly with a stable prefix

Status: accepted

## Context

Caching only works if equal inputs produce byte-identical prompts, and provider prefix caching only discounts identical prefixes.

## Decision

Prompts are built from ordered blocks: task frame, repository map, target tests, first file selection (original versions), then files added later, attempt history, current state, failure, instruction. Everything is sorted and budgeted deterministically. Repository content is wrapped as `<untrusted>` data, with secrets redacted.

## Why

Plan, patch and repair share the first four blocks word for word, which is where most tokens are. Deterministic prompts make the L1 cache and replay possible.

## Alternatives considered

- Re-ranking files every call: better recall, no cache hits, no reproducibility.

## Consequences

Files requested later are appended, never inserted, so the prefix stays stable.
