# Contributing to AshwaX

Contributions are very welcome — bug fixes, tests, docs, broker adapters, dashboard polish,
and well-evidenced strategy ideas. Open an issue first for anything bigger than a small fix so
we can agree on the approach before you spend time on it.

## Ground rules (this is a trading system)

1. **Risk controls are not optional.** Changes must not bypass the Central Risk Manager
   (`src/risk/`), weaken a stop, raise a limit by default, or add behaviour that acts without
   being logged.
2. **Paper stays the default.** Never commit a config that turns on live trading
   (`paper_mode: false`, `execution_mode: "live"`, `paper_mode_locked: false`).
   `tests/unit/test_example_configs.py` checks the examples.
3. **No strategy without evidence.** A new or changed strategy needs a written hypothesis, a
   backtest at `--cost-pct 0.10` with out-of-sample results, and clear pass / fail and
   rollback criteria. It ships disabled and paper-only. Please don't tune parameters to fit
   recent history.
4. **Fail closed.** If an input is missing (data, LLM verdict, broker response), skip the
   trade rather than guess.
5. **No secrets or personal data** in code, configs, tests, logs or screenshots.

## Getting set up

Follow [INSTALL.md](INSTALL.md), then:

```bash
pip install -e ".[dev]"
pre-commit install        # ruff, ruff-format, mypy, secret and large-file checks
pytest -q
```

Python 3.13+, line length 100, type hints on public functions, Google-style docstrings.

## Making a change

- Keep pull requests small and focused; every changed line should trace to the PR's purpose.
- Add tests for real logic changes (normal case, plus an edge or failure case where it
  matters). Tests live in `tests/` and mirror the source layout.
- Update the README / INSTALL when setup, dependencies or user-visible behaviour changes.
- New strategies must be listed in `config/strategies_registry.json` so they can be toggled
  from the dashboard.
- Describe in the PR what you changed, why, and how you tested it.

## Licensing of contributions

AshwaX is under the [PolyForm Noncommercial License 1.0.0](LICENSE). By submitting a
contribution you agree that it is licensed under the same terms, and that the project owner
may also offer the combined work, including your contribution, under other licenses (for
example a separate commercial license). You keep the copyright in what you wrote.

## Reporting security issues

Please don't open a public issue for a vulnerability or a way to bypass risk controls.
Contact the maintainer privately through GitHub.

## Conduct

Be respectful and constructive. Assume good intent, critique code and not people.
