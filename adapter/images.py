"""Turn OpenAI image requests into one headless `grok` image tool call."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import quote

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_PATH_RE = re.compile(r"(/[^\s\"']+\.(?:png|jpg|jpeg|webp))", re.IGNORECASE)


class ImageError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def aspect_ratio(size: object) -> str:
    if not isinstance(size, str) or "x" not in size.lower():
        return "1:1"
    raw = size.lower().replace(" ", "")
    width_text, _, height_text = raw.partition("x")
    try:
        width = int(width_text)
        height = int(height_text)
    except ValueError:
        return "1:1"
    if width <= 0 or height <= 0:
        return "1:1"
    ratio = width / height
    if 0.9 <= ratio <= 1.1:
        return "1:1"
    if ratio >= 1.7:
        return "16:9"
    if ratio >= 1.3:
        return "3:2"
    if ratio <= 0.6:
        return "9:16"
    return "2:3"


def _session_dir(cwd: Path, session_id: str) -> Path:
    encoded = quote(str(cwd), safe="")
    return Path.home() / ".grok" / "sessions" / encoded / session_id


def _newest(roots: list[Path], since: float, skip: set[Path]) -> Path | None:
    best: Path | None = None
    best_mtime = since
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if path.resolve() in skip:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime + 1 < since:
                continue
            if best is None or mtime >= best_mtime:
                best = path
                best_mtime = mtime
    return best


def _paths_in_text(text: str) -> list[Path]:
    found = []
    for match in _PATH_RE.findall(text):
        path = Path(match)
        if path.is_file():
            found.append(path)
    return found


def generate_image(
    *,
    prompt: str,
    edit_source: Path | None,
    size: object,
    work_root: Path,
    timeout: float = 240,
) -> Path:
    if not prompt.strip():
        raise ImageError("prompt is empty")
    job = work_root / uuid.uuid4().hex
    job.mkdir(parents=True)
    source_copy: Path | None = None
    if edit_source is not None:
        source_copy = job / ("source" + edit_source.suffix.lower())
        shutil.copyfile(edit_source, source_copy)
    started = time.time()
    session_id = str(uuid.uuid4())
    ratio = aspect_ratio(size)
    if source_copy is None:
        tool = "image_gen"
        instruction = (
            "Call the image_gen tool exactly once. "
            f"Set aspect_ratio to {ratio}. "
            "Pass this prompt through unchanged:\n"
            f"{prompt}\n"
            "Do not call any other tool. When the tool returns, stop."
        )
    else:
        tool = "image_edit"
        instruction = (
            "Call the image_edit tool exactly once. "
            f"Set image to this absolute path: {source_copy}. "
            "Pass this prompt through unchanged:\n"
            f"{prompt}\n"
            "Do not call any other tool. When the tool returns, stop."
        )
    prompt_file = job / "prompt.txt"
    prompt_file.write_text(instruction)
    command = [
        "grok",
        "--prompt-file",
        str(prompt_file),
        "--verbatim",
        "--tools",
        tool,
        "--max-turns",
        "4",
        "--yolo",
        "--permission-mode",
        "bypassPermissions",
        "--no-auto-update",
        "--output-format",
        "streaming-json",
        "--cwd",
        str(job),
        "--session-id",
        session_id,
    ]
    env = os.environ.copy()
    try:
        completed = subprocess.run(
            command,
            cwd=job,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ImageError(f"grok image call timed out after {int(timeout)}s") from exc
    except OSError as exc:
        raise ImageError(f"could not start grok: {exc}") from exc
    (job / "grok.stdout").write_text(completed.stdout)
    (job / "grok.stderr").write_text(completed.stderr[-8000:])
    skip = {source_copy.resolve()} if source_copy else set()
    roots = [job, _session_dir(job, session_id)]
    chosen = _newest(roots, started, skip)
    if chosen is None:
        for candidate in _paths_in_text(completed.stdout):
            if candidate.resolve() not in skip:
                chosen = candidate
                break
    if chosen is None:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        detail = tail[-1][:300] if tail else f"exit {completed.returncode}"
        raise ImageError(f"grok produced no image ({detail})")
    return chosen
