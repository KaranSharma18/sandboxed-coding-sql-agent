# ADR-0023: A least-privilege GitHub App that only opens draft PRs

Status: accepted

## Context

Write access to customer repositories is the most dangerous permission we hold.

## Decision

Only the publisher holds the App's private key. Tokens are minted per repository with only the permissions an operation needs (`contents: read` for the worker's download, write scopes only for publishing). Fixloop never pushes to existing branches: it creates `fixloop/<task>` and opens a draft PR. Comment commands require write permission and are refused on forks.

## Why

Even a fully compromised worker can only read one repository for one hour; a human always reviews and merges.

## Alternatives considered

- Pushing directly to the PR branch: surprising and dangerous.

## Consequences

Webhooks are signature-checked and deduplicated by delivery id.
