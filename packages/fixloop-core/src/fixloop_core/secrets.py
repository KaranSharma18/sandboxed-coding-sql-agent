"""Secret detection and redaction for anything that might reach a model prompt.

Customer repositories sometimes contain credentials. Before a file enters a prompt it passes
these rules (a compact subset of the well-known gitleaks rules). Matches are replaced with
`<redacted:kind>`, key files are excluded entirely, and policy refuses any edit whose anchor
contains a redaction marker, so a secret can never be overwritten by accident.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

REDACTION_PREFIX = "<redacted:"

_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----[\s\S]*?"
            r"(?:-----END (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----|\Z)"
        ),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_live_[0-9a-zA-Z]{20,}\b")),
    ("llm_api_key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("url_credentials", re.compile(r"\b[a-z][a-z0-9+.-]{1,20}://[^:\s/@]{1,64}:[^@\s/]{3,128}@")),
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)"
            r"\b\s*[=:]\s*['\"][^'\"\s]{8,}['\"]"
        ),
    ),
]

SECRET_FILE_GLOBS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials.json",
    "service-account*.json",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "*.keystore",
]


@dataclass(frozen=True)
class SecretHit:
    kind: str
    line: int
    start: int
    end: int


def is_secret_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_FILE_GLOBS)


def scan_text(text: str) -> list[SecretHit]:
    hits: list[SecretHit] = []
    for kind, pattern in _RULES:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            hits.append(SecretHit(kind, line, match.start(), match.end()))
    # Drop hits contained in an earlier, larger hit (a JWT inside an assigned secret, etc.).
    hits.sort(key=lambda h: (h.start, -(h.end - h.start)))
    merged: list[SecretHit] = []
    for hit in hits:
        if merged and hit.start < merged[-1].end:
            continue
        merged.append(hit)
    return merged


def redact_text(text: str) -> tuple[str, list[SecretHit]]:
    hits = scan_text(text)
    if not hits:
        return text, []
    pieces: list[str] = []
    cursor = 0
    for hit in hits:
        pieces.append(text[cursor : hit.start])
        # Keep line structure so line numbers in traces still point at the right place.
        newlines = text.count("\n", hit.start, hit.end)
        pieces.append(f"{REDACTION_PREFIX}{hit.kind}>" + "\n" * newlines)
        cursor = hit.end
    pieces.append(text[cursor:])
    return "".join(pieces), hits


def contains_redaction(text: str) -> bool:
    return REDACTION_PREFIX in text
