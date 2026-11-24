# ADR-0007: Strict structured outputs and format re-asks

Status: accepted

## Context

Free-text outputs need fragile parsing.

## Decision

Every role has a Pydantic schema with `extra=forbid`, enums and length limits. The provider gets a sanitized JSON schema for constrained decoding; Pydantic validates again. Invalid output gets up to two re-asks with the exact validation errors; truncated output gets a 'make it smaller' re-ask.

## Why

Validation errors are fixable feedback. Length limits make a runaway answer fail fast. The schema version is part of every cache key.

## Alternatives considered

- Regex parsing of free text: brittle.
- Unlimited re-asks: cost without bound.

## Consequences

A third invalid answer is a `format_error` attempt failure.
