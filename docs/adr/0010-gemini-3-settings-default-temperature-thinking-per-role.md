# ADR-0010: Gemini 3 settings: default temperature, thinking per role

Status: accepted

## Context

Gemini 3 models are tuned for temperature 1.0; lowering it can cause loops and worse reasoning.

## Decision

We leave temperature at the provider default and control behaviour with structured output, thinking levels per role (`low` for digests, `high` for escalated repairs) and output caps.

## Why

Follows Google's guidance for these models; determinism comes from our caches and checkpoints, not from sampling settings.

## Alternatives considered

- Temperature 0 for determinism: degrades Gemini 3 quality and is still not deterministic.

## Consequences

If a model rejects a thinking level, the backend retries once with the model's default and remembers that.
