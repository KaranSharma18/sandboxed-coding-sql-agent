# ADR-0006: Anchored search/replace edits

Status: accepted

## Context

Models are bad at producing unified diffs with correct line numbers and hunk headers.

## Decision

The model returns edits: path, action (`replace`, `insert_before`, `insert_after`, `create`, `delete`) and an exact `search` anchor that must occur exactly once. We apply them, tolerate only whitespace differences, check syntax, and produce the git diff ourselves.

## Why

Anchors fail loudly and specifically ('found 2 times', with the closest region) instead of applying in the wrong place. A failed apply becomes a precise re-ask rather than a wasted sandbox run.

## Alternatives considered

- Unified diffs from the model: frequent malformed hunks.
- Whole-file rewrites: huge outputs, silent unrelated changes.

## Consequences

Two edits that overlap fail; the model is asked to merge them.
