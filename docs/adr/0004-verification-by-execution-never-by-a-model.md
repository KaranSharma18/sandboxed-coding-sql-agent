# ADR-0004: Verification by execution, never by a model

Status: accepted

## Context

A model asked whether its own patch is correct says yes far too often.

## Decision

Correctness is decided only by running code: pytest targets, regressions, hidden tests, reruns, SQL comparisons and assertions. No model output can mark a task verified.

## Why

Execution is the only judge that cannot be talked into a wrong answer. It is also what the user would do next anyway, so the agent's result is directly trustworthy.

## Alternatives considered

- An LLM judge: cheap but unreliable, and it can be prompt-injected by repository content.
- Static review only: misses runtime behaviour.

## Consequences

Tasks without an oracle can only be `executed_only`, never `verified`, and the result says so.
