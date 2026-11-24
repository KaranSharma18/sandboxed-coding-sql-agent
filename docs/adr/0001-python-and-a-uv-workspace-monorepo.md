# ADR-0001: Python and a uv workspace monorepo

Status: accepted

## Context

Fixloop has five services and four shared packages. They share domain models, settings and many tests, and the team works mostly in Python on Windows and Linux.

## Decision

One repository, one uv workspace (`packages/*`, `apps/*`, `eval`), one lock file. Services run on Python 3.12; the sandbox-side packages support 3.10 to 3.13.

## Why

A shared lock file means every service runs the exact versions CI tested. uv resolves and installs in seconds on Windows and Linux alike, so `uv sync` is the whole setup. Typed Pydantic models shared across services remove a class of contract bugs that separate repositories invite.

## Alternatives considered

- Separate repositories per service: version skew between services, duplicated models.
- Poetry or pip-tools: slower, and workspace support is weaker.

## Consequences

Changes to `fixloop-core` affect every service, so CI runs the full suite on every pull request. Import-linter contracts keep the packages from tangling.
