"""Private-cluster replacement for the word-echo-op static Worker."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

SERVICE_NAME = "word-echo-op"
SERVICE_VERSION = "1.0.1"
EXPECTED_ASSET_SHA256 = "364263a47a7a44b12fadaf0f81b83d1a11813e46107e874523e4701468945a14"
ASSET_PATH = Path(
    os.environ.get("WORD_ECHO_ASSET_PATH", Path(__file__).parent / "public" / "index.html")
).resolve(strict=True)
ASSET_BYTES = ASSET_PATH.read_bytes()
ASSET_SHA256 = hashlib.sha256(ASSET_BYTES).hexdigest()
if ASSET_SHA256 != EXPECTED_ASSET_SHA256:
    raise RuntimeError("immutable static asset failed its provenance gate")

logger = logging.getLogger(SERVICE_NAME)
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self' data:; connect-src 'none'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'self'; form-action 'none'; "
        "media-src 'self'"
    ),
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "X-Echo-Origin": "forge-private",
}


class FixedWindowLimiter:
    """Small bounded limiter for the single-worker, read-only service."""

    def __init__(self) -> None:
        self.read_limit = int(os.environ.get("WORD_ECHO_READS_PER_MINUTE", "300"))
        self.preflight_limit = int(os.environ.get("WORD_ECHO_PREFLIGHTS_PER_MINUTE", "60"))
        self._counts: dict[tuple[str, str, int], int] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()

    def allow(self, client: str, bucket: str) -> tuple[bool, int]:
        window = int(time.monotonic() // 60)
        limit = self.preflight_limit if bucket == "preflight" else self.read_limit
        key = (client, bucket, window)
        with self._lock:
            if len(self._counts) > 4096:
                self._counts = {k: v for k, v in self._counts.items() if k[2] >= window - 1}
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count
        return count <= max(limit, 1), max(1, 60 - int(time.monotonic() % 60))


rate_limiter = FixedWindowLimiter()
app = FastAPI(
    title="Word Echo",
    version=SERVICE_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _safe_request_id(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "")
    return supplied if REQUEST_ID_RE.fullmatch(supplied) else uuid.uuid4().hex


def _client_key(request: Request) -> str:
    direct = request.client.host if request.client else "unknown"
    try:
        trusted_proxy = ipaddress.ip_address(direct).is_loopback
    except ValueError:
        trusted_proxy = False
    if trusted_proxy:
        forwarded = request.headers.get("cf-connecting-ip") or request.headers.get(
            "x-forwarded-for", ""
        ).split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return direct[:128]


def _path_is_safe(request: Request) -> bool:
    raw = request.scope.get("raw_path", b"")
    if len(raw) > 2048 or b"\x00" in raw:
        return False
    try:
        decoded = raw.decode("ascii", "strict")
    except UnicodeDecodeError:
        return False
    for _ in range(2):
        decoded = unquote(decoded)
    if "\x00" in decoded or "\\" in decoded:
        return False
    return not any(
        segment in {".", ".."} or segment.startswith(".")
        for segment in decoded.split("/")
        if segment
    )


def _apply_headers(response: Response, request_id: str) -> None:
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    response.headers["X-Request-ID"] = request_id


@app.middleware("http")
async def request_guard(request: Request, call_next):
    started = time.perf_counter()
    request_id = _safe_request_id(request)
    method = request.method.upper()
    normalized_path = request.url.path[:256]
    try:
        if not _path_is_safe(request):
            response = JSONResponse({"error": "not_found", "request_id": request_id}, status_code=404)
        elif method not in {"GET", "HEAD", "OPTIONS"}:
            response = JSONResponse(
                {"error": "method_not_allowed", "request_id": request_id},
                status_code=405,
                headers={"Allow": "GET, HEAD, OPTIONS"},
            )
        elif method == "OPTIONS" and request.url.path != "/health":
            response = JSONResponse(
                {"error": "method_not_allowed", "request_id": request_id},
                status_code=405,
                headers={"Allow": "GET, HEAD"},
            )
        else:
            allowed, retry_after = rate_limiter.allow(
                _client_key(request), "preflight" if method == "OPTIONS" else "read"
            )
            if not allowed:
                response = JSONResponse(
                    {"error": "rate_limited", "request_id": request_id},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
            else:
                response = await call_next(request)
    except Exception:
        logger.exception("unhandled request failure request_id=%s", request_id)
        response = JSONResponse(
            {"error": "internal_error", "request_id": request_id}, status_code=500
        )
    _apply_headers(response, request_id)
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    logger.info(
        json.dumps(
            {
                "dur_ms": duration_ms,
                "method": method,
                "path": normalized_path,
                "request_id": request_id,
                "status": response.status_code,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return response


def _health_bytes() -> bytes:
    return json.dumps(
        {
            "ok": True,
            "service": SERVICE_NAME,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "version": SERVICE_VERSION,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@app.api_route("/health", methods=["GET", "HEAD", "OPTIONS"])
async def health(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Headers": "Content-Type, X-Request-ID",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Max-Age": "600",
            },
        )
    payload = _health_bytes()
    headers = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}
    if request.method == "HEAD":
        headers["Content-Length"] = str(len(payload))
        return Response(status_code=200, media_type="application/json", headers=headers)
    return Response(content=payload, status_code=200, media_type="application/json", headers=headers)


@app.api_route("/{spa_path:path}", methods=["GET", "HEAD"])
async def static_spa(request: Request, spa_path: str) -> Response:
    headers = {"ETag": f'"sha256-{ASSET_SHA256}"'}
    if spa_path in {"", "index.html"}:
        headers["Cache-Control"] = "public, max-age=3600"
    if request.method == "HEAD":
        headers["Content-Length"] = str(len(ASSET_BYTES))
        return Response(status_code=200, media_type="text/html; charset=utf-8", headers=headers)
    return Response(
        content=ASSET_BYTES,
        status_code=200,
        media_type="text/html; charset=utf-8",
        headers=headers,
    )
