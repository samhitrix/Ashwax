"""Config loader tests: must degrade to safe defaults on any error, never raise."""

from __future__ import annotations

import json

import src.notifications.config as cfgmod


def test_missing_file_falls_back_to_defaults(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cfgmod, "_CFG_PATH", tmp_path / "does_not_exist.json")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    cfgmod.reset_config_cache()
    cfg = cfgmod.load_notification_config()
    assert cfg.enabled is True
    assert cfg.credentials_present is False
    cfgmod.reset_config_cache()


def test_malformed_json_falls_back_to_defaults_without_raising(monkeypatch, tmp_path) -> None:
    bad = tmp_path / "notifications.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(cfgmod, "_CFG_PATH", bad)
    cfgmod.reset_config_cache()
    cfg = cfgmod.load_notification_config()  # must not raise
    assert cfg.enabled is True
    cfgmod.reset_config_cache()


def test_credentials_present_requires_both(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cfgmod, "_CFG_PATH", tmp_path / "missing.json")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    cfgmod.reset_config_cache()
    cfg = cfgmod.load_notification_config()
    assert cfg.credentials_present is False
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    cfgmod.reset_config_cache()
    cfg2 = cfgmod.load_notification_config()
    assert cfg2.credentials_present is True
    cfgmod.reset_config_cache()


def test_unknown_keys_in_subsections_are_ignored(monkeypatch, tmp_path) -> None:
    path = tmp_path / "notifications.json"
    path.write_text(
        json.dumps({"throttle": {"modify_min_r_moved": 0.5, "some_future_key": 123}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfgmod, "_CFG_PATH", path)
    cfgmod.reset_config_cache()
    cfg = cfgmod.load_notification_config()  # must not raise TypeError
    assert cfg.throttle.modify_min_r_moved == 0.5
    cfgmod.reset_config_cache()


def test_event_enabled_defaults_true_for_unknown_key() -> None:
    from src.notifications.config import NotificationConfig

    cfg = NotificationConfig(events={"entry_fill": False})
    assert cfg.event_enabled("entry_fill") is False
    assert cfg.event_enabled("some_new_event_type") is True


def test_mode_enabled_respects_include_flags() -> None:
    from src.notifications.config import NotificationConfig

    cfg = NotificationConfig(include_paper=False, include_live=True)
    assert cfg.mode_enabled(is_paper=True) is False
    assert cfg.mode_enabled(is_paper=False) is True
