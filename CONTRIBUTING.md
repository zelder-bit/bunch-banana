# Contributing

Thanks for helping with bunch. The code is small and tested; please keep both true.

## Setup

```bash
cd bunch
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
```

## Tests

```bash
pytest -q
```

Coverage:

```bash
pytest -q --cov=bunch --cov-report=term
```

## Lint and types

```bash
ruff check bunch clients
mypy bunch/bunch
```

Both are also run in CI (`.github/workflows/ci.yml`). The lint rule set is small on
purpose (`ruff.toml`); keep it that way rather than adding rules that churn existing code.

## Before you commit

Nothing private may ever land in the repo (hostnames, IPs, keys, tokens). Run:

```bash
scripts/check-public.sh --all
```

It runs in CI too. Install it as a pre-commit hook once:

```bash
ln -sf ../../scripts/check-public.sh .git/hooks/pre-commit
```

## Style

- Match the surrounding code: type hints everywhere, long lines are fine (the formatter is
  not part of this project), plain text over JSON for agent output.
- Tests live under `bunch/tests/`; add one for any behaviour change.
