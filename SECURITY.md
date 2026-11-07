# Security policy

## Supported versions

Shakedown is at 0.x and under active development. Security fixes go to the latest release on `main`. Earlier versions are not maintained.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: open a security advisory at <https://github.com/kkgzsprov/shakedown/security/advisories/new>. Do not open a public issue for an unpatched vulnerability.

Include the affected version or commit, a reproduction path, and the impact you see. You will get an acknowledgment and a decision on a fix or a disclosure timeline.

## Scope notes

- Shakedown reads a hosted API key from a configured environment variable or `api_key_file` and removes it from the game process environment. If you find a path that writes a key or a provider response body to a log, tape, receipt, or error message, report it as a vulnerability.
- The `jev` hosted backend sends the game's state and candidate descriptions to the configured endpoint. Configuration of an untrusted endpoint is an operator decision, not a vulnerability. Plain HTTP is accepted only for loopback test fixtures.
- Issues in the example games under `examples/` that cannot affect an adapter's own threat model are regular bugs, not vulnerabilities.
