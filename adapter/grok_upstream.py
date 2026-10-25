"""Read the Grok CLI session and forward OpenAI-style calls to cli-chat-proxy."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.client import HTTPSConnection
from pathlib import Path
AUTH_PATH = Path.home() / ".grok" / "auth.json"
PROXY_HOST = "cli-chat-proxy.grok.com"
_VERSION_LOCK = threading.Lock()
_VERSION_CACHE: tuple[float, str] | None = None


class UpstreamError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _parse_expiry(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    # auth.json uses nanosecond fractions; datetime accepts microseconds.
    if "." in text:
        head, tail = text.split(".", 1)
        digits = []
        rest = []
        for index, char in enumerate(tail):
            if char.isdigit() and not rest:
                digits.append(char)
            else:
                rest.append(tail[index:])
                break
        frac = "".join(digits)[:6].ljust(6, "0")
        text = head + "." + frac + (rest[0] if rest else "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_record() -> dict:
    try:
        payload = json.loads(AUTH_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise UpstreamError(401, f"could not read {AUTH_PATH}: {exc}") from exc
    records = [item for item in payload.values() if isinstance(item, dict) and item.get("key")]
    if not records:
        raise UpstreamError(401, "no Grok session token in ~/.grok/auth.json; run grok login")
    records.sort(key=lambda item: _parse_expiry(item.get("expires_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return records[-1]


def session_token() -> str:
    record = load_record()
    expires = _parse_expiry(record.get("expires_at"))
    if expires is not None and (expires - datetime.now(timezone.utc)).total_seconds() < 120:
        subprocess.run(
            ["grok", "models", "--no-auto-update"],
            check=False,
            capture_output=True,
            timeout=60,
        )
        record = load_record()
    token = record.get("key")
    if not isinstance(token, str) or not token:
        raise UpstreamError(401, "Grok session token is empty; run grok login")
    return token


def client_version() -> str:
    global _VERSION_CACHE
    now = time.monotonic()
    with _VERSION_LOCK:
        if _VERSION_CACHE and now - _VERSION_CACHE[0] < 300:
            return _VERSION_CACHE[1]
        try:
            output = subprocess.check_output(["grok", "--version"], text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UpstreamError(500, f"could not read grok version: {exc}") from exc
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        version = match.group(1) if match else "1.0.70"
        _VERSION_CACHE = (now, version)
        return version


def forward(path: str, body: bytes, timeout: float = 300) -> tuple[int, str, bytes]:
    """POST body to the subscription proxy and return status, content-type, and body."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamError(400, f"request body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpstreamError(400, "request body must be a JSON object")
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        model = "grok-4.7"
    # LiteLLM names the upstream model openai/<id>. The proxy routes on the header.
    if model.startswith("openai/"):
        model = model.split("/", 1)[1]
    payload["model"] = model
    encoded = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + session_token(),
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-model-override": model,
        "x-grok-client-version": client_version(),
        "x-grok-client-identifier": "grok-cli",
        "Accept": "text/event-stream" if payload.get("stream") else "application/json",
    }
    connection = HTTPSConnection(PROXY_HOST, timeout=timeout)
    try:
        connection.request("POST", path, body=encoded, headers=headers)
        response = connection.getresponse()
        data = response.read()
        content_type = response.getheader("Content-Type") or "application/json"
        return response.status, content_type, data
    except TimeoutError as exc:
        raise UpstreamError(504, f"cli-chat-proxy timed out: {exc}") from exc
    except OSError as exc:
        raise UpstreamError(502, f"cli-chat-proxy connection failed: {exc}") from exc
    finally:
        connection.close()


def stream_forward(path: str, body: bytes, timeout: float = 300):
    """Yield (status, content_type, iterator-of-bytes). Caller closes the connection."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamError(400, f"request body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpstreamError(400, "request body must be a JSON object")
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        model = "grok-4.7"
    if model.startswith("openai/"):
        model = model.split("/", 1)[1]
    payload["model"] = model
    payload["stream"] = True
    encoded = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + session_token(),
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-model-override": model,
        "x-grok-client-version": client_version(),
        "x-grok-client-identifier": "grok-cli",
        "Accept": "text/event-stream",
    }
    connection = HTTPSConnection(PROXY_HOST, timeout=timeout)
    try:
        connection.request("POST", path, body=encoded, headers=headers)
        response = connection.getresponse()
    except (TimeoutError, OSError) as exc:
        connection.close()
        raise UpstreamError(502, f"cli-chat-proxy connection failed: {exc}") from exc
    content_type = response.getheader("Content-Type") or "text/event-stream"

    def chunks():
        try:
            while True:
                piece = response.read(8192)
                if not piece:
                    break
                yield piece
        finally:
            connection.close()

    return response.status, content_type, chunks()


