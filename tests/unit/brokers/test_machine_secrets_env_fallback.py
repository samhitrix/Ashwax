"""machine_secrets falls back to environment variables where DPAPI is unavailable."""

import pytest

from src.brokers import machine_secrets as ms


@pytest.fixture
def no_dpapi(monkeypatch):
    monkeypatch.setattr(ms, "win32crypt", None)


def test_env_name_maps_non_alphanumerics():
    assert ms._env_name("kite_login:AB1234") == "ASHWAX_SECRET_KITE_LOGIN_AB1234"


def test_get_reads_environment_when_dpapi_missing(no_dpapi, monkeypatch):
    monkeypatch.setenv("ASHWAX_SECRET_KITE_TOTP_AB1234", "JBSWY3DPEHPK3PXP")
    assert ms.get_machine_secret("kite_totp:AB1234") == "JBSWY3DPEHPK3PXP"


def test_get_returns_none_when_env_unset_or_empty(no_dpapi, monkeypatch):
    monkeypatch.delenv("ASHWAX_SECRET_KITE_LOGIN_ZZ9", raising=False)
    assert ms.get_machine_secret("kite_login:ZZ9") is None
    monkeypatch.setenv("ASHWAX_SECRET_KITE_LOGIN_ZZ9", "")
    assert ms.get_machine_secret("kite_login:ZZ9") is None


def test_set_is_refused_without_dpapi(no_dpapi):
    with pytest.raises(RuntimeError, match="ASHWAX_SECRET_KITE_LOGIN_AB1234"):
        ms.set_machine_secret("kite_login:AB1234", "pw")
