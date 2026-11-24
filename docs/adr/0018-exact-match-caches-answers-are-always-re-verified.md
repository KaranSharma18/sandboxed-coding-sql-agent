# ADR-0018: Exact-match caches; answers are always re-verified

Status: accepted

## Context

The same task often arrives twice (retries, CI reruns, other agents).

## Decision

Four layers, all keyed by a hash of every input that can change the output and scoped per tenant: L1 model responses, L2 verified fixes (snapshot + oracle + environment), L3 execution results (by exec id, within a task), L4 failure memo (signature + oracle + environment, with 'the edits still apply' as precondition). L2 and L4 hits are verified in the sandbox like any candidate.

## Why

Rule zero: a cache may save work, never change an answer. Exact keys make hits safe; re-verification makes stale answers harmless.

## Alternatives considered

- Semantic (embedding) caches: hits on inputs that only look similar.

## Consequences

Cache data lives in a separate, evictable Redis; losing it costs money, never correctness.
