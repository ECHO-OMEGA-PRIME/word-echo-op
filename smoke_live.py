#!/usr/bin/env python3
"""Live HTTP contract smoke for Word Echo; response bodies are never printed."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

ASSET_SHA256 = "364263a47a7a44b12fadaf0f81b83d1a11813e46107e874523e4701468945a14"
REQUIRED_HEADERS = {
    "content-security-policy",
    "permissions-policy",
    "referrer-policy",
    "strict-transport-security",
    "x-content-type-options",
    "x-echo-origin",
    "x-frame-options",
    "x-request-id",
}


def request(base: str, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        method=method,
        headers={
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "X-Request-ID": "word-echo-live-smoke",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def require_headers(headers: dict[str, str], label: str) -> None:
    missing = sorted(REQUIRED_HEADERS - headers.keys())
    if missing:
        raise AssertionError(f"{label}: missing security headers {missing}")


def run(base: str, *, public_edge: bool, exercise_rate_limit: bool) -> int:
    checks = 0
    status, headers, body = request(base, "/health")
    assert status == 200, f"health status={status}"
    require_headers(headers, "health")
    payload = json.loads(body)
    assert payload.get("ok") is True and payload.get("service") == "word-echo-op", "health identity"
    assert headers.get("access-control-allow-origin") == "*", "health CORS"
    checks += 1

    cache_bust = f"?echo_smoke={time.time_ns()}" if public_edge else ""
    for path in ("/", "/index.html", "/deep/spa/link"):
        status, headers, body = request(base, f"{path}{cache_bust}")
        assert status == 200, f"{path} status={status}"
        require_headers(headers, path)
        assert headers.get("x-echo-origin") == "forge-private", f"{path} origin marker"
        if not public_edge:
            assert hashlib.sha256(body).hexdigest() == ASSET_SHA256, f"{path} asset hash"
        checks += 1

    status, headers, body = request(base, f"/{cache_bust}", "HEAD")
    assert status == 200 and body == b"", f"HEAD / status={status} body={len(body)}"
    require_headers(headers, "HEAD /")
    if not public_edge:
        assert int(headers["content-length"]) == 401921, "HEAD / content length"
    checks += 1

    status, headers, body = request(base, "/health", "HEAD")
    assert status == 200 and body == b"", f"HEAD /health status={status} body={len(body)}"
    require_headers(headers, "HEAD /health")
    checks += 1

    status, headers, _ = request(base, "/health", "OPTIONS")
    assert status == 204, f"OPTIONS /health status={status}"
    require_headers(headers, "OPTIONS /health")
    assert headers.get("access-control-allow-origin") == "*", "preflight CORS"
    checks += 1

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        status, headers, _ = request(base, "/health", method)
        assert status == 405, f"{method} /health status={status}"
        require_headers(headers, f"{method} /health")
        checks += 1

    for path in ("/%2e%2e/%2eenv", "/.git/config", "/%2500"):
        status, headers, _ = request(base, path)
        if public_edge:
            assert status in {400, 403, 404}, f"{path} edge status={status}"
        else:
            assert status == 404, f"{path} origin status={status}"
            require_headers(headers, path)
        checks += 1

    if exercise_rate_limit:
        saw_429 = False
        for index in range(40):
            status, headers, _ = request(base, f"/rate-smoke-{index}")
            if status == 429:
                require_headers(headers, "rate limit")
                assert int(headers.get("retry-after", "0")) > 0
                saw_429 = True
                break
        assert saw_429
        checks += 1

    print(json.dumps({"asset_sha256": ASSET_SHA256, "checks": checks, "ok": True}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--public-edge", action="store_true")
    parser.add_argument("--exercise-rate-limit", action="store_true")
    parser.add_argument("--force-fail", action="store_true")
    args = parser.parse_args()
    if args.force_fail:
        print("forced smoke failure", file=sys.stderr)
        return 9
    try:
        return run(args.base, public_edge=args.public_edge, exercise_rate_limit=args.exercise_rate_limit)
    except Exception as exc:
        print(f"smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
