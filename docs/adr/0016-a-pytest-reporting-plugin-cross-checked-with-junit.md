# ADR-0016: A pytest reporting plugin cross-checked with JUnit

Status: accepted

## Context

Parsing console output is fragile, and a single report can be faked by the code under test.

## Decision

A dependency-free plugin writes a JSONL report (collection, per-test outcomes, frames, assertion details, per-test coverage of changed files) next to pytest's own JUnit XML. The verdict requires the two reports and the exit code to agree.

## Why

Structured data makes traces compact and signatures reliable; two independent reports make tampering visible (`report_mismatch`).

## Alternatives considered

- Parsing pytest's terminal output: breaks across versions and plugins.

## Consequences

The plugin supports pytest 7 to 9 on Python 3.10 to 3.13, and a per-test timeout where SIGALRM exists.
