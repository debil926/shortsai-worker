"""RunPod serverless entry point: search YouTube, download the best
candidate, trim it down to a short clip, and hand back only that short
clip (base64) - the point is that the slow/expensive part (searching,
downloading a full-length source over a fast cloud connection) happens
here, and only a few seconds of already-trimmed video crosses back to
whatever machine called this, over whatever connection it has.

Reuses backend/internet_search.py and backend/internet_library.py as-is
(both are dependency-free of the rest of the ShortsAI project - internet_
library.py only needs a plain directory to store its downloads in, which
here is a per-job temp dir, not the real project structure).
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runpod  # type: ignore

from backend_lite.internet_library import InternetLibrary, InternetLibraryError
from backend_lite.internet_search import InternetSearchError, _search_youtube

MAX_CANDIDATES_DEFAULT = 3
MAX_DURATION_DEFAULT = 120.0
DOWNLOAD_TIMEOUT_SECONDS = 90
TRIM_PADDING_SECONDS = 1.0


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _probe_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    try:
        return max(0.0, float((result.stdout or "0").strip()))
    except ValueError:
        return 0.0


def _trim(source: Path, output: Path, *, start: float, duration: float) -> None:
    command = [
        _ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, start):.3f}", "-i", str(source),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(output),
    ]
    subprocess.run(command, capture_output=True, text=True, timeout=120, check=True)


def _search_and_fetch(job_input: dict[str, Any], work_root: Path) -> dict[str, Any]:
    query = str(job_input.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}

    beat_text = str(job_input.get("beat_text") or query)
    max_candidates = max(1, min(6, int(job_input.get("max_candidates") or MAX_CANDIDATES_DEFAULT)))
    max_duration = float(job_input.get("max_duration") or MAX_DURATION_DEFAULT)
    target_duration = float(job_input.get("target_duration") or 3.0)
    source_start = max(0.0, float(job_input.get("source_start") or 0.0))

    try:
        results = _search_youtube(query, max_candidates * 2)
    except InternetSearchError as exc:
        return {"error": f"search failed: {exc}"}

    library = InternetLibrary(work_root)
    attempts = 0
    last_error = ""
    for remote in results:
        if attempts >= max_candidates:
            break
        if str(remote.get("media_type") or "") != "video":
            continue
        duration = float(remote.get("duration") or 0)
        if duration <= 0 or duration > max_duration:
            continue
        attempts += 1
        try:
            entry, _cache_hit = library.ingest(
                remote, search_query=query, beat_text=beat_text,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )
        except InternetLibraryError as exc:
            last_error = str(exc)
            continue

        source_path = Path(str(entry.get("asset_path") or entry.get("source_path") or ""))
        if not source_path.is_file():
            last_error = f"downloaded but file missing: {source_path}"
            continue

        real_duration = _probe_duration(source_path)
        trim_start = min(source_start, max(0.0, real_duration - target_duration))
        trim_duration = min(target_duration + TRIM_PADDING_SECONDS, max(0.5, real_duration - trim_start))

        output = work_root / "clip_out.mp4"
        try:
            _trim(source_path, output, start=trim_start, duration=trim_duration)
        except subprocess.CalledProcessError as exc:
            last_error = f"trim failed: {exc.stderr[-500:] if exc.stderr else exc}"
            continue

        clip_bytes = output.read_bytes()
        return {
            "clip_base64": base64.b64encode(clip_bytes).decode("ascii"),
            "duration": trim_duration,
            "source_start": trim_start,
            "source_page": str(entry.get("source_page") or remote.get("source_page") or ""),
            "provider": str(entry.get("provider") or remote.get("provider") or ""),
            "title": str(remote.get("title") or ""),
            "attempts": attempts,
            "size_bytes": len(clip_bytes),
        }

    return {"error": f"no usable candidate found after {attempts} attempt(s); last_error={last_error}"}


def handler(job: dict[str, Any]) -> dict[str, Any]:
    job_input = dict(job.get("input") or {})
    with tempfile.TemporaryDirectory(prefix="shortsai_worker_") as tmp:
        return _search_and_fetch(job_input, Path(tmp))


runpod.serverless.start({"handler": handler})
