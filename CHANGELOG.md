# Changelog

All notable changes to this project are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.0.1] — Unreleased

### Added

- M0–M7 implementation paths: wire protocol and bridge, `random`/`mock`/`tape` offline deciders, two example games (tinyraid, switchyard), input recording and replay, isolated batch runs, aggregation, C1–C7 adapter conformance, QA explore/anomalies/triage/clips, the tinyraid fault-injection benchmark, planner drafts with operator approval and budget receipts, holdout-gated balance proposals, the agent skill, and the optional pinned Jev HTTP backend.
- `shakedown apply`: re-verify a reviewed defaults proposal against the live tuning key table and write it under `--approved`.
- `decider.sampling` (`argmax` default, `seeded`) for seeded draws from Jev response probabilities.
- `decider.api_key_file` for reading the hosted API key from a file relative to the configuration file; it takes precedence over the environment variable.
- Community files: CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md, issue and pull request templates.
- PyPI packaging metadata and a tag-driven release workflow.
- macOS added to the CI test matrix.
