"""Exercise the local LiteLLM proxy. Reads .env. Does not print secrets."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env() -> None:
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


def request(method: str, path: str, body: bytes | None = None, headers: dict | None = None, timeout: float = 120):
    base = os.environ.get("LITELLM_BASE", "http://127.0.0.1:4000")
    req_headers = {"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"]}
    if headers:
        req_headers.update(headers)
    request_obj = urllib.request.Request(base + path, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(request_obj, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def expect(name: str, ok: bool, detail: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    return ok


def main() -> int:
    load_env()
    passed = True

    status, _ctype, raw = request("GET", "/v1/models")
    try:
        ids = sorted(item.get("id") for item in json.loads(raw).get("data", []))
    except (json.JSONDecodeError, AttributeError):
        ids = []
    needed = {"grok-4.7", "grok-image", "grok-tts", "grok-asr"}
    aliases = {
        "claude-haiku-4-5",
        "claude-haiku-5-5",
        "claude-sonnet-5",
        "claude-sonnet-5-5",
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-fable-5",
        "claude-fable-5-1",
        "gpt-5",
        "gpt-5.6",
        "gpt-5-mini",
        "gpt-5-nano",
        "gpt-6",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-6.1",
        "gpt-6.1-sol",
        "gpt-6.1-luna",
        "gpt-4o",
        "gpt-4o-mini",
        "gemini-2.5-pro",
        "gemini-4-argon",
    }
    missing = sorted((needed | aliases) - set(ids))
    passed &= expect(
        "models",
        status == 200 and not missing,
        f"HTTP {status} count={len(ids)} missing={missing}",
    )

    ui_names = []
    ui_status = 0
    ui_total = None
    for page in (1, 2):
        ui_status, _ctype, raw = request(
            "GET", f"/v2/model/info?include_team_models=true&page={page}&size=50"
        )
        try:
            body = json.loads(raw)
            ui_total = body.get("total_count")
            ui_names.extend(row.get("model_name") for row in body.get("data", []))
        except json.JSONDecodeError:
            ui_names = []
            break
    ui_missing = sorted((needed | aliases) - set(ui_names))
    passed &= expect(
        "ui-models",
        ui_status == 200
        and not ui_missing
        and "gpt-5-mini" in ui_names[:50]
        and "claude-haiku-4-5" in ui_names[:50]
        and "grok-image" in ui_names[:50],
        f"HTTP {ui_status} total={ui_total} page1={ui_names[:50]} missing={ui_missing}",
    )

    status, _ctype, raw = request("GET", "/model/info")
    modes = {}
    try:
        for row in json.loads(raw).get("data", []):
            info = row.get("model_info") if isinstance(row.get("model_info"), dict) else {}
            modes[row.get("model_name")] = info.get("mode")
    except json.JSONDecodeError:
        pass
    passed &= expect(
        "model-info",
        modes.get("grok-4.7") == "chat"
        and modes.get("gpt-5-mini") == "chat"
        and modes.get("claude-sonnet-5-5") == "chat"
        and modes.get("grok-image") == "image_generation"
        and modes.get("grok-tts") == "audio_speech"
        and modes.get("grok-asr") == "audio_transcription",
        str(modes),
    )

    chat = json.dumps(
        {
            "model": "grok-4.7",
            "messages": [{"role": "user", "content": "Reply with the single word pong."}],
            "max_tokens": 16,
        }
    ).encode()
    status, _ctype, raw = request("POST", "/v1/chat/completions", chat, {"Content-Type": "application/json"})
    content = ""
    try:
        content = json.loads(raw)["choices"][0]["message"].get("content") or ""
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        content = raw[:180].decode("utf-8", "replace")
    passed &= expect("chat", status == 200 and "pong" in content.lower(), f"HTTP {status} {content[:80]!r}")

    alias_chat = json.dumps(
        {
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "Reply with the single word pong."}],
            "max_tokens": 16,
        }
    ).encode()
    status, _ctype, raw = request("POST", "/v1/chat/completions", alias_chat, {"Content-Type": "application/json"})
    alias_content = ""
    alias_model = ""
    try:
        alias_body = json.loads(raw)
        alias_content = alias_body["choices"][0]["message"].get("content") or ""
        alias_model = str(alias_body.get("model") or "")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        alias_content = raw[:180].decode("utf-8", "replace")
    passed &= expect(
        "alias-chat",
        status == 200 and "pong" in alias_content.lower(),
        f"HTTP {status} model={alias_model!r} {alias_content[:80]!r}",
    )

    anthropic = json.dumps(
        {
            "model": "claude-haiku-4-5",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply with the single word pong."}],
        }
    ).encode()
    status, _ctype, raw = request("POST", "/v1/messages", anthropic, {"Content-Type": "application/json"})
    anthropic_text = raw.decode("utf-8", "replace")
    passed &= expect(
        "alias-messages",
        status == 200 and "pong" in anthropic_text.lower(),
        f"HTTP {status} bytes={len(raw)}",
    )

    streamed = json.dumps(
        {
            "model": "grok-4.7",
            "messages": [{"role": "user", "content": "Reply with the single word pong."}],
            "max_tokens": 16,
            "stream": True,
        }
    ).encode()
    status, ctype, raw = request(
        "POST", "/v1/chat/completions", streamed, {"Content-Type": "application/json"}
    )
    text = raw.decode("utf-8", "replace")
    passed &= expect(
        "chat-stream",
        status == 200 and "data:" in text and "pong" in text.lower(),
        f"HTTP {status} type={ctype} bytes={len(raw)}",
    )

    responses = json.dumps(
        {
            "model": "grok-4.7",
            "input": "Reply with the single word pong.",
            "max_output_tokens": 32,
        }
    ).encode()
    status, _ctype, raw = request("POST", "/v1/responses", responses, {"Content-Type": "application/json"})
    response_text = raw.decode("utf-8", "replace")
    passed &= expect(
        "responses",
        status == 200 and "pong" in response_text.lower(),
        f"HTTP {status} bytes={len(raw)}",
    )

    speech = json.dumps({"model": "grok-tts", "input": "hello from the dev stub", "voice": "alloy"}).encode()
    status, ctype, raw = request("POST", "/v1/audio/speech", speech, {"Content-Type": "application/json"})
    passed &= expect(
        "speech",
        status == 200 and raw[:3] == b"ID3" or (status == 200 and raw[:2] == b"\xff\xfb"),
        f"HTTP {status} type={ctype} bytes={len(raw)} magic={raw[:3]!r}",
    )

    boundary = "----grokdevboundary"
    audio = b"not-really-audio"
    multipart = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="model"\r\n\r\n'
        "grok-asr\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="note.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    status, _ctype, raw = request(
        "POST",
        "/v1/audio/transcriptions",
        multipart,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    transcript = ""
    try:
        transcript = json.loads(raw).get("text") or ""
    except json.JSONDecodeError:
        transcript = raw[:180].decode("utf-8", "replace")
    passed &= expect(
        "transcription",
        status == 200 and transcript.startswith("dev transcript (stub):"),
        f"HTTP {status} {transcript!r}",
    )

    if "--skip-images" in sys.argv:
        print("SKIP images")
        return 0 if passed else 1

    image = json.dumps(
        {
            "model": "grok-image",
            "prompt": "a solid blue square centered on a plain white background, no text",
            "size": "1024x1024",
            "n": 1,
        }
    ).encode()
    status, _ctype, raw = request(
        "POST", "/v1/images/generations", image, {"Content-Type": "application/json"}, timeout=300
    )
    url = ""
    try:
        item = json.loads(raw)["data"][0]
        url = item.get("url") or ""
        has_b64 = bool(item.get("b64_json"))
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        has_b64 = False
        url = raw[:180].decode("utf-8", "replace")
    image_ok = status == 200 and url.startswith("http://127.0.0.1:4010/v1/media/") and has_b64
    passed &= expect("image", image_ok, f"HTTP {status} url={url}")
    if image_ok:
        with urllib.request.urlopen(url, timeout=30) as fetched:
            media = fetched.read(16)
            media_type = fetched.headers.get("Content-Type", "")
        passed &= expect(
            "image-fetch",
            media[:3] == b"\xff\xd8\xff" or media[:8] == b"\x89PNG\r\n\x1a\n" or media[:4] == b"RIFF",
            f"type={media_type} magic={media[:8]!r}",
        )

        edit_boundary = "----grokimgedit"
        # Re-download the full image for the edit call.
        with urllib.request.urlopen(url, timeout=30) as fetched:
            full = fetched.read()
        edit = (
            f"--{edit_boundary}\r\n"
            'Content-Disposition: form-data; name="model"\r\n\r\n'
            "grok-image\r\n"
            f"--{edit_boundary}\r\n"
            'Content-Disposition: form-data; name="prompt"\r\n\r\n'
            "make the square red\r\n"
            f"--{edit_boundary}\r\n"
            'Content-Disposition: form-data; name="image"; filename="source.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode() + full + f"\r\n--{edit_boundary}--\r\n".encode()
        status, _ctype, raw = request(
            "POST",
            "/v1/images/edits",
            edit,
            {"Content-Type": f"multipart/form-data; boundary={edit_boundary}"},
            timeout=300,
        )
        edit_url = ""
        try:
            edit_url = json.loads(raw)["data"][0].get("url") or ""
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            edit_url = raw[:180].decode("utf-8", "replace")
        passed &= expect(
            "image-edit",
            status == 200 and edit_url.startswith("http://127.0.0.1:4010/v1/media/"),
            f"HTTP {status} url={edit_url}",
        )

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
