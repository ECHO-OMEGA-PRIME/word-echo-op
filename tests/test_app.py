from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

import app as word_app

client = TestClient(word_app.app)
SECURITY_HEADERS = {
    "content-security-policy",
    "permissions-policy",
    "referrer-policy",
    "strict-transport-security",
    "x-content-type-options",
    "x-echo-origin",
    "x-frame-options",
    "x-request-id",
}


def assert_hardened(response) -> None:
    assert SECURITY_HEADERS <= set(response.headers)
    assert response.headers["x-echo-origin"] == "forge-private"


def setup_function() -> None:
    word_app.rate_limiter.reset()
    word_app.rate_limiter.read_limit = 300
    word_app.rate_limiter.preflight_limit = 60


def test_exact_asset_and_spa_contract() -> None:
    for path in ("/", "/index.html", "/any/spa/path"):
        response = client.get(path)
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == word_app.EXPECTED_ASSET_SHA256
        assert_hardened(response)
    assert client.get("/").headers["cache-control"] == "public, max-age=3600"
    assert "cache-control" not in client.get("/any/spa/path").headers


def test_health_head_and_preflight_contract() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "word-echo-op"
    assert response.headers["access-control-allow-origin"] == "*"
    assert_hardened(response)
    head = client.head("/")
    assert head.status_code == 200 and head.content == b""
    assert int(head.headers["content-length"]) == 401921
    assert_hardened(head)
    preflight = client.options("/health")
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in preflight.headers
    assert_hardened(preflight)


def test_mutations_and_unsafe_paths_fail_closed() -> None:
    for method in ("post", "put", "patch", "delete"):
        response = client.request(method.upper(), "/health", content=b"never-read")
        assert response.status_code == 405
        assert_hardened(response)
    for path in ("/%2e%2e/%2eenv", "/.git/config", "/%2500"):
        response = client.get(path)
        assert response.status_code == 404
        assert_hardened(response)


def test_rate_limit_is_bounded_and_hardened() -> None:
    word_app.rate_limiter.read_limit = 2
    assert client.get("/rate-a").status_code == 200
    assert client.get("/rate-b").status_code == 200
    response = client.get("/rate-c")
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    assert_hardened(response)
