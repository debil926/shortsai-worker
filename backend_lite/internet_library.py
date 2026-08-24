from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import re
import time
import threading
import queue
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


INTERNET_LIBRARY_VERSION = 8


class InternetLibraryError(RuntimeError):
    pass


EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/avif": ".avif", "video/mp4": ".mp4",
    "video/webm": ".webm", "video/quicktime": ".mov",
}


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _clean(value: Any, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _safe_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
        return ""
    return url


def _signature(candidate: dict[str, Any]) -> str:
    raw = "|".join((str(candidate.get("provider") or ""),
                    str(candidate.get("download_url") or ""),
                    str(candidate.get("source_page") or ""))).encode()
    return hashlib.sha256(raw).hexdigest()


def _extension(url: str, content_type: str, media_type: str) -> str:
    clean = content_type.split(";")[0].strip().lower()
    if clean in EXTENSIONS:
        return EXTENSIONS[clean]
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".mp4", ".webm", ".mov"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return mimetypes.guess_extension(clean) or (".mp4" if media_type == "video" else ".jpg")


def _download(urls: list[str], output_base: Path, media_type: str,
              max_bytes: int, timeout: int) -> tuple[Path, str]:
    errors: list[str] = []
    for raw_url in urls:
        url = _safe_url(raw_url)
        if not url:
            continue
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 ShortsAI-V2-STEP27/1.0",
            "Accept": "image/avif,image/webp,image/*,video/*,*/*;q=0.8",
        })
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = str(response.headers.get("Content-Type") or "")
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise InternetLibraryError("Файл больше допустимого размера.")
                target = output_base.with_suffix(_extension(url, content_type, media_type))
                temp = target.with_suffix(target.suffix + ".tmp")
                temp.unlink(missing_ok=True)
                total = 0
                with open(temp, "wb") as output:
                    while True:
                        chunk = response.read(512 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise InternetLibraryError("Файл превысил допустимый размер.")
                        output.write(chunk)
                if total < 500 or "text/html" in content_type.lower():
                    temp.unlink(missing_ok=True)
                    raise InternetLibraryError("Ссылка вернула не медиафайл.")
                os.replace(temp, target)
                return target, url
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                OSError, InternetLibraryError) as exc:
            errors.append(f"{url}: {exc}")
    raise InternetLibraryError("Не удалось скачать выбранный материал.\n" + "\n".join(errors[-3:]))




def _normalize_downloaded_image(asset_path: Path, output_base: Path) -> Path:
    """Normalize browser-oriented still formats before they reach preview/render.

    AVIF/WebP are valid web assets, but downstream storyboard/FFmpeg paths in
    ShortsAI assume a conventional still image and may add image-only options
    that are not supported by every demuxer.  Convert these formats once, at
    ingestion time, and persist the normalized PNG path in the internet index.
    """
    suffix = asset_path.suffix.lower()
    if suffix not in {".avif", ".webp", ".heic", ".heif"}:
        return asset_path
    target = output_base.with_suffix(".png")
    temp = target.with_suffix(".png.tmp")
    temp.unlink(missing_ok=True)
    ffmpeg = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
    ffmpeg_error = ""
    if ffmpeg:
        try:
            proc = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(asset_path), "-frames:v", "1", "-f", "image2", str(temp)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", timeout=20, check=False,
            )
            if proc.returncode == 0 and temp.is_file() and temp.stat().st_size > 200:
                os.replace(temp, target)
                asset_path.unlink(missing_ok=True)
                return target
            ffmpeg_error = (proc.stderr or proc.stdout or "ffmpeg image conversion failed")[-1000:]
        except (OSError, subprocess.SubprocessError) as exc:
            ffmpeg_error = str(exc)
        finally:
            temp.unlink(missing_ok=True)
    # Pillow is a secondary decoder only.  It is intentionally optional: the
    # production project already requires FFmpeg, but this gives WebP/AVIF a
    # second chance on machines with the corresponding Pillow codec installed.
    try:
        from PIL import Image  # type: ignore
        with Image.open(asset_path) as image:
            image.seek(0)
            image.convert("RGB").save(target, format="PNG")
        if target.is_file() and target.stat().st_size > 200:
            asset_path.unlink(missing_ok=True)
            return target
    except Exception as exc:
        pillow_error = str(exc)
    else:
        pillow_error = "Pillow image conversion failed"
    target.unlink(missing_ok=True)
    raise InternetLibraryError(
        f"Не удалось нормализовать интернет-картинку {suffix} в PNG. "
        f"FFmpeg: {ffmpeg_error or 'unavailable'}; Pillow: {pillow_error}"
    )

def _yt_dlp_command() -> list[str] | None:
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        return None


def _browser_available(name: str) -> bool:
    local = Path(str(os.environ.get("LOCALAPPDATA") or ""))
    roaming = Path(str(os.environ.get("APPDATA") or ""))
    paths = {
        "edge": local / "Microsoft" / "Edge" / "User Data",
        "chrome": local / "Google" / "Chrome" / "User Data",
        "brave": local / "BraveSoftware" / "Brave-Browser" / "User Data",
        "firefox": roaming / "Mozilla" / "Firefox" / "Profiles",
    }
    return bool(str(paths.get(name) or "")) and paths.get(name, Path()).is_dir()


def _cookie_attempts(renderer_root: Path, settings: dict[str, Any], url: str) -> list[list[str]]:
    if not any(host in url.lower() for host in ("youtube.com", "youtu.be")):
        return [[]]
    section = settings.get("yt_dlp") if isinstance(settings.get("yt_dlp"), dict) else {}
    cookie_file = str(section.get("cookies_file") or "").strip()
    configured = str(section.get("cookies_browser") or "auto").strip().lower()
    profile = str(section.get("browser_profile") or "").strip()
    attempts: list[list[str]] = []
    if cookie_file:
        path = Path(cookie_file)
        if not path.is_absolute():
            path = renderer_root / path
        if path.is_file():
            attempts.append(["--cookies", str(path)])

    browser_names: list[str] = []
    if configured and configured not in {"none", "off", "false"}:
        if configured == "auto":
            # Try every browser that actually has a profile.  The previous
            # implementation stopped at the first browser, which is a bad
            # assumption on Windows: the active YouTube session may live in
            # Chrome/Brave while Firefox/Edge merely exists on disk.
            browser_names = [
                name for name in ("chrome", "edge", "brave", "firefox")
                if _browser_available(name)
            ]
        else:
            browser_names = [configured]
    for browser in browser_names:
        value = f"{browser}:{profile}" if profile else browser
        attempts.append(["--cookies-from-browser", value])

    # Public downloads should not touch a live browser cookie database unless
    # they actually need to. This also avoids Chromium "database is locked"
    # and Windows DPAPI failures being treated as the primary download error.
    attempts.insert(0, [])
    unique: list[list[str]] = []
    for attempt in attempts or [[]]:
        if attempt not in unique:
            unique.append(attempt)
    return unique


def _youtube_strategy_attempts(url: str) -> list[list[str]]:
    """Ordered yt-dlp strategies for public YouTube videos.

    Keep the first attempt conservative, then vary player client / format
    only when YouTube needs it.  Cookies are handled independently by
    _cookie_attempts so the same strategy can be retried with the user's
    available signed-in browser sessions.
    """
    if not any(host in url.lower() for host in ("youtube.com", "youtu.be")):
        return [[]]
    return [
        [],
        ["--extractor-args", "youtube:player_client=android_vr"],
        ["--extractor-args", "youtube:player_client=web_safari"],
        ["--extractor-args", "youtube:player_client=web,android_vr"],
    ]


def _youtube_format_attempts(url: str) -> list[tuple[str, list[str]]]:
    """Format fallbacks for YouTube.

    A format-selection failure is cheap and must not consume the whole network
    budget.  Try progressively less opinionated selectors, ending with no -f
    at all so yt-dlp can choose from the formats actually exposed by the
    current YouTube player client.
    """
    if not any(host in url.lower() for host in ("youtube.com", "youtu.be")):
        return [("configured", [])]
    return [
        ("720p-av", ["-f", "bv*[height<=720]+ba/b[height<=720]/best[height<=720]/best"]),
        ("mp4-fallback", ["-f", "best[ext=mp4][height<=720]/best[ext=mp4]/best[height<=720]/best"]),
        ("single-best", ["-f", "best[height<=720]/best"]),
        ("auto", []),
    ]


def _youtube_format_unavailable_error(text: str) -> bool:
    value = str(text or "").lower()
    return (
        "requested format is not available" in value
        or "only images are available for download" in value
        or "use --list-formats" in value
        or "use --list-formats to see" in value
    )


def _youtube_auth_or_bot_error(text: str) -> bool:
    value = str(text or "").lower()
    needles = (
        "sign in to confirm",
        "sign in to youtube",
        "not a bot",
        "confirm you’re not a bot",
        "confirm you're not a bot",
        "login required",
        "cookies",
    )
    return any(token in value for token in needles)


def _cookie_label(cookie_args: list[str]) -> str:
    if not cookie_args:
        return "without cookies"
    if "--cookies-from-browser" in cookie_args:
        try:
            return "cookies:" + cookie_args[cookie_args.index("--cookies-from-browser") + 1]
        except Exception:
            return "browser cookies"
    if "--cookies" in cookie_args:
        return "cookies:file"
    return "cookies"


def _extractor_runtime_args(url: str) -> list[str]:
    """Return the current yt-dlp JavaScript solver options for YouTube."""
    if not any(host in url.lower() for host in ("youtube.com", "youtu.be")):
        return []
    node = shutil.which("node.exe") or shutil.which("node")
    if not node:
        raise InternetLibraryError(
            "YouTube download requires Node.js for the yt-dlp JavaScript challenge solver."
        )
    return [
        "--js-runtimes", f"node:{node}",
        "--remote-components", "ejs:github",
    ]



def _download_file_size(output_base: Path | None) -> int:
    """Return the current bytes written for one yt-dlp output base.

    yt-dlp may write ``.part`` files and later rename/merge them, so track all
    matching files instead of assuming one exact suffix.
    """
    if output_base is None:
        return 0
    total = 0
    try:
        for file in output_base.parent.glob(output_base.name + ".*"):
            try:
                if file.is_file():
                    total += max(0, int(file.stat().st_size))
            except OSError:
                continue
    except OSError:
        return 0
    return total


def _communicate_with_youtube_phase_budget(
    proc: subprocess.Popen,
    *,
    output_base: Path | None = None,
    extraction_timeout: float = 35,
    download_timeout: float = 30,
    download_hard_timeout: float = 180,
) -> tuple[str, str, bool, str]:
    """Read yt-dlp live with phase-aware and progress-aware watchdogs.

    Extraction has a fixed deadline.  Once yt-dlp reaches ``[download]`` the
    old global/fixed transfer clock is intentionally abandoned: the transfer
    is considered healthy while the destination/part file keeps growing.
    ``download_timeout`` is therefore an *inactivity/stall* timeout, while
    ``download_hard_timeout`` remains a final safety cap for pathological
    processes.  This prevents a legitimate 120-second section download from
    being killed merely because it needs more than 35 seconds on the wire.
    """
    q: queue.Queue[tuple[str, str | None]] = queue.Queue()
    out_lines: list[str] = []
    err_lines: list[str] = []

    def pump(stream, tag: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                q.put((tag, line))
        except Exception:
            pass
        finally:
            q.put((tag, None))

    threads = []
    for stream, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
        if stream is None:
            continue
        t = threading.Thread(target=pump, args=(stream, tag), daemon=True)
        t.start()
        threads.append(t)

    started = time.monotonic()
    extraction_deadline = started + max(1.0, float(extraction_timeout))
    phase = "extraction"
    ended_streams = 0
    stall_deadline: float | None = None
    hard_deadline: float | None = None
    last_size = _download_file_size(output_base)

    while True:
        now = time.monotonic()
        if proc.poll() is not None and ended_streams >= len(threads):
            break

        if phase == "extraction":
            deadline = extraction_deadline
        else:
            # File growth is stronger liveness evidence than elapsed wall time.
            current_size = _download_file_size(output_base)
            if current_size > last_size:
                last_size = current_size
                stall_deadline = now + max(1.0, float(download_timeout))
            deadline = min(
                stall_deadline if stall_deadline is not None else now + max(1.0, float(download_timeout)),
                hard_deadline if hard_deadline is not None else now + max(1.0, float(download_hard_timeout)),
            )

        if now >= deadline:
            return "".join(out_lines), "".join(err_lines), True, phase

        try:
            tag, line = q.get(timeout=min(0.25, max(0.01, deadline - now)))
        except queue.Empty:
            continue
        if line is None:
            ended_streams += 1
            continue
        if tag == "out":
            out_lines.append(line)
        else:
            err_lines.append(line)
        low = line.lower()

        if phase == "extraction" and (
            "[download]" in low
            or "destination:" in low
            or "resuming download" in low
        ):
            phase = "download"
            now = time.monotonic()
            stall_deadline = now + max(1.0, float(download_timeout))
            hard_deadline = now + max(float(download_timeout), float(download_hard_timeout), 1.0)
            last_size = _download_file_size(output_base)
        elif phase == "download" and (
            "[download]" in low
            or "merging formats" in low
            or "fixing" in low
            or "postprocess" in low
        ):
            # yt-dlp emitted fresh transfer/post-processing activity.
            stall_deadline = time.monotonic() + max(1.0, float(download_timeout))

    return "".join(out_lines), "".join(err_lines), False, phase

def _download_with_extractor(
    url: str,
    output_base: Path,
    max_bytes: int,
    timeout: int,
    *,
    renderer_root: Path,
) -> Path:
    command = _yt_dlp_command()
    if not command:
        raise InternetLibraryError(
            "Для этого видео нужен yt-dlp. Установи в V2: .\\venv\\Scripts\\python.exe -m pip install -U yt-dlp"
        )
    settings = _read_json(renderer_root / "internet_search_settings.json", {})
    output_template = str(output_base) + ".%(ext)s"
    lower_url = url.lower()
    is_youtube = any(host in lower_url for host in ("youtube.com", "youtu.be"))
    is_coub = "coub.com" in lower_url
    format_selector = "bv*+ba/b" if is_coub else None
    base_args = [
        "--no-playlist",
        "--no-progress",
        "--restrict-filenames",
        "--retries", "1",
        "--fragment-retries", "2",
        "--socket-timeout", "12",
        "--max-filesize", str(max_bytes),
        "--download-sections", "*0-120",
        "--merge-output-format", "mp4",
        "-o", output_template,
    ]
    if format_selector:
        base_args += ["-f", format_selector]
    runtime_args = _extractor_runtime_args(url)
    cookie_attempts = _cookie_attempts(renderer_root, settings, url)
    strategies = _youtube_strategy_attempts(url)
    format_attempts = _youtube_format_attempts(url) if is_youtube else [("configured", [])]
    errors: list[str] = []
    attempt_log: list[str] = []
    auth_seen_without_cookies = False
    format_error_seen = False
    cookie_attempt_seen = False
    youtube_download_started_ever = False
    youtube_download_phase_attempts = 0
    started = time.monotonic()
    # YouTube needs a larger orchestration budget than a direct HTTP fetch.
    # Each yt-dlp attempt has its own extraction and transfer clocks below.
    total_budget = max(70, min(max(int(timeout), 70), 110)) if is_youtube else max(12, min(int(timeout), 50))

    # Strategy-first for anonymous access, then all available cookie sources.
    # Once YouTube explicitly asks for sign-in/bot confirmation, skip further
    # anonymous strategy variants and go directly to cookies.
    plans: list[tuple[list[str], list[str], str, list[str]]] = []
    for cookie_args in cookie_attempts:
        for strategy_args in strategies:
            for format_label, format_args in format_attempts:
                plans.append((cookie_args, strategy_args, format_label, format_args))

    for cookie_args, strategy_args, format_label, format_args in plans:
        elapsed_total = time.monotonic() - started
        # The orchestration budget limits repeated extraction attempts only.
        # Once a real media transfer has started, do NOT let that old wall-clock
        # budget suppress the next download-format retry.  The transfer itself
        # is protected by the file-growth stall watchdog and hard safety cap.
        if elapsed_total >= total_budget and not (is_youtube and youtube_download_started_ever):
            errors.append("youtube extraction/orchestration budget reached before any real download")
            break
        if is_youtube and youtube_download_started_ever and youtube_download_phase_attempts >= 4:
            errors.append("youtube download retry limit reached after real transfer attempts")
            break
        if auth_seen_without_cookies and not cookie_args:
            continue
        for partial in output_base.parent.glob(output_base.name + ".*.part"):
            partial.unlink(missing_ok=True)
        args = command + base_args + runtime_args + strategy_args + cookie_args + format_args + [url]
        proc = None
        label = _cookie_label(cookie_args)
        if cookie_args:
            cookie_attempt_seen = True
        strategy_label = "default"
        if "--extractor-args" in strategy_args:
            try:
                strategy_label = strategy_args[strategy_args.index("--extractor-args") + 1]
            except Exception:
                strategy_label = "alternate-client"
        attempt_log.append(f"{label}/{strategy_label}/format={format_label}")
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            if is_youtube:
                remaining = max(10, int(total_budget - (time.monotonic() - started)))
                # Extraction is bounded, but an active media transfer is not
                # killed by the orchestration clock.  After [download], file
                # growth resets a stall watchdog; a separate hard cap remains.
                extraction_budget = max(15, min(40, remaining))
                transfer_stall_budget = 60
                transfer_hard_budget = 240
                stdout, stderr, timed_out, timeout_phase = _communicate_with_youtube_phase_budget(
                    proc,
                    output_base=output_base,
                    extraction_timeout=extraction_budget,
                    download_timeout=transfer_stall_budget,
                    download_hard_timeout=transfer_hard_budget,
                )
                if timeout_phase == "download":
                    youtube_download_started_ever = True
                    youtube_download_phase_attempts += 1
                if timed_out:
                    if os.name == "nt" and proc.pid:
                        try:
                            subprocess.run(
                                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=5,
                                check=False,
                            )
                        except Exception:
                            proc.kill()
                    else:
                        proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                    details = ((stderr or "") + "\n" + (stdout or "")).strip()[-1200:]
                    if not cookie_args and _youtube_auth_or_bot_error(details):
                        auth_seen_without_cookies = True
                    errors.append(
                        f"{label}/{strategy_label}/format={format_label}: "
                        f"youtube {timeout_phase} timeout" + (f" | {details}" if details else "")
                    )
                    continue
            else:
                remaining = max(4, int(total_budget - (time.monotonic() - started)))
                per_attempt_timeout = max(4, min(12, remaining))
                try:
                    stdout, stderr = proc.communicate(timeout=per_attempt_timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        stdout, stderr = proc.communicate(timeout=5)
                    except Exception:
                        stdout, stderr = "", ""
                    details = ((stderr or "") + "\n" + (stdout or "")).strip()[-900:]
                    errors.append(f"{label}/{strategy_label}/format={format_label}: download timeout" + (f" | {details}" if details else ""))
                    continue
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{label}/{strategy_label}/format={format_label}: extractor launch failed: {exc}")
            continue
        details = ((stderr if proc is not None else "") or (stdout if proc is not None else "") or "").strip()[-1800:]
        if proc is None or proc.returncode != 0:
            if _youtube_format_unavailable_error(details):
                format_error_seen = True
                errors.append(
                    f"{label}/{strategy_label}/format={format_label}: FORMAT_UNAVAILABLE -> next format | {details}"
                )
                # Do not reinterpret a concrete format error as an auth failure
                # and do not burn the remaining budget on the same selector.
                continue
            if not cookie_args and _youtube_auth_or_bot_error(details):
                auth_seen_without_cookies = True
            errors.append(f"{label}/{strategy_label}/format={format_label}: {details}")
            continue
        candidates = sorted(
            output_base.parent.glob(output_base.name + ".*"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        for file in candidates:
            if file.suffix.lower() in {".mp4", ".webm", ".mov", ".mkv"} and file.stat().st_size > 1000:
                return file

    if is_youtube:
        browser_attempts = [x for x in attempt_log if x.startswith("cookies:")]
        diag = (
            f" YouTube strategies tried={len(attempt_log)}; "
            f"cookie_sources={browser_attempts or ['none available']}; "
            f"format_fallback={'used' if format_error_seen else 'not-needed'}."
        )
        if cookie_attempt_seen and format_error_seen:
            extra = (
                " YouTube auth/cookies were attempted successfully enough to reach format selection; "
                "the blocker is format availability, not Chrome/Firefox login."
            )
        elif cookie_attempt_seen:
            extra = (
                " YouTube cookies are active; remaining failures are extraction/download timing or extractor errors, "
                "not a request to close Chrome."
            )
        else:
            extra = (
                " Sign in to YouTube in Chrome/Edge/Brave/Firefox, close that browser, "
                "or set yt_dlp.cookies_browser / yt_dlp.cookies_file in internet_search_settings.json."
            )
    else:
        diag = ""
        extra = ""
    raise InternetLibraryError(
        "yt-dlp could not download the video. " + "\n".join(errors[-5:]) + diag + extra
    )


class InternetLibrary:
    def __init__(self, renderer_root: Path) -> None:
        self.renderer_root = renderer_root
        self.root = renderer_root / "internet_library"
        self.files_root = self.root / "files"
        self.index_path = self.root / "index.json"
        self.files_root.mkdir(parents=True, exist_ok=True)

    def _payload(self) -> dict[str, Any]:
        payload = _read_json(self.index_path, {})
        entries = payload.get("entries") if isinstance(payload, dict) else None
        return {"version": 1, "updated_at": payload.get("updated_at") if isinstance(payload, dict) else None,
                "entries": entries if isinstance(entries, list) else []}

    def annotate_entity_evidence(self, entry_id: str, entity: str, source: str = "search_result_metadata+query") -> dict[str, Any] | None:
        """Persist bounded acquisition-time entity evidence for one internet entry."""
        entry_id = str(entry_id or "").strip()
        entity = str(entity or "").strip().lower()
        if not entry_id or not entity:
            return None
        payload = self._payload()
        updated = None
        for raw in payload["entries"]:
            if not isinstance(raw, dict) or str(raw.get("id") or "") != entry_id:
                continue
            values = [str(x).strip().lower() for x in raw.get("acquisition_entity_evidence") or [] if str(x).strip()]
            if entity not in values:
                values.append(entity)
            raw["acquisition_entity_evidence"] = sorted(set(values))
            raw["acquisition_entity_evidence_source"] = source
            raw.pop("_entity_coverage_feature_version", None)
            updated = dict(raw)
            break
        if updated is not None:
            payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            _write_json(self.index_path, payload)
        return updated

    def ingest(self, candidate: dict[str, Any], search_query: str, beat_text: str,
               max_bytes: int = 250 * 1024 * 1024, timeout: int = 50) -> tuple[dict[str, Any], bool]:
        if not isinstance(candidate, dict):
            raise InternetLibraryError("Неверный интернет-кандидат.")
        signature = _signature(candidate)
        payload = self._payload()
        for raw in payload["entries"]:
            if isinstance(raw, dict) and raw.get("remote_signature") == signature:
                asset = Path(str(raw.get("asset_path") or ""))
                if asset.is_file() and asset.stat().st_size > 500:
                    cached = dict(raw)
                    if str(cached.get("source_kind") or "") == "internet_image":
                        normalized = _normalize_downloaded_image(asset, self.files_root / signature[:24])
                        if normalized != asset:
                            cached["asset_path"] = str(normalized)
                            raw["asset_path"] = str(normalized)
                            payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
                            _write_json(self.index_path, payload)
                    return cached, True

        media_type = str(candidate.get("media_type") or "image").lower()
        if media_type not in {"image", "video", "gif"}:
            media_type = "image"
        urls = list(dict.fromkeys(str(candidate.get(k) or "") for k in
                                  ("download_url", "fallback_url", "preview_url") if candidate.get(k)))
        if not urls:
            raise InternetLibraryError("У кандидата нет адреса для скачивания.")
        output_base = self.files_root / signature[:24]
        direct_download = bool(candidate.get("direct_download", True))
        extractor_url = _safe_url(candidate.get("extractor_url") or candidate.get("source_page"))
        direct_error = ""
        asset_path = None
        used_url = ""
        if direct_download:
            try:
                asset_path, used_url = _download(urls, output_base, media_type, max_bytes, timeout)
            except InternetLibraryError as exc:
                direct_error = str(exc)
        if asset_path is None and media_type in {"video", "gif"} and extractor_url:
            try:
                asset_path = _download_with_extractor(
                    extractor_url,
                    output_base,
                    max_bytes,
                    max(8, min(int(timeout), 30)),
                    renderer_root=self.renderer_root,
                )
                used_url = extractor_url
            except InternetLibraryError as exc:
                if direct_error:
                    raise InternetLibraryError(direct_error + "\n" + str(exc)) from exc
                raise
        if asset_path is None:
            if direct_error:
                raise InternetLibraryError(direct_error)
            asset_path, used_url = _download(urls, output_base, media_type, max_bytes, timeout)
        if media_type == "image":
            asset_path = _normalize_downloaded_image(Path(asset_path), output_base)
        provider = _clean(candidate.get("provider"), 60) or "internet"
        title = _clean(candidate.get("title") or candidate.get("description") or search_query or "Интернет-материал", 220)
        description = _clean(candidate.get("description") or title, 500)
        source_kind = "internet_meme" if provider in {"tenor", "imgflip"} else (
            "internet_video" if media_type in {"video", "gif"} else "internet_image")
        duration = float(candidate.get("duration") or 0)
        if duration <= 0:
            duration = 6.0 if source_kind == "internet_meme" else 30.0
        entry = {
            "id": f"internet_{signature[:20]}", "source_kind": source_kind,
            "asset_path": str(asset_path), "duration": round(max(0.5, duration), 3),
            "description": title, "text": description, "title": title,
            "category": "meme" if source_kind == "internet_meme" else "internet",
            "tags": [x for x in (provider, media_type, source_kind, _clean(search_query, 100)) if x],
            "search_query": " ".join(x for x in (search_query, beat_text, title, description) if str(x).strip()),
            "provider": provider, "remote_signature": signature,
            "remote_download_url": used_url, "source_page": _clean(candidate.get("source_page"), 1000),
            "attribution": _clean(candidate.get("attribution"), 500),
            "width": int(candidate.get("width") or 0), "height": int(candidate.get("height") or 0),
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
        payload["entries"] = [x for x in payload["entries"] if not (
            isinstance(x, dict) and str(x.get("id") or "") == entry["id"])] + [entry]
        payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        _write_json(self.index_path, payload)
        return entry, False
