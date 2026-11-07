# Contributing to Shakedown

## Requirements

- Python 3.11 or later.
- No third-party packages. Shakedown uses only the standard library.
- Do not add a runtime dependency without a discussed reason. Test helpers must also stay in the standard library.

## Run the tests

```bash
python -m unittest discover -s tests -v
```

The tests need no network and no API key. HTTP decider tests use a loopback fixture server only. Keep it that way: a test that contacts a real provider is a bug.

## Project rules

- **Determinism first.** Seeded inputs must reproduce identical outputs. If you add a random draw, it must come from a seeded generator and be replayable.
- **Evidence is never overwritten.** Commands write receipts and artifacts to new locations. A new fact gets a new file or an explicit receipt update, like `proposal.json`'s `applied` flag.
- **Approval is explicit.** Any command that mutates a project's files or spends a budget needs an operator flag such as `--approved`, not a default.
- **Secrets stay out.** Never write API keys, tokens, or provider response bodies to configuration, logs, tapes, receipts, or exception text. Tests must never contain real credentials.
- **Claims stay honest.** Distinguish implementation from measured evidence. `unmeasured` is not a pass. If a doc claims a result, the claim must name the run that produced it.

## Documentation

- The English documents under `docs/` are the source. When you change one, update the Korean file with the same name under `docs/ko/` in the same change, or mark it out of date at the top.
- `README.ko.md` mirrors `README.md` under the same rule.

## Game adapters

An adapter is supported when `shakedown check-adapter` reports C1–C3 passing against it. See `docs/03-adapter-protocol.md` for the contract and `skill/shakedown/` for guides and transport skeletons. The bundled `examples/tinyraid` and `examples/switchyard` are the reference adapters.

## Commits and pull requests

- Say what changed and why. Name the commands that verified it.
- Keep a pull request to one concern. Include the test run output for code changes.
- Fork the repository and open a pull request against `main`. Direct pushes to `main` are for maintainers.
