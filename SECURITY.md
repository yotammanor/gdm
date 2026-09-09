# Security policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

## Reporting a vulnerability

Please report security issues privately to
[yotam.manor@gmail.com](mailto:yotam.manor@gmail.com).

Include:

- A description of the issue and its impact
- Steps to reproduce
- Affected versions
- Any suggested fix or mitigation

Do not open public GitHub issues for undisclosed vulnerabilities.

## Scope

Reports are welcome for:

- Credential or token handling in GDM
- Unsafe defaults in OAuth scopes or sharing helpers
- Data exposure across project roots or tenants
- Injection or unsafe deserialization in codecs

Out of scope:

- Google Drive or Sheets platform vulnerabilities (report those to Google)
- Misconfigured Drive sharing by project owners
- Issues that require compromising a user's Google account outside GDM

## Response

Maintainers aim to acknowledge reports within **5 business days** and will
coordinate disclosure after a fix or mitigation is available.

## Security model

GDM delegates authorization to Google Drive permissions. It does not provide
row-level security. See [Permissions](docs/permissions/index.md) for details.

OAuth tokens are stored in platform-specific user data directories and are never
written to the Drive project folder.
