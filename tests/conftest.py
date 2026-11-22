"""Root pytest configuration.

Redirects logging to a temp directory BEFORE any test module gets a chance
to import src.orchestrator (whose module-level `setup_logging("orchestrator",
log_dir=str(_LOGS))` call — see src/orchestrator.py:86 — points unconditionally
at the real project logs/ directory and is guarded by a one-shot
`_setup_done` flag in src/infrastructure/logger.py). Without this, running
the test suite writes real log lines (e.g. "reason=test block" CRM fixtures,
duplicate OPT-1 position IDs) into the SAME logs/*.log files the live
launcher.py process writes to, making live diagnosis genuinely confusing —
found earlier while trying to read today's real options.log activity and
finding pytest fixture noise mixed into it.

conftest.py at the tests/ root is imported by pytest before any test file in
the suite, so this call wins the `_setup_done` race against orchestrator.py's
own module-level call.
"""

from __future__ import annotations

import tempfile

import pytest
from src.infrastructure.logger import setup_logging

_TEST_LOG_DIR = tempfile.mkdtemp(prefix="ashwax_test_logs_")
setup_logging("pytest", log_dir=_TEST_LOG_DIR)


@pytest.fixture(autouse=True)
def _reset_api_broker_cache():
    """Clear the API's short-lived broker cache around every test.

    src/api/server.py caches Kite reads for a few seconds and coalesces concurrent
    callers. Tests patch _kite() per case and run well inside those TTLs, so without
    this a test could be served the previous test's broker payload.
    """
    from src.api import server

    server._clear_broker_cache()
    yield
    server._clear_broker_cache()


@pytest.fixture(autouse=True)
def _qty_floor_off_by_default(monkeypatch):
    """min_qty_floor reads the operator's real config/capital.json, where it may be
    switched on. Keep it off in tests so sizing tests don't depend on that live
    switch; tests/unit/risk/test_qty_floor.py exercises it explicitly."""
    from src.risk import qty_floor

    monkeypatch.setattr(qty_floor, "_PATH", qty_floor._PATH.with_name("__no_such_capital__.json"))


@pytest.fixture(autouse=True)
def _structure_shadow_output_off_the_real_file(monkeypatch, tmp_path):
    """structure_shadow is enabled in the real options_settings.json, so any test that drives
    SellerDesk.run_cycle would append synthetic rows to the forward-trial research file
    (data/research/options_structure_shadow.jsonl) and contaminate hypothesis R. Redirect it."""
    from src.options import structure_shadow

    monkeypatch.setattr(structure_shadow, "_OUT", tmp_path / "structure_shadow_test.jsonl")
    structure_shadow._DONE.clear()
    structure_shadow._SKIP_LOGGED.clear()
