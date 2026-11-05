"""The unauthenticated control API must only answer localhost Host headers (DNS rebinding)."""

from __future__ import annotations

from src.api.server import app
from starlette.testclient import TestClient


def test_foreign_host_rejected() -> None:
    client = TestClient(app, base_url="http://evil.example")
    assert client.get("/__no_such_route").status_code == 400


def test_localhost_hosts_pass_the_guard() -> None:
    for base in ("http://localhost:3000", "http://127.0.0.1:3000"):
        # 404 = reached routing, i.e. the host guard let it through.
        assert TestClient(app, base_url=base).get("/__no_such_route").status_code == 404
