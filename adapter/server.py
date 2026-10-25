"""OpenAI-compatible development backend for the local LiteLLM proxy.

Chat and responses go to the Grok subscription proxy. Image routes shell out
to `grok`. Speech returns a short beep. Transcription returns a fixed stub.
Bind this to localhost only.
"""

from __future__ import annotations

import base64
import hmac
import json
import mimetypes
import os
import subprocess
import threading
import time
import uuid
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from adapter.grok_upstream import UpstreamError, forward, stream_forward
from adapter.images import ImageError, generate_image

ROOT = Path(__file__).resolve().parent.parent
MEDIA_DIR = ROOT / "media"
WORK_DIR = ROOT / "var" / "jobs"
BEEP_PATH = ROOT / "var" / "beep.mp3"
PUBLIC_BASE = os.environ.get("ADAPTER_PUBLIC_BASE", "http://127.0.0.1:4010").rstrip("/")
ADAPTER_API_KEY = os.environ.get("ADAPTER_API_KEY", "")
IMAGE_LOCK = threading.Lock()


def ensure_beep() -> None:
    BEEP_PATH.parent.mkdir(parents=True, exist_ok=True)
    if BEEP_PATH.is_file() and BEEP_PATH.stat().st_size > 0:
        return
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.4",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-q:a",
            "9",
            str(BEEP_PATH),
        ],
        check=True,
        capture_output=True,
    )


def _authorized(header: str | None) -> bool:
    if not ADAPTER_API_KEY:
        return False
    if not header or not header.lower().startswith("bearer "):
        return False
    presented = header.split(" ", 1)[1].strip()
    return hmac.compare_digest(presented, ADAPTER_API_KEY)


def _error(status: int, message: str) -> bytes:
    return json.dumps({"error": {"message": message, "type": "adapter_error"}}).encode()


def _store_media(source: Path) -> tuple[str, bytes]:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower() if source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else ".png"
    name = uuid.uuid4().hex + suffix
    destination = MEDIA_DIR / name
    destination.write_bytes(source.read_bytes())
    return name, destination.read_bytes()


def _image_response(source: Path) -> bytes:
    name, raw = _store_media(source)
    body = {
        "created": int(time.time()),
        "data": [
            {
                "url": f"{PUBLIC_BASE}/v1/media/{name}",
                "b64_json": base64.b64encode(raw).decode("ascii"),
            }
        ],
    }
    return json.dumps(body).encode()


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], list[tuple[str, str, bytes]]]:
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = BytesParser(policy=HTTP).parsebytes(header + body)
    fields: dict[str, str] = {}
    files: list[tuple[str, str, bytes]] = []
    if not message.is_multipart():
        return fields, files
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if "form-data" not in disposition:
            continue
        name = part.get_param("name", header="content-disposition")
        filename = part.get_param("filename", header="content-disposition")
        payload = part.get_payload(decode=True) or b""
        if not isinstance(name, str):
            continue
        if filename:
            files.append((name, filename, payload))
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or "0")
        if length < 0 or length > 32 * 1024 * 1024:
            raise ValueError("body is too large")
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self) -> bool:
        if _authorized(self.headers.get("Authorization")):
            return True
        self._send(401, _error(401, "invalid adapter key"), "application/json")
        return False

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, b'{"status":"ok"}', "application/json")
            return
        if path.startswith("/v1/media/"):
            name = unquote(path.removeprefix("/v1/media/"))
            if "/" in name or name.startswith("."):
                self._send(404, _error(404, "not found"), "application/json")
                return
            target = (MEDIA_DIR / name).resolve()
            if MEDIA_DIR.resolve() not in target.parents or not target.is_file():
                self._send(404, _error(404, "not found"), "application/json")
                return
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), content_type)
            return
        self._send(404, _error(404, "not found"), "application/json")

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        try:
            body = self._read_body()
        except ValueError as exc:
            self._send(400, _error(400, str(exc)), "application/json")
            return
        try:
            if path in ("/v1/chat/completions", "/v1/responses"):
                self._proxy(path, body)
                return
            if path == "/v1/images/generations":
                self._images(body, edit=False)
                return
            if path == "/v1/images/edits":
                self._images(body, edit=True)
                return
            if path == "/v1/audio/speech":
                self._speech()
                return
            if path == "/v1/audio/transcriptions":
                self._transcription(body)
                return
        except UpstreamError as exc:
            self._send(exc.status, _error(exc.status, exc.message), "application/json")
            return
        except ImageError as exc:
            self._send(502, _error(502, exc.message), "application/json")
            return
        self._send(404, _error(404, "not found"), "application/json")

    def _proxy(self, path: str, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        streaming = isinstance(payload, dict) and payload.get("stream") is True
        if not streaming:
            status, content_type, data = forward(path, body)
            self._send(status, data, content_type.split(";")[0] or "application/json")
            return
        status, content_type, chunks = stream_forward(path, body)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for piece in chunks:
            self.wfile.write(f"{len(piece):X}\r\n".encode("ascii"))
            self.wfile.write(piece)
            self.wfile.write(b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def _images(self, body: bytes, *, edit: bool) -> None:
        content_type = self.headers.get("Content-Type") or ""
        source: Path | None = None
        if edit or "multipart/form-data" in content_type:
            fields, files = _parse_multipart(content_type, body)
            prompt = fields.get("prompt") or ""
            size = fields.get("size")
            upload = next((item for item in files if item[0] in ("image", "image[]")), None)
            if upload is None:
                self._send(400, _error(400, "image file is required"), "application/json")
                return
            suffix = Path(upload[1]).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                suffix = ".png"
            WORK_DIR.mkdir(parents=True, exist_ok=True)
            source = WORK_DIR / (uuid.uuid4().hex + suffix)
            source.write_bytes(upload[2])
        else:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, _error(400, "image request is not JSON"), "application/json")
                return
            if not isinstance(payload, dict):
                self._send(400, _error(400, "image request must be an object"), "application/json")
                return
            prompt = payload.get("prompt") if isinstance(payload.get("prompt"), str) else ""
            size = payload.get("size")
        if not isinstance(prompt, str) or not prompt.strip():
            self._send(400, _error(400, "prompt is required"), "application/json")
            return
        with IMAGE_LOCK:
            image = generate_image(
                prompt=prompt,
                edit_source=source,
                size=size,
                work_root=WORK_DIR,
            )
        self._send(200, _image_response(image), "application/json")

    def _speech(self) -> None:
        ensure_beep()
        raw = BEEP_PATH.read_bytes()
        self._send(200, raw, "audio/mpeg")

    def _transcription(self, body: bytes) -> None:
        content_type = self.headers.get("Content-Type") or ""
        received = len(body)
        if "multipart/form-data" in content_type:
            _fields, files = _parse_multipart(content_type, body)
            if files:
                received = len(files[0][2])
        text = f"dev transcript (stub): received {received} bytes"
        self._send(200, json.dumps({"text": text}).encode(), "application/json")


def main() -> None:
    if not ADAPTER_API_KEY:
        raise SystemExit("ADAPTER_API_KEY is not set")
    ensure_beep()
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("ADAPTER_HOST", "127.0.0.1")
    port = int(os.environ.get("ADAPTER_PORT", "4010"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"adapter listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
