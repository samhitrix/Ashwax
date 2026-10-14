"""global.json system.log_level + equity.screener.ohlcv_history_years are config-driven (Phase 2)."""

from __future__ import annotations

import json


def test_global_log_level_reads_config():
    from src.infrastructure.logger import _global_log_level

    # config ships system.log_level — reader returns it upper-cased, never None here
    level = _global_log_level()
    assert level is not None
    assert level == level.upper()


def test_log_level_env_var_no_longer_read(monkeypatch):
    """2026-09-23: LOG_LEVEL used to win over config/global.json system.log_level,
    so a deployed .env silently overrode the deliberate config knob and setting
    DEBUG in global.json did nothing unless .env happened to agree. Removed --
    config/global.json system.log_level is now the only control. Module-level
    LOG_LEVEL is computed once at import time, so this asserts the reader
    (_global_log_level) ignores the env var, not the cached constant."""
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    from src.infrastructure.logger import _global_log_level

    # _global_log_level() reads only config/global.json; it must resolve to the
    # config's value (DEBUG in this repo's config), never the env var above.
    assert _global_log_level() != "WARNING"


def test_ohlcv_history_years_prefers_global_screener():
    from src.market_data.ohlcv_downloader import _load_global_config

    screener = _load_global_config().get("equity", {}).get("screener", {})
    # the key the downloader uses as the history authority must exist in config
    assert "ohlcv_history_years" in screener
    assert int(screener["ohlcv_history_years"]) >= 1


def test_global_config_matches_disk():
    from src.market_data.ohlcv_downloader import _BASE, _load_global_config

    on_disk = json.loads((_BASE / "config" / "global.json").read_text(encoding="utf-8"))
    assert _load_global_config() == on_disk
