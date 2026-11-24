# ADR-0002: Gemini through the official google-genai SDK, routed per role

Status: accepted

## Context

The loop has distinct jobs (plan, patch, repair, digest, SQL) with very different difficulty and cost.

## Decision

Every model call goes through `LLMService.generate` with a *role*. `config/models.yaml` maps each role and profile to a model, a fallback, a thinking level, an output cap and a timeout. Flash handles plan and patch, Flash-Lite digests traces, Pro handles escalated repairs.

## Why

Routing by role puts the expensive model only where it changes outcomes (a repair after the cheap model failed). One door for every call means budgets, caching, retries, call records and cost accounting cannot be bypassed.

## Alternatives considered

- One model for everything: either too expensive or too weak.
- Letting the model choose its own model: unpredictable cost.

## Consequences

Prices live in config with effective dates; a startup check fails if a role uses a model without a price.
