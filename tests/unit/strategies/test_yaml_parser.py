"""Tests for YAMLParser."""

import pytest
from src.strategies.parsers.yaml_parser import YAMLParseError, YAMLParser

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_VALID_YAML = """\
meta:
  name: vcp_daily
  product: DELIVERY
  direction: LONG
  timeframe: DAILY

universe_filter:
  min_price: 100
  min_adv_crores: 10
  exchanges:
    - NSE

entry_conditions:
  logic: AND
  conditions:
    - name: breakout
      type: PRICE
      condition: price_above
      reference: consolidation_high
      lookback_days: 21

stop_loss:
  method: recent_low
  lookback_days: 21
  buffer_pct: 0.5
  max_sl_pct: 8.0

target:
  method: risk_reward
  ratio: 2.5

exit_conditions: {}
"""

_PARSER = YAMLParser()


# ---------------------------------------------------------------------------
# Normal cases
# ---------------------------------------------------------------------------


def test_valid_yaml_string_returns_spec() -> None:
    spec = _PARSER.parse(_VALID_YAML)
    assert spec.meta.name == "vcp_daily"
    assert spec.source_format == "yaml"
    assert spec.stop_loss.lookback_days == 21
    assert spec.target.ratio == 2.5


def test_dict_input_returns_spec() -> None:
    import yaml

    data = yaml.safe_load(_VALID_YAML)
    spec = _PARSER.parse(data)
    assert spec.meta.name == "vcp_daily"
    assert spec.source_format == "yaml"


def test_universe_filter_parsed() -> None:
    spec = _PARSER.parse(_VALID_YAML)
    assert spec.universe_filter.min_price == 100.0
    assert spec.universe_filter.min_adv_crores == 10.0
    assert "NSE" in spec.universe_filter.exchanges


def test_entry_conditions_parsed() -> None:
    spec = _PARSER.parse(_VALID_YAML)
    assert len(spec.entry_conditions.conditions) == 1
    assert spec.entry_conditions.conditions[0].name == "breakout"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_missing_meta_name_raises() -> None:
    yaml_no_name = _VALID_YAML.replace("  name: vcp_daily\n", "")
    with pytest.raises(YAMLParseError, match="meta.name"):
        _PARSER.parse(yaml_no_name)


def test_missing_stop_loss_raises() -> None:
    yaml_no_sl = "\n".join(
        line for line in _VALID_YAML.splitlines() if not line.startswith("stop_loss")
    )
    with pytest.raises(YAMLParseError):
        _PARSER.parse(yaml_no_sl)


# ---------------------------------------------------------------------------
# Failure cases
# ---------------------------------------------------------------------------


def test_invalid_yaml_syntax_raises() -> None:
    with pytest.raises(YAMLParseError, match="Invalid YAML"):
        _PARSER.parse(":\tbad: [unclosed")


def test_yaml_root_not_mapping_raises() -> None:
    with pytest.raises(YAMLParseError, match="mapping"):
        _PARSER.parse("- item1\n- item2\n")
