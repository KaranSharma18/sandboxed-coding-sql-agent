# ADR-0024: SQL verification: static checks, read-only execution, differential data

Status: accepted

## Context

A query can match the expected rows by accident and still be wrong.

## Decision

Candidates are parsed and qualified with sqlglot (unknown identifiers, denied functions, time functions without `as_of`), cost-guarded with `EXPLAIN`, executed as a read-only role on a copy of the snapshot, compared with tolerances, checked against properties and assertions, and, with a reference query, compared again on mutated copies of the data (rows deleted, duplicated, NULLs added, values shifted).

## Why

Each layer is cheap and catches a different class of wrong answer; the mutated copies catch the 'right by accident' class that row comparison alone misses.

## Alternatives considered

- Comparing on the original data only: misses wrong joins and missing DISTINCT that happen to match.

## Consequences

Value checks stay hidden from the model; only shape requirements are shown, so it cannot hard-code the answer.
