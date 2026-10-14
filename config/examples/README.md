# Paper-only example configs

The files in `config/` are the operator's working configuration and may enable real-money
trading (swing segment `paper_mode: false`, a strategy with `execution_mode: "live"`).

These copies are identical except that everything is forced to **paper**:

| File | Change vs `config/` |
|---|---|
| `global.json` | `system.segments.swing.paper_mode` -> `true` |
| `operator.json` | `account.paper_mode_locked` -> `true` (locks the whole account to paper) |
| `strategies_registry.json` | every strategy `execution_mode` -> `"paper"` |

To start from a safe baseline (e.g. a fresh clone):

```bash
cp config/examples/*.json config/
```

Going live is a deliberate, manual edit of those three keys after weeks of paper results.
`tests/unit/test_example_configs.py` fails if any example ever enables real money.
