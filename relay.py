#!/usr/bin/env python3
"""CURI's local OpenAI-compatible relay.

The relay retries transport errors and temporary upstream failures before any
real SSE output is committed to the client. It records metadata-only JSONL
events for the CURI dashboard; request and response bodies are never logged.
"""
from __future__ import annotations

import json
import os
import random
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


RETRY_STATUS = {408, 425, 429} | set(range(500, 600))
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailer", "transfer-encoding", "upgrade", "host"}
STRUCTURAL_EVENTS = {"response.created", "response.queued", "response.in_progress",
                     "response.metadata", "response.output_item.added", "response.content_part.added"}
TERMINAL_EVENTS = {"response.completed", "response.failed", "error"}
MAX_BUFFERED_SSE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class RelayConfig:
    upstream: str
    host: str = "127.0.0.1"
    port: int = 8080
    max_retries: int = 3
    backoff_seconds: float = 0.5
    request_timeout: float = 120.0
    event_path: str = os.path.expanduser("~/.curi/relay-events.jsonl")
    buffer_until_success: bool = False


class EventWriter:
    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        self.lock = threading.Lock()

    def write(self, event: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        payload = {"schema_version": 1, "timestamp": iso_now(), **event}
        with self.lock, open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_upstream_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            return "<invalid upstream URL>"
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host if parsed.port is None else f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return "<invalid upstream URL>"


def upstream_url(base: str, path: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/v1") and path == "/v1":
        return base
    if base.endswith("/v1") and path.startswith("/v1/"):
        path = path[3:]
    return base + (path if path.startswith("/") else "/" + path)


def requested_model(body: bytes) -> str:
    try:
        value = json.loads(body).get("model", "")
        return str(value)[:120] if value is not None else ""
    except (TypeError, ValueError, AttributeError):
        return ""


def retry_delay(config: RelayConfig, attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(60.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    ceiling = min(60.0, max(0.0, config.backoff_seconds) * (2 ** attempt))
    return ceiling / 2 + random.random() * ceiling / 2 if ceiling else 0.0


def classify_error(value: Any) -> str | None:
    text = str(value or "").lower()
    if not text:
        return None
    if "capacity" in text or "overloaded" in text or "server_unavailable" in text:
        return "capacity"
    if "usage_limit" in text or "usage limit" in text or "limit reached" in text:
        return "usage_limit"
    if "rate limit" in text or "429" in text:
        return "rate_limit"
    return "error"


def retryable_error(value: Any) -> bool:
    return classify_error(value) in {"capacity", "usage_limit", "rate_limit"}


def frame_parts(frame: bytes) -> tuple[str, str]:
    event = ""
    data: list[str] = []
    for line in frame.decode("utf-8", errors="replace").splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    raw = "\n".join(data)
    if not event and raw:
        try:
            event = str(json.loads(raw).get("type", ""))
        except (ValueError, AttributeError):
            pass
    return event, raw


def response_model(raw: str) -> str:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(value, dict):
        return ""
    response = value.get("response") if isinstance(value.get("response"), dict) else {}
    return str(response.get("model") or value.get("model") or "")[:120]


def frame_commits_output(event: str, raw: str) -> bool:
    if event in STRUCTURAL_EVENTS or event in {"", "response.failed", "error"}:
        return False
    if event == "response.completed":
        return False
    return True


class PreCommitStreamError(Exception):
    def __init__(self, reason: str, error_class: str | None = None):
        super().__init__(reason)
        self.error_class = error_class


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    config: RelayConfig
    events: EventWriter

    def send_response(self, code: int, message: str | None = None) -> None:
        self.log_request(code)
        self.send_response_only(code, message)

    def do_GET(self) -> None:
        if self.path in ("/healthz", "/readyz"):
            self.send_bytes(200, b'{"status":"ok"}', "application/json")
        else:
            self.proxy_request()

    def do_POST(self) -> None: self.proxy_request()
    def do_HEAD(self) -> None: self.proxy_request()
    def do_OPTIONS(self) -> None: self.proxy_request()
    def do_PUT(self) -> None: self.proxy_request()
    def do_PATCH(self) -> None: self.proxy_request()
    def do_DELETE(self) -> None: self.proxy_request()

    def send_bytes(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_headers(self, response: Any, streaming: bool = False) -> None:
        self.send_response(getattr(response, "status", 200))
        for key, value in response.headers.items():
            lowered = key.lower()
            if lowered in HOP_BY_HOP or (streaming and lowered == "content-length"):
                continue
            self.send_header(key, value)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def proxy_request(self) -> None:
        request_id = "req-" + uuid.uuid4().hex[:12]
        started = time.monotonic()
        if not self.path.startswith("/v1"):
            self.send_bytes(404, b'{"error":"path must start with /v1"}', "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else b""
        model = requested_model(body)
        target = upstream_url(self.config.upstream, self.path)
        headers = {key: value for key, value in self.headers.items() if key.lower() not in HOP_BY_HOP}
        final: dict[str, Any] = {"request_id": request_id, "requested_model": model,
                                 "reported_model": None, "status": None, "error_class": None,
                                 "attempts": 0, "first_byte_ms": None, "duration_ms": None,
                                 "stream_terminal": None}
        for attempt in range(self.config.max_retries + 1):
            final["attempts"] = attempt + 1
            req = Request(target, data=body if "Content-Length" in self.headers else None,
                          headers=headers, method=self.command)
            try:
                with urlopen(req, timeout=self.config.request_timeout, context=ssl.create_default_context()) as response:
                    content_type = (response.headers.get("Content-Type") or "").lower()
                    if "text/event-stream" in content_type and self.command != "HEAD":
                        self.forward_sse(response, final, started)
                    else:
                        data = response.read()
                        final["reported_model"] = response_model(data.decode("utf-8", errors="replace")) or None
                        self.send_headers(response)
                        if self.command != "HEAD":
                            self.wfile.write(data)
                    final["status"] = getattr(response, "status", 200)
                    if final["stream_terminal"] in (None, "response.completed"):
                        final["error_class"] = None
                    final["duration_ms"] = round((time.monotonic() - started) * 1000)
                    self.events.write(final)
                    return
            except HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read(2048).decode("utf-8", errors="replace")
                except Exception:
                    pass
                final["status"] = exc.code
                final["error_class"] = classify_error(detail) or ("rate_limit" if exc.code == 429 else "error")
                if exc.code in RETRY_STATUS and attempt < self.config.max_retries:
                    time.sleep(retry_delay(self.config, attempt, exc.headers.get("Retry-After") if exc.headers else None))
                    continue
                payload = detail.encode("utf-8") or json.dumps({"error": "upstream request failed", "status": exc.code}).encode()
                self.send_bytes(exc.code, payload, "application/json")
                break
            except PreCommitStreamError as exc:
                final["error_class"] = exc.error_class or "error"
                if attempt < self.config.max_retries:
                    time.sleep(retry_delay(self.config, attempt))
                    continue
                self.send_bytes(502, json.dumps({"error": "upstream stream failed", "detail": str(exc)}).encode(), "application/json")
                final["status"] = 502
                break
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                final["error_class"] = classify_error(exc) or "transport"
                if attempt < self.config.max_retries:
                    time.sleep(retry_delay(self.config, attempt))
                    continue
                self.send_bytes(502, json.dumps({"error": "upstream unavailable"}).encode(), "application/json")
                final["status"] = 502
                break
        final["duration_ms"] = round((time.monotonic() - started) * 1000)
        self.events.write(final)

    def forward_sse(self, response: Any, final: dict[str, Any], started: float) -> None:
        pending: list[bytes] = []
        buffer = b""
        committed = False
        terminal = ""
        first_byte_at: float | None = None
        while True:
            try:
                chunk = response.read(64 * 1024)
            except (OSError, TimeoutError, URLError) as exc:
                if not committed:
                    raise PreCommitStreamError(str(exc), classify_error(exc))
                final["stream_terminal"] = "incomplete"
                return
            if not chunk:
                break
            if first_byte_at is None:
                first_byte_at = time.monotonic()
                final["first_byte_ms"] = round((first_byte_at - started) * 1000)
            buffer += chunk.replace(b"\r\n", b"\n")
            while b"\n\n" in buffer:
                frame, buffer = buffer.split(b"\n\n", 1)
                wire = frame + b"\n\n"
                event, raw = frame_parts(wire)
                reported = response_model(raw)
                if reported:
                    final["reported_model"] = reported
                pending.append(wire)
                if self.config.buffer_until_success and sum(len(item) for item in pending) > MAX_BUFFERED_SSE_BYTES:
                    raise PreCommitStreamError("buffered SSE response exceeded 64 MiB")
                if event in TERMINAL_EVENTS:
                    terminal = event
                    final["error_class"] = classify_error(raw) if event != "response.completed" else final["error_class"]
                    if event != "response.completed" and not committed and retryable_error(raw):
                        raise PreCommitStreamError(raw[:200], final["error_class"])
                ready_to_commit = event == "response.completed" or (event in TERMINAL_EVENTS and not retryable_error(raw))
                if not self.config.buffer_until_success:
                    ready_to_commit = ready_to_commit or frame_commits_output(event, raw)
                if not committed and ready_to_commit:
                    self.send_headers(response, streaming=True)
                    committed = True
                    for item in pending:
                        self.wfile.write(item)
                    self.wfile.flush()
                    pending.clear()
                elif committed:
                    self.wfile.write(wire)
                    self.wfile.flush()
        if buffer:
            if not committed:
                raise PreCommitStreamError("upstream ended with an incomplete SSE frame")
        if not terminal:
            if not committed:
                raise PreCommitStreamError("upstream closed before a terminal SSE event")
            final["stream_terminal"] = "incomplete"
        else:
            final["stream_terminal"] = terminal
        if not committed:
            self.send_headers(response, streaming=True)
            for item in pending:
                self.wfile.write(item)
            self.wfile.flush()

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def create_server(config: RelayConfig) -> ThreadingHTTPServer:
    if not config.upstream:
        raise ValueError("an upstream URL is required")
    parsed = urlsplit(config.upstream)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("upstream must be an http or https URL")
    if config.max_retries < 0 or config.request_timeout <= 0 or config.backoff_seconds < 0:
        raise ValueError("retry and timeout settings must be non-negative; timeout must be positive")
    writer = EventWriter(config.event_path)
    handler = type("ConfiguredRelayHandler", (RelayHandler,), {"config": config, "events": writer})
    server = ThreadingHTTPServer((config.host, config.port), handler)
    return server


def serve(config: RelayConfig) -> None:
    server = create_server(config)
    print(f"CURI relay listening at http://{config.host}:{server.server_port}/v1 -> {safe_upstream_url(config.upstream)}")
    print(f"relay events: {os.path.abspath(os.path.expanduser(config.event_path))}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    serve(RelayConfig(upstream=os.environ.get("UPSTREAM_BASE_URL", "")))
