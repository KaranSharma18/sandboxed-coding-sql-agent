# Security policy

Please report vulnerabilities privately, through GitHub's "Report a vulnerability" button on
this repository (Security tab), or by email to the maintainer listed in `.github/CODEOWNERS`.
Do not open a public issue for a vulnerability.

Include what you found, how to reproduce it and what an attacker could do with it. We
acknowledge reports within three working days and aim to fix critical issues within 14 days.

Sandbox escapes, cross-tenant data access, credential exposure and ways to make an unverified
change look verified are the most important classes for this project. The threat model is in
[docs/security.md](docs/security.md).
