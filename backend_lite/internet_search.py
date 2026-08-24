from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


ENGINE_VERSION = "38.0"
HTTP_TIMEOUT_SECONDS = 12
YT_DLP_SEARCH_TIMEOUT_SECONDS = 25


class InternetSearchError(RuntimeError):
    pass


DEFAULT_SETTINGS: dict[str, Any] = {
    "version": 4,
    "result_limit": 10,
    "candidate_pool": 48,
    "vision_candidates": 10,
    "vision_threshold": 0.50,
    "max_per_provider": 10,
    "cache_hours": 24,
    "country": "ALL",
    "search_language": "auto",
    "providers": {
        "wikimedia": True,
        "brave_images": True,
        "brave_videos": True,
        "youtube": True,
        "pexels": True,
        "tenor": True,
        "giphy": True,
        "pixabay": True,
        "imgflip_templates": True,
    },
    "brave_api_key_file": "secrets/brave_search_key.txt",
    "pexels_api_key_file": "secrets/pexels_key.txt",
    "tenor_api_key_file": "secrets/tenor_key.txt",
    "giphy_api_key_file": "secrets/giphy_key.txt",
    "pixabay_api_key_file": "secrets/pixabay_key.txt",
    "tenor_client_key": "shortsai_v2",
    "yt_dlp": {
        "cookies_browser": "auto",
        "cookies_file": "",
        "browser_profile": "",
    },
}

RU_MEME_HINTS = {
    "удив": "shocked reaction meme",
    "шок": "shocked reaction meme",
    "офиг": "shocked reaction meme",
    "неожидан": "surprised reaction meme",
    "провал": "fail reaction meme",
    "проиг": "fail reaction meme",
    "груст": "sad reaction meme",
    "плач": "crying reaction meme",
    "смех": "laughing reaction meme",
    "смеш": "laughing reaction meme",
    "сомнен": "doubt reaction meme",
    "подозр": "suspicious reaction meme",
    "ждать": "waiting reaction meme",
    "ожидан": "waiting reaction meme",
    "нелов": "awkward reaction meme",
    "побед": "victory celebration meme",
}

MEME_HINTS = {
    "Drake Hot Bling": "choice prefer comparison no yes",
    "Two Buttons": "choice dilemma decision panic",
    "Distracted Boyfriend": "choice distracted switch betray",
    "Disaster Girl": "disaster chaos evil fire",
    "Waiting Skeleton": "waiting long time delay",
    "Sad Pablo Escobar": "sad alone waiting disappointed",
    "Surprised Pikachu": "surprised shock unexpected",
    "Futurama Fry": "doubt not sure suspicious",
    "Hide the Pain Harold": "pain awkward pretend sad",
    "Laughing Leo": "laugh funny mock",
    "Mocking Spongebob": "mock sarcasm stupid",
    "This Is Fine": "disaster calm fire fine",
    "Always Has Been": "reveal always truth",
    "Trade Offer": "trade offer receive",
    "Gru's Plan": "plan fail realization",
    "One Does Not Simply": "difficult impossible",
}


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def ensure_settings(renderer_root: Path) -> Path:
    path = renderer_root / "internet_search_settings.json"
    if not path.is_file():
        path.write_text(
            json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    (renderer_root / "secrets").mkdir(parents=True, exist_ok=True)
    return path


def load_settings(renderer_root: Path) -> dict[str, Any]:
    raw = _read_json(ensure_settings(renderer_root), {})
    result = json.loads(json.dumps(DEFAULT_SETTINGS))
    if isinstance(raw, dict):
        result.update({k: v for k, v in raw.items() if k != "providers"})
        if isinstance(raw.get("providers"), dict):
            result["providers"].update(raw["providers"])
        if isinstance(raw.get("yt_dlp"), dict):
            defaults = dict(DEFAULT_SETTINGS["yt_dlp"])
            defaults.update(raw["yt_dlp"])
            result["yt_dlp"] = defaults
    return result


def _secret(renderer_root: Path, settings: dict[str, Any], file_key: str) -> str:
    direct = str(settings.get(file_key.removesuffix("_file")) or "").strip()
    if direct:
        return direct
    value = str(settings.get(file_key) or "").strip()
    if not value:
        return ""
    path = Path(value)
    if not path.is_absolute():
        path = renderer_root / path
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig").strip()


def provider_status(renderer_root: Path) -> dict[str, Any]:
    settings = load_settings(renderer_root)
    providers = settings.get("providers") or {}
    brave = bool(_secret(renderer_root, settings, "brave_api_key_file"))
    pexels = bool(_secret(renderer_root, settings, "pexels_api_key_file"))
    tenor = bool(_secret(renderer_root, settings, "tenor_api_key_file"))
    giphy = bool(_secret(renderer_root, settings, "giphy_api_key_file"))
    pixabay = bool(_secret(renderer_root, settings, "pixabay_api_key_file"))
    return {
        "wikimedia": bool(providers.get("wikimedia", True)),
        "imgflip_templates": bool(providers.get("imgflip_templates", True)),
        "brave_images": bool(providers.get("brave_images", True) and brave),
        "brave_videos": bool(providers.get("brave_videos", True) and brave),
        "pexels": bool(providers.get("pexels", True) and pexels),
        "tenor": bool(providers.get("tenor", True) and tenor),
        "giphy": bool(providers.get("giphy", True) and giphy),
        "pixabay": bool(providers.get("pixabay", True) and pixabay),
        "brave_key_missing": bool((providers.get("brave_images", True) or providers.get("brave_videos", True)) and not brave),
        "pexels_key_missing": bool(providers.get("pexels", True) and not pexels),
        "tenor_key_missing": bool(providers.get("tenor", True) and not tenor),
        "giphy_key_missing": bool(providers.get("giphy", True) and not giphy),
        "pixabay_key_missing": bool(providers.get("pixabay", True) and not pixabay),
        "settings_path": str(renderer_root / "internet_search_settings.json"),
    }


def _request_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_headers = {
        "User-Agent": "ShortsAI-V2-STEP27/1.0",
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")[:1000]
        raise InternetSearchError(f"HTTP {exc.code}: {details or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise InternetSearchError(str(exc.reason or exc)) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise InternetSearchError("Сервис вернул неверный JSON.") from exc
    if not isinstance(payload, dict):
        raise InternetSearchError("Сервис вернул неверный ответ.")
    return payload


def _clean(value: Any, limit: int = 320) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _candidate(provider: str, media_type: str, title: str, preview_url: str,
               download_url: str, source_page: str, **extra: Any) -> dict[str, Any]:
    stable = download_url or preview_url
    rid = hashlib.sha256(f"{provider}|{stable}".encode()).hexdigest()[:20]
    return {
        "id": f"remote_{rid}",
        "provider": provider,
        "media_type": media_type,
        "title": _clean(title, 180),
        "description": _clean(extra.get("description"), 320),
        "preview_url": str(preview_url or download_url),
        "download_url": str(download_url or preview_url),
        "fallback_url": str(extra.get("fallback_url") or preview_url),
        "preview_video_url": str(extra.get("preview_video_url") or ""),
        "extractor_url": str(extra.get("extractor_url") or ""),
        "direct_download": bool(extra.get("direct_download", True)),
        "source_page": str(source_page or ""),
        "width": int(extra.get("width") or 0),
        "height": int(extra.get("height") or 0),
        "duration": float(extra.get("duration") or 0),
        "attribution": _clean(extra.get("attribution"), 240),
    }


def _search_wikimedia(query: str, count: int) -> list[dict[str, Any]]:
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrsearch": query, "gsrnamespace": "6",
        "gsrlimit": str(max(1, min(25, count))), "prop": "imageinfo",
        "iiprop": "url|mime|size|extmetadata", "iiurlwidth": "720",
        "origin": "*",
    }
    payload = _request_json(
        "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
    )
    results: list[dict[str, Any]] = []
    for page in ((payload.get("query") or {}).get("pages") or []):
        if not isinstance(page, dict):
            continue
        infos = page.get("imageinfo") or []
        if not infos or not isinstance(infos[0], dict):
            continue
        info = infos[0]
        mime = str(info.get("mime") or "")
        if not (mime.startswith("image/") or mime.startswith("video/")):
            continue
        original = str(info.get("url") or "")
        preview = str(info.get("thumburl") or original)
        if not original:
            continue
        metadata = info.get("extmetadata") or {}
        def meta(name: str) -> str:
            value = metadata.get(name) if isinstance(metadata, dict) else None
            return _clean(value.get("value") if isinstance(value, dict) else "")
        results.append(_candidate(
            "wikimedia", "video" if mime.startswith("video/") else "image",
            str(page.get("title") or "").removeprefix("File:"), preview,
            original, str(info.get("descriptionurl") or ""),
            description=meta("ImageDescription"), width=info.get("width"),
            height=info.get("height"),
            attribution=" · ".join(x for x in (meta("Artist"), meta("LicenseShortName")) if x),
            fallback_url=preview,
            preview_video_url=original if mime.startswith("video/") else "",
            direct_download=True,
        ))
    return results


def _search_brave(query: str, count: int, key: str, country: str, language: str) -> list[dict[str, Any]]:
    # Use the language of each actual query. STEP27 originally forced English even
    # when the user typed Russian, which made Brave return broad unrelated pages.
    detected_language = "ru" if re.search(r"[А-Яа-яЁё]", query) else "en"
    configured_language = str(language or "auto").strip().lower()
    effective_language = (
        detected_language
        if configured_language in {"", "auto", "detect"}
        or (detected_language == "ru" and configured_language == "en")
        else configured_language
    )
    params = {"q": query, "count": str(max(1, min(50, count))),
              "country": country or "ALL", "search_lang": effective_language,
              "safesearch": "strict"}
    payload = _request_json(
        "https://api.search.brave.com/res/v1/images/search?" + urllib.parse.urlencode(params),
        {"X-Subscription-Token": key},
    )
    results: list[dict[str, Any]] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        props = item.get("properties") or {}
        thumb = item.get("thumbnail") or {}
        original = str(props.get("url") or item.get("image_url") or "")
        preview = str(thumb.get("src") or props.get("placeholder") or original)
        if not (original or preview):
            continue
        media_hint = " ".join(str(x or "") for x in (
            props.get("format"), props.get("content_type"), original, preview
        )).lower()
        media_type = "gif" if ("gif" in media_hint or original.lower().split("?")[0].endswith(".gif")) else "image"
        results.append(_candidate(
            "brave_images", media_type, str(item.get("title") or "Интернет-изображение"),
            preview, original or preview,
            str(item.get("page_url") or item.get("source") or item.get("url") or ""),
            description=item.get("description"), width=props.get("width"),
            height=props.get("height"), fallback_url=preview,
        ))
    return results



def _search_brave_videos(query: str, count: int, key: str, country: str, language: str,
                         offset: int = 0) -> list[dict[str, Any]]:
    detected_language = "ru" if re.search(r"[А-Яа-яЁё]", query) else "en"
    configured_language = str(language or "auto").strip().lower()
    effective_language = detected_language if configured_language in {"", "auto", "detect"} else configured_language
    params = {
        "q": query,
        "count": str(max(1, min(50, count))),
        "offset": str(max(0, min(9, offset))),
        "country": country or "ALL",
        "search_lang": effective_language,
        "safesearch": "strict",
    }
    payload = _request_json(
        "https://api.search.brave.com/res/v1/videos/search?" + urllib.parse.urlencode(params),
        {"X-Subscription-Token": key},
    )
    results: list[dict[str, Any]] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        page_url = str(item.get("url") or item.get("page_url") or "")
        thumb = item.get("thumbnail") or {}
        preview = str(thumb.get("src") if isinstance(thumb, dict) else thumb or "")
        video = item.get("video") or {}
        duration = 0.0
        for raw in (item.get("duration"), video.get("duration") if isinstance(video, dict) else None):
            try:
                duration = float(raw or 0)
                if duration > 0:
                    break
            except (TypeError, ValueError):
                pass
        if not page_url:
            continue
        results.append(_candidate(
            "brave_video", "video", str(item.get("title") or query),
            preview, page_url, page_url,
            description=item.get("description"),
            duration=duration,
            attribution=str((item.get("meta_url") or {}).get("hostname") or "Brave Video") if isinstance(item.get("meta_url"), dict) else "Brave Video",
            fallback_url=preview,
            extractor_url=page_url,
            direct_download=False,
        ))
    return results

def _yt_dlp_search_command() -> list[str] | None:
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return None
    return [sys.executable, "-m", "yt_dlp"]


def _parse_youtube_search_line(line: str, query: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(item, dict):
        return None
    video_id = str(item.get("id") or "").strip()
    if not video_id:
        return None
    watch_url = str(item.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}")
    duration = 0.0
    try:
        duration = float(item.get("duration") or 0)
    except (TypeError, ValueError):
        pass
    thumbnails = item.get("thumbnails") or []
    preview = ""
    if isinstance(thumbnails, list) and thumbnails:
        usable = [t for t in thumbnails if isinstance(t, dict) and t.get("url")]
        if usable:
            best = max(usable, key=lambda t: int(t.get("width") or 0))
            preview = str(best.get("url") or "")
    return _candidate(
        "youtube", "video",
        str(item.get("title") or query),
        preview, watch_url, watch_url,
        description=item.get("description"),
        duration=duration,
        attribution=str(item.get("channel") or item.get("uploader") or "YouTube"),
        fallback_url=preview,
        extractor_url=watch_url,
        direct_download=False,
    )


def _search_youtube(query: str, count: int) -> list[dict[str, Any]]:
    """Real gameplay footage (streamer/creator uploads demonstrating a
    specific character's abilities) lives on YouTube in a way generic web-
    image/stock-video search just doesn't index - see the diagnosis that
    motivated this: Brave was returning wiki/build-guide screenshots for
    "Brawl Stars Wendy shield gameplay" while YouTube search for the exact
    same text turns up actual gameplay/showcase videos. yt-dlp's ytsearch:
    pseudo-URL scrapes YouTube's own search - no API key needed, unlike
    Brave/Pexels/Tenor. --flat-playlist --skip-download means this call is
    metadata only; nothing downloads until a candidate is actually selected
    and handed to InternetLibrary.ingest, exactly like every other
    provider's results already work."""
    command = _yt_dlp_search_command()
    if command is None:
        raise InternetSearchError("yt-dlp is not available for YouTube search")
    safe_count = max(1, min(20, int(count or 8)))
    search_query = f"ytsearch{safe_count}:{query}"
    try:
        result = subprocess.run(
            [*command, search_query, "--flat-playlist", "--dump-json",
             "--skip-download", "--no-warnings"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=YT_DLP_SEARCH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise InternetSearchError(
            f"YouTube search timed out after {YT_DLP_SEARCH_TIMEOUT_SECONDS}s"
        )
    except OSError as exc:
        raise InternetSearchError(f"YouTube search failed to start: {exc}")

    results: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        candidate = _parse_youtube_search_line(line, query)
        if candidate is not None:
            results.append(candidate)
    return results


def _best_pexels_video(files: list[Any]) -> dict[str, Any] | None:
    valid = [f for f in files if isinstance(f, dict) and f.get("link") and
             "mp4" in str(f.get("file_type") or "video/mp4").lower()]
    if not valid:
        return None
    valid.sort(key=lambda f: (
        int(int(f.get("height") or 0) >= int(f.get("width") or 0)),
        int(int(f.get("width") or 0) >= 720),
        -abs(int(f.get("width") or 0) - 1080),
    ), reverse=True)
    return valid[0]


def _search_pexels(query: str, count: int, key: str) -> list[dict[str, Any]]:
    headers = {"Authorization": key}
    results: list[dict[str, Any]] = []
    video_payload = _request_json(
        "https://api.pexels.com/v1/videos/search?" + urllib.parse.urlencode({
            "query": query, "per_page": str(max(3, min(10, count // 2 + 1)))
        }), headers)
    for item in video_payload.get("videos") or []:
        if not isinstance(item, dict):
            continue
        selected = _best_pexels_video(list(item.get("video_files") or []))
        if not selected:
            continue
        pictures = item.get("video_pictures") or []
        preview = str(pictures[0].get("picture") or "") if pictures and isinstance(pictures[0], dict) else ""
        user = item.get("user") or {}
        results.append(_candidate(
            "pexels_video", "video", f"Pexels video · {query}", preview,
            str(selected.get("link") or ""), str(item.get("url") or ""),
            description=f"Автор: {user.get('name') or ''}",
            width=selected.get("width"), height=selected.get("height"),
            duration=item.get("duration"), attribution=user.get("name"),
            fallback_url=preview, preview_video_url=str(selected.get("link") or ""), direct_download=True,
        ))
    photo_payload = _request_json(
        "https://api.pexels.com/v1/search?" + urllib.parse.urlencode({
            "query": query, "per_page": str(max(3, min(10, count // 2 + 1)))
        }), headers)
    for item in photo_payload.get("photos") or []:
        if not isinstance(item, dict):
            continue
        src = item.get("src") or {}
        original = str(src.get("large2x") or src.get("large") or src.get("original") or "")
        preview = str(src.get("medium") or src.get("small") or original)
        if original:
            results.append(_candidate(
                "pexels_image", "image", str(item.get("alt") or f"Pexels photo · {query}"),
                preview, original, str(item.get("url") or ""),
                width=item.get("width"), height=item.get("height"),
                attribution=item.get("photographer"), fallback_url=preview,
            ))
    return results


def _search_tenor(query: str, count: int, key: str, client_key: str) -> list[dict[str, Any]]:
    params = {"q": query, "key": key, "client_key": client_key or "shortsai_v2",
              "limit": str(max(1, min(50, count))),
              "media_filter": "mp4,webm,gif,tinygif",
              "contentfilter": "medium", "locale": "ru_RU"}
    payload = _request_json(
        "https://tenor.googleapis.com/v2/search?" + urllib.parse.urlencode(params)
    )
    results: list[dict[str, Any]] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        formats = item.get("media_formats") or {}
        selected_name = next((n for n in ("mp4", "webm", "gif", "tinygif")
                              if isinstance(formats.get(n), dict) and formats[n].get("url")), None)
        if not selected_name:
            continue
        media = formats[selected_name]
        dims = media.get("dims") or [0, 0]
        preview = str((formats.get("tinygif") or {}).get("url") or media.get("url") or "")
        mp4_media = formats.get("mp4") if isinstance(formats.get("mp4"), dict) else {}
        mp4_url = str(mp4_media.get("url") or media.get("url") or "")
        results.append(_candidate(
            "tenor", "gif",
            str(item.get("content_description") or item.get("title") or query),
            preview, mp4_url, str(item.get("itemurl") or ""),
            description="Tenor reaction", width=dims[0] if len(dims) > 0 else 0,
            height=dims[1] if len(dims) > 1 else 0,
            duration=mp4_media.get("duration") or media.get("duration"), attribution="Tenor", fallback_url=preview,
            preview_video_url=mp4_url, direct_download=True,
        ))
    return results



def _search_giphy(query: str, count: int, key: str) -> list[dict[str, Any]]:
    payload = _request_json(
        "https://api.giphy.com/v1/gifs/search?" + urllib.parse.urlencode({
            "api_key": key, "q": query, "limit": str(max(1, min(50, count))),
            "rating": "pg", "lang": "ru" if re.search(r"[А-Яа-яЁё]", query) else "en",
        })
    )
    results: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        images = item.get("images") or {}
        original = images.get("original_mp4") or images.get("original") or {}
        preview_obj = images.get("preview_gif") or images.get("fixed_width_small") or images.get("downsized") or {}
        mp4_url = str(original.get("mp4") or (images.get("original") or {}).get("mp4") or "")
        gif_url = str((images.get("original") or {}).get("url") or "")
        preview = str(preview_obj.get("url") or gif_url or mp4_url)
        download = mp4_url or gif_url
        if not download:
            continue
        results.append(_candidate(
            "giphy", "gif", str(item.get("title") or query), preview, download,
            str(item.get("url") or ""), description="GIPHY reaction",
            width=original.get("width"), height=original.get("height"),
            attribution=str((item.get("user") or {}).get("display_name") or "GIPHY"),
            fallback_url=gif_url or preview, preview_video_url=mp4_url, direct_download=True,
        ))
    return results


def _pick_pixabay_video(videos: dict[str, Any]) -> dict[str, Any] | None:
    order = ("medium", "small", "large", "tiny")
    for name in order:
        row = videos.get(name)
        if isinstance(row, dict) and row.get("url"):
            return row
    return None


def _search_pixabay(query: str, count: int, key: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    common = {"key": key, "q": query, "per_page": str(max(3, min(50, count))), "safesearch": "true"}
    video_payload = _request_json("https://pixabay.com/api/videos/?" + urllib.parse.urlencode(common))
    for item in video_payload.get("hits") or []:
        if not isinstance(item, dict):
            continue
        selected = _pick_pixabay_video(item.get("videos") or {})
        if not selected:
            continue
        video_url = str(selected.get("url") or "")
        preview = str(item.get("picture_id") or "")
        if preview and not preview.startswith("http"):
            preview = f"https://i.vimeocdn.com/video/{preview}_640x360.jpg"
        results.append(_candidate(
            "pixabay_video", "video", str(item.get("tags") or query), preview, video_url,
            str(item.get("pageURL") or ""), description=item.get("tags"),
            width=selected.get("width"), height=selected.get("height"), duration=item.get("duration"),
            attribution=item.get("user"), fallback_url=preview,
            preview_video_url=video_url, direct_download=True,
        ))
    image_payload = _request_json("https://pixabay.com/api/?" + urllib.parse.urlencode({**common, "image_type": "photo"}))
    for item in image_payload.get("hits") or []:
        if not isinstance(item, dict):
            continue
        original = str(item.get("largeImageURL") or item.get("webformatURL") or "")
        preview = str(item.get("previewURL") or original)
        if original:
            results.append(_candidate(
                "pixabay_image", "image", str(item.get("tags") or query), preview, original,
                str(item.get("pageURL") or ""), description=item.get("tags"),
                width=item.get("imageWidth"), height=item.get("imageHeight"), attribution=item.get("user"),
                fallback_url=preview, direct_download=True,
            ))
    return results

def _tokens(value: str) -> set[str]:
    return {x for x in re.sub(r"[^a-z0-9]+", " ", value.lower()).split() if len(x) > 1}


def _search_imgflip(query: str, count: int) -> list[dict[str, Any]]:
    payload = _request_json("https://api.imgflip.com/get_memes")
    query_tokens = _tokens(query)
    scored: list[tuple[float, dict[str, Any]]] = []
    for pos, item in enumerate(((payload.get("data") or {}).get("memes") or [])):
        if not isinstance(item, dict) or not item.get("url"):
            continue
        name = str(item.get("name") or "")
        searchable = _tokens(name + " " + MEME_HINTS.get(name, ""))
        score = len(query_tokens & searchable) * 10 + max(0, 2 - pos / 50)
        scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    results: list[dict[str, Any]] = []
    for _, item in scored[:count]:
        mid = str(item.get("id") or "")
        url = str(item.get("url") or "")
        results.append(_candidate(
            "imgflip", "image", str(item.get("name") or "Meme template"),
            url, url, f"https://imgflip.com/memetemplate/{mid}" if mid else "https://imgflip.com/memetemplates",
            description="Популярный мем-шаблон без текста",
            width=item.get("width"), height=item.get("height"), attribution="Imgflip template",
        ))
    return results


def _normalize_query(value: str, limit: int = 220) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:limit]


def _norm(value: str) -> str:
    value = str(value or "").casefold().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value)
    return " ".join(value.split())


RU_STOPWORDS = {
    "и", "в", "на", "с", "по", "к", "у", "из", "за", "для", "это",
    "как", "что", "чтобы", "то", "же", "вот", "мне", "ты", "я", "мы",
    "покажи", "показать", "найди", "найти", "нужно", "надо", "сделай",
}
EN_STOPWORDS = {
    "a", "an", "the", "and", "or", "to", "of", "for", "in", "on",
    "with", "show", "find", "image", "photo", "video", "picture",
}


def _content_tokens(value: str) -> set[str]:
    result: set[str] = set()
    for token in _norm(value).split():
        if len(token) < 3 or token in RU_STOPWORDS or token in EN_STOPWORDS:
            continue
        result.add(token)
    return result


def _contains_any(text: str, needles: tuple[str, ...] | list[str] | set[str]) -> bool:
    normalized = _norm(text)
    return any(_norm(needle) in normalized for needle in needles if _norm(needle))


def _unique_queries(values: list[str], limit: int = 4) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = _normalize_query(value)
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
        if len(result) >= limit:
            break
    return result


def _deterministic_plan(raw_query: str, mode: str) -> dict[str, Any]:
    raw = _normalize_query(raw_query)
    lower = _norm(raw)
    queries: list[str] = [raw]
    positive_groups: list[list[str]] = []
    negative_terms: list[str] = []
    intent = "generic"

    if mode == "memes":
        for needle, replacement in RU_MEME_HINTS.items():
            if needle in lower:
                return {
                    "intent": "reaction_meme",
                    "queries": _unique_queries([replacement, raw], 3),
                    "positive_groups": [["reaction", "meme"]],
                    "negative_terms": [],
                }
        return {
            "intent": "reaction_meme",
            "queries": _unique_queries([f"funny reaction meme {raw}", raw], 3),
            "positive_groups": [["reaction", "meme"]],
            "negative_terms": [],
        }

    like_words = ("лайк", "поставь лайк", "палец вверх", "подпиш", "subscribe", "like")
    if _contains_any(lower, like_words):
        intent = "cta_like_subscribe"
        # For CTA requests the literal phrase "поставь лайк" is too vague.
        # Search for the actual visual object the editor needs.
        queries = [
            "youtube like button subscribe button overlay transparent png",
            "like subscribe animation green screen",
            "кнопка лайк и подписка прозрачный фон",
        ]
        positive_groups = [
            ["like", "лайк", "thumb", "палец"],
            ["subscribe", "подпис", "button", "кнопк"],
        ]
        negative_terms = [
            "daily offer", "монеты", "coins", "shop", "магазин", "skin",
            "скин", "brawler collection", "коллекции бойцов", "friendly battle",
            "дружеского боя", "gameplay screenshot", "скриншот игры",
            "brawl stars guide", "гайд", "box simulator", "бокс симулятор",
            "supercell account", "аккаунт supercell", "event modes", "режимы и события",
        ]
    elif _contains_any(lower, ("таймер", "отсчет", "обратный отсчет", "countdown")):
        intent = "countdown_timer"
        queries.extend([
            "countdown timer overlay transparent png",
            "15 second countdown timer green screen",
            "таймер обратного отсчета прозрачный фон",
        ])
        positive_groups = [["timer", "countdown", "таймер", "отсчет"]]
        negative_terms = ["clock product", "watch", "часы купить"]
    elif _contains_any(lower, ("qr", "куар", "кьюар", "qr код", "qr code")):
        intent = "qr_code"
        queries.extend([
            "qr code overlay transparent png",
            "scan qr code animation green screen",
            "qr код прозрачный фон",
        ])
        positive_groups = [["qr", "scan", "скан", "код"]]
    elif _contains_any(lower, ("стрелк", "указател", "arrow", "pointer")):
        intent = "pointer_arrow"
        queries.extend([
            "animated arrow pointer overlay transparent png",
            "red arrow green screen animation",
            "стрелка указатель прозрачный фон",
        ])
        positive_groups = [["arrow", "pointer", "стрелк", "указател"]]
    elif _contains_any(lower, ("галочк", "готово", "check mark", "success")):
        intent = "success_check"
        queries.extend([
            "success check mark animation transparent png",
            "green checkmark overlay green screen",
            "галочка готово прозрачный фон",
        ])
        positive_groups = [["check", "success", "галочк", "готово"]]
    elif _contains_any(lower, ("гем", "gem", "кристалл", "ящик", "box", "награ", "reward",
                                  "стар дроп", "starr drop", "бравлер", "brawler", "brawl stars")):
        intent = "brawl_specific"
        if "brawl stars" not in lower:
            queries.extend([f"Brawl Stars {raw}", f"Brawl Stars {raw} screenshot"])
        positive_groups = [["brawl", "бравл", "gem", "гем", "reward", "награ", "box", "ящик"]]
    else:
        # For named Brawl characters or game UI phrases, a concise game variant helps,
        # but only when the user actually typed a game-related word.
        if _contains_any(lower, ("лили", "lily", "спайк", "spike", "эдгар", "edgar",
                                  "шелли", "shelly", "кольт", "colt", "анджело", "angelo")):
            intent = "brawl_character"
            queries.extend([f"Brawl Stars {raw} character", f"Brawl Stars {raw} gameplay"])
            positive_groups = [["brawl", "character", "персонаж", *sorted(_content_tokens(raw))]]
        elif re.search(r"[а-яё]", raw, re.IGNORECASE):
            # Keep the exact Russian query. AI may add one concise English variant.
            positive_groups = [sorted(_content_tokens(raw))]
        else:
            positive_groups = [sorted(_content_tokens(raw))]

    return {
        "intent": intent,
        "queries": _unique_queries(queries, 4),
        "positive_groups": [g for g in positive_groups if g],
        "negative_terms": negative_terms,
    }


def _ai_query_plan(raw_query: str, spoken_text: str, visual_goal: str, mode: str,
                   api_key: str, model: str, cache_dir: Path) -> dict[str, Any] | None:
    if not api_key:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(json.dumps({
        "prompt_version": 4,
        "mode": mode,
        "q": raw_query,
        "s": _normalize_query(spoken_text, 180),
        "g": _normalize_query(visual_goal, 180),
        "m": model,
    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cache_path = cache_dir / f"query_plan_{cache_key}.json"
    cached = _read_json(cache_path, {})
    if isinstance(cached, dict) and isinstance(cached.get("queries"), list):
        return cached
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, timeout=90.0, max_retries=1)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "Ты планировщик поиска визуалов для Shorts. Главный и обязательный "
                    "источник смысла — current_query, который ввел пользователь. Нельзя "
                    "склеивать с ним всю реплику или visual_goal. Контекст используй только "
                    "чтобы понять неоднозначное слово. Сформируй 1-3 коротких поисковых "
                    "запроса: точный русский и/или точный английский. Для призыва поставить "
                    "лайк нужны кнопка like/subscribe, overlay, transparent PNG или green "
                    "screen, а не скриншоты Brawl Stars. Для имени бравлера сохрани имя и "
                    "добавь Brawl Stars. Верни JSON: {\"intent\":\"...\","
                    "\"queries\":[\"...\"],\"positive_terms\":[\"...\"],"
                    "\"negative_terms\":[\"...\"]}."
                )},
                {"role": "user", "content": json.dumps({
                    "mode": mode,
                    "current_query": raw_query,
                    "context_only_replica": _normalize_query(spoken_text, 180),
                    "context_only_visual_goal": _normalize_query(visual_goal, 180),
                }, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        parsed = json.loads(response.choices[0].message.content or "{}")
        queries = _unique_queries([str(x) for x in (parsed.get("queries") or [])], 3)
        if not queries:
            return None
        result = {
            "intent": _normalize_query(str(parsed.get("intent") or "ai"), 60),
            "queries": queries,
            "positive_terms": [
                _normalize_query(str(x), 80) for x in (parsed.get("positive_terms") or [])
                if _normalize_query(str(x), 80)
            ][:16],
            "negative_terms": [
                _normalize_query(str(x), 80) for x in (parsed.get("negative_terms") or [])
                if _normalize_query(str(x), 80)
            ][:16],
            "created_at": time.time(),
        }
        cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    except Exception:
        return None


def build_query_plan(raw_query: str, spoken_text: str, visual_goal: str, mode: str,
                     api_key: str, model: str, cache_dir: Path, use_ai: bool) -> dict[str, Any]:
    # Search planning is based ONLY on what is currently typed into the search box.
    # spoken_text and visual_goal are intentionally ignored. AI is used later only
    # to inspect/rerank found images, never to rewrite the query.
    del spoken_text, visual_goal, api_key, model, cache_dir, use_ai
    raw_query = _normalize_query(raw_query)
    if not raw_query:
        raise InternetSearchError("Поисковый запрос пустой.")
    base = _deterministic_plan(raw_query, mode)
    return {
        "intent": str(base.get("intent") or "generic"),
        "queries": _unique_queries(list(base.get("queries") or [raw_query]), 4),
        "positive_groups": [list(x) for x in (base.get("positive_groups") or [])],
        "negative_terms": list(dict.fromkeys(
            _normalize_query(x, 80)
            for x in (base.get("negative_terms") or [])
            if x
        )),
        "method": "typed_query_only_rules_v4",
    }


def _tag_results(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    for item in items:
        item["search_query_used"] = query
    return items


def _candidate_text(item: dict[str, Any]) -> str:
    # IMPORTANT: never include the query itself in the candidate text.
    # The old engine did that, so every random result looked relevant merely
    # because it had been fetched by a relevant query.
    return _norm(" ".join([
        str(item.get("title") or ""),
        str(item.get("description") or ""),
        str(item.get("source_page") or ""),
    ]))


def _lexical_score(item: dict[str, Any], plan: dict[str, Any]) -> float:
    text = _candidate_text(item)
    title = _norm(str(item.get("title") or ""))
    used_query = _norm(str(item.get("search_query_used") or ""))
    score = 0.0

    all_query_tokens = set()
    for query in plan.get("queries") or []:
        all_query_tokens.update(_content_tokens(str(query)))
    title_tokens = _content_tokens(title)
    text_tokens = _content_tokens(text)
    score += 2.6 * len(all_query_tokens & title_tokens)
    score += 0.9 * len(all_query_tokens & text_tokens)

    matched_groups = 0
    for group in plan.get("positive_groups") or []:
        normalized_group = [_norm(str(x)) for x in group if _norm(str(x))]
        if any(term in text for term in normalized_group):
            matched_groups += 1
            score += 8.0
        else:
            score -= 4.5
    for term in plan.get("negative_terms") or []:
        normalized = _norm(str(term))
        if normalized and normalized in text:
            score -= 14.0

    intent = str(plan.get("intent") or "")
    hard_relevant = True
    if intent == "cta_like_subscribe":
        tokens = _content_tokens(text)
        cta_tokens = {
            "like", "лайк", "thumb", "thumbs", "subscribe", "subscription",
            "подписка", "подписаться", "button", "кнопка", "кнопки",
            "overlay", "transparent", "animation", "green", "screen", "png",
        }
        has_cta = bool(tokens & cta_tokens)
        has_visual_asset = bool(tokens & {
            "overlay", "transparent", "animation", "green", "screen", "png", "button", "кнопка", "кнопки"
        })
        bad_topic = any(term in text for term in (
            "daily offer", "coins", "монеты", "shop", "магазин", "brawler",
            "бойц", "friendly battle", "дружеск", "box simulator",
            "бокс симулятор", "supercell account", "аккаунт supercell",
            "режимы и события", "event modes", "гайд", "guide",
        ))
        dislike_only = ("dislike" in tokens or "дизлайк" in tokens) and not bool(
            tokens & {"subscribe", "subscription", "подписка", "button", "кнопка", "overlay"}
        )
        hard_relevant = has_cta and not dislike_only and not (bad_topic and not has_visual_asset)
        if has_cta:
            score += 12.0
        if has_visual_asset:
            score += 10.0
        if bad_topic:
            score -= 35.0
        if dislike_only:
            score -= 50.0
        if not hard_relevant:
            score -= 100.0
    elif intent == "countdown_timer":
        if not any(term in text for term in ("timer", "countdown", "таймер", "отсчет")):
            score -= 35.0
    elif intent == "qr_code":
        if "qr" not in text and "куар" not in text:
            score -= 35.0
    elif intent in {"brawl_specific", "brawl_character"}:
        if "brawl" in text or "бравл" in text:
            score += 8.0
        else:
            score -= 8.0

    width = int(item.get("width") or 0)
    height = int(item.get("height") or 0)
    if width and height:
        if width >= 500 and height >= 500:
            score += 1.5
        if width < 250 or height < 250:
            score -= 3.0

    provider = str(item.get("provider") or "")
    if provider in {"brave_images", "brave_video"}:
        score += 1.0
    elif provider.startswith("pexels") or provider.startswith("pixabay"):
        score += 0.5

    # Results produced by the most specific generated query get a small bonus.
    if used_query and used_query != _norm(str((plan.get("queries") or [""])[0])):
        score += 1.0
    item["relevance_score"] = round(score, 3)
    item["matched_groups"] = matched_groups
    item["hard_relevant"] = bool(hard_relevant)
    return score


def _dedupe_and_rank(items: list[dict[str, Any]], plan: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    best_by_url: dict[str, tuple[float, dict[str, Any]]] = {}
    for item in items:
        url = str(item.get("download_url") or item.get("preview_url") or "").split("?")[0]
        key = re.sub(r"\W+", "", url.casefold()) or str(item.get("id") or "")
        score = _lexical_score(item, plan)
        previous = best_by_url.get(key)
        if previous is None or score > previous[0]:
            best_by_url[key] = (score, item)
    ranked = sorted(best_by_url.values(), key=lambda pair: pair[0], reverse=True)
    intent = str(plan.get("intent") or "")
    if intent in {"cta_like_subscribe", "countdown_timer", "qr_code", "pointer_arrow", "success_check"}:
        # For concrete overlay requests, showing nothing is better than showing
        # unrelated Brawl Stars screenshots.
        return [
            item for score, item in ranked
            if score >= 8.0 and bool(item.get("hard_relevant", True))
        ][:limit]
    strong = [item for score, item in ranked if score >= 2.0]
    if strong:
        return strong[:limit]
    return [item for _, item in ranked[:min(limit, 4)]]


def _title_fingerprint(value: str) -> str:
    # Only exact normalized titles are duplicates. Token-set fingerprints were
    # too aggressive and collapsed a whole page of similar but distinct results.
    return _norm(value)[:180]


def _diversify_results(items: list[dict[str, Any]], max_per_provider: int, limit: int) -> list[dict[str, Any]]:
    """Keep search results varied instead of showing ten copies from one site."""
    selected: list[dict[str, Any]] = []
    provider_counts: dict[str, int] = {}
    seen_titles: set[str] = set()
    deferred: list[dict[str, Any]] = []
    for item in items:
        provider = str(item.get("provider") or "unknown")
        fingerprint = _title_fingerprint(str(item.get("title") or ""))
        if fingerprint and fingerprint in seen_titles:
            continue
        if provider_counts.get(provider, 0) >= max_per_provider:
            deferred.append(item)
            continue
        selected.append(item)
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        if fingerprint:
            seen_titles.add(fingerprint)
        if len(selected) >= limit:
            return selected
    for item in deferred:
        fingerprint = _title_fingerprint(str(item.get("title") or ""))
        if fingerprint and fingerprint in seen_titles:
            continue
        selected.append(item)
        if fingerprint:
            seen_titles.add(fingerprint)
        if len(selected) >= limit:
            break
    return selected


def _ensure_media_mix(
    primary: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    *,
    limit: int,
    mode: str,
) -> list[dict[str, Any]]:
    """Keep actual moving candidates available for strict post-download review."""
    desired_moving = min(4 if mode == "memes" else 3, max(1, limit // 3))
    moving = [
        item for item in ranked
        if str(item.get("media_type") or "") in {"video", "gif"}
    ]
    result = list(primary[:limit])
    seen = {str(item.get("id") or "") for item in result}
    present = sum(
        1 for item in result
        if str(item.get("media_type") or "") in {"video", "gif"}
    )
    for item in moving:
        if present >= desired_moving:
            break
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen:
            continue
        if len(result) >= limit:
            removable = next(
                (
                    index for index in range(len(result) - 1, -1, -1)
                    if str(result[index].get("media_type") or "image") == "image"
                ),
                len(result) - 1,
            )
            removed = result.pop(removable)
            seen.discard(str(removed.get("id") or ""))
        result.append(item)
        seen.add(item_id)
        present += 1
    return result[:limit]


def _vision_rerank(items: list[dict[str, Any]], plan: dict[str, Any], api_key: str,
                   model: str, limit: int, threshold: float = 0.50) -> tuple[list[dict[str, Any]], bool]:
    if not api_key or not items:
        return items[:limit], False
    sample = items[:min(10, len(items))]
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, timeout=90.0, max_retries=1)
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": (
                "Отранжируй фото, GIF и видео-превью для монтажа Shorts. Цель: "
                f"{json.dumps({'intent': plan.get('intent'), 'queries': plan.get('queries')}, ensure_ascii=False)}. "
                "Оцени реальное содержимое картинки, а не только название. Для призыва "
                "поставить лайк нужны видимые кнопки like/subscribe, палец вверх, CTA overlay "
                "или прозрачная анимация. Случайные скриншоты, другой объект с похожим "
                "словом и неверную сущность отвергай: sprays не равны Starr Drops, часы "
                "на здании не равны игровому таймеру. При одинаковой точности предпочитай "
                "движущееся видео или GIF статичной картинке. Верни JSON "
                "{\"ranked\":[{\"id\":\"...\",\"score\":0-100,\"reason\":\"...\"}]}"
            ),
        }]
        for item in sample:
            content.append({
                "type": "text",
                "text": (
                    f"ID={item.get('id')} TYPE={item.get('media_type')} "
                    f"TITLE={item.get('title')} DESCRIPTION={item.get('description')}"
                ),
            })
            content.append({
                "type": "image_url",
                "image_url": {"url": str(item.get("preview_url") or item.get("download_url")), "detail": "low"},
            })
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        parsed = json.loads(response.choices[0].message.content or "{}")
        score_by_id: dict[str, tuple[float, str]] = {}
        for row in parsed.get("ranked") or []:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or "")
            try:
                score = max(0.0, min(100.0, float(row.get("score") or 0)))
            except (TypeError, ValueError):
                score = 0.0
            if rid:
                score_by_id[rid] = (score, _normalize_query(str(row.get("reason") or ""), 180))
        for item in sample:
            vscore, reason = score_by_id.get(str(item.get("id") or ""), (0.0, ""))
            item["vision_relevance"] = round(vscore / 100.0, 3)
            item["vision_reason"] = reason
            item["combined_relevance"] = round(float(item.get("relevance_score") or 0) + vscore * 0.22, 3)
        sample.sort(key=lambda x: float(x.get("combined_relevance") or x.get("relevance_score") or 0), reverse=True)
        accepted = [x for x in sample if float(x.get("vision_relevance") or 0) >= threshold]
        return (accepted or sample[:min(4, len(sample))])[:limit], True
    except Exception:
        return items[:limit], False


class InternetSearchService:
    def __init__(self, renderer_root: Path, api_key: str, model: str, project_root: Path) -> None:
        self.renderer_root = renderer_root
        self.api_key = api_key
        self.model = model
        self.cache_dir = project_root / "internet_search_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        ensure_settings(renderer_root)

    def status(self) -> dict[str, Any]:
        status = provider_status(self.renderer_root)
        status["engine_version"] = ENGINE_VERSION
        return status

    def search(self, mode: str, raw_query: str, spoken_text: str, visual_goal: str,
               limit: int = 10, use_ai: bool = True, refresh: bool = False) -> dict[str, Any]:
        mode = "memes" if mode == "memes" else "internet"
        settings = load_settings(self.renderer_root)
        limit = max(1, min(30, int(limit or settings.get("result_limit") or 18)))
        plan = build_query_plan(raw_query, spoken_text, visual_goal, mode,
                                self.api_key, self.model, self.cache_dir, use_ai)
        queries = list(plan.get("queries") or [])
        cache_key = hashlib.sha256(json.dumps({
            "version": 372,
            "mode": mode,
            "plan": plan,
            "limit": limit,
            "providers": settings.get("providers"),
            "use_ai": bool(use_ai),
        }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cache_path = self.cache_dir / f"results_{cache_key}.json"
        max_age = max(0.0, float(settings.get("cache_hours") or 24)) * 3600
        if not refresh and cache_path.is_file() and time.time() - cache_path.stat().st_mtime <= max_age:
            cached = _read_json(cache_path, {})
            if isinstance(cached.get("results"), list):
                cached["cache_hit"] = True
                return cached

        started_at = time.perf_counter()
        providers = settings.get("providers") or {}
        candidate_pool = max(limit, min(60, int(settings.get("candidate_pool") or 30)))
        vision_candidates = max(limit, min(20, int(settings.get("vision_candidates") or 10)))
        max_per_provider = max(2, min(12, int(settings.get("max_per_provider") or 6)))
        jobs: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = []
        brave = _secret(self.renderer_root, settings, "brave_api_key_file")
        pexels = _secret(self.renderer_root, settings, "pexels_api_key_file")
        tenor = _secret(self.renderer_root, settings, "tenor_api_key_file")
        giphy = _secret(self.renderer_root, settings, "giphy_api_key_file")
        pixabay = _secret(self.renderer_root, settings, "pixabay_api_key_file")
        primary_queries = queries[:2] if queries else []

        if providers.get("brave_images", True) and brave:
            for pos, current_query in enumerate(primary_queries):
                count = candidate_pool if pos == 0 else max(16, candidate_pool // 2)
                jobs.append((f"Brave Images[{pos + 1}]", lambda q=current_query, c=count: _tag_results(
                    _search_brave(q, c, brave, str(settings.get("country") or "ALL"), "auto"), q)))
        if providers.get("brave_videos", True) and brave:
            for pos, current_query in enumerate(primary_queries):
                video_query = current_query if mode == "internet" else f"{current_query} reaction meme"
                jobs.append((f"Brave Video[{pos + 1}]", lambda q=video_query: _tag_results(
                    _search_brave_videos(q, max(10, candidate_pool // 2), brave,
                                         str(settings.get("country") or "ALL"), "auto", 0), q)))
        if providers.get("youtube", True) and mode == "internet" and primary_queries:
            youtube_query = primary_queries[0]
            jobs.append(("YouTube", lambda q=youtube_query: _tag_results(
                _search_youtube(q, max(6, candidate_pool // 4)), q)))
        if providers.get("pexels", True) and pexels and queries:
            jobs.append(("Pexels", lambda q=queries[0]: _tag_results(
                _search_pexels(q, max(14, candidate_pool // 2), pexels), q)))
        if providers.get("pixabay", True) and pixabay and queries:
            jobs.append(("Pixabay", lambda q=queries[0]: _tag_results(
                _search_pixabay(q, max(14, candidate_pool // 2), pixabay), q)))
        if providers.get("wikimedia", True) and queries:
            jobs.append(("Wikimedia", lambda q=queries[0]: _tag_results(
                _search_wikimedia(q, max(10, candidate_pool // 3)), q)))
        if mode == "memes":
            if providers.get("tenor", True) and tenor:
                for pos, current_query in enumerate(primary_queries):
                    jobs.append((f"Tenor[{pos + 1}]", lambda q=current_query: _tag_results(
                        _search_tenor(q, max(16, candidate_pool // 2), tenor,
                                      str(settings.get("tenor_client_key") or "shortsai_v2")), q)))
            if providers.get("giphy", True) and giphy and queries:
                jobs.append(("GIPHY", lambda q=queries[0]: _tag_results(
                    _search_giphy(q, max(16, candidate_pool // 2), giphy), q)))
            if providers.get("imgflip_templates", True) and queries:
                jobs.append(("Imgflip", lambda q=queries[0]: _tag_results(
                    _search_imgflip(q, candidate_pool), q)))

        collected: list[dict[str, Any]] = []
        errors: list[str] = []
        provider_counts: dict[str, int] = {}
        if jobs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(jobs))) as pool:
                futures = {pool.submit(fn): label for label, fn in jobs}
                for future in concurrent.futures.as_completed(futures):
                    label = futures[future]
                    try:
                        rows = future.result()
                        collected.extend(rows)
                        provider_counts[label] = len(rows)
                    except Exception as exc:
                        errors.append(f"{label}: {exc}")

        ranked = _dedupe_and_rank(collected, plan, max(candidate_pool, vision_candidates))
        ranked = _diversify_results(ranked, max_per_provider=max_per_provider, limit=max(candidate_pool, vision_candidates))
        # In meme mode moving media must be above static templates. For normal internet
        # search, keep mixed results but still give direct videos a small priority.
        media_bonus = {"video": 3.0, "gif": 2.5, "image": 0.0} if mode == "memes" else {"video": 1.2, "gif": 0.8, "image": 0.0}
        for item in ranked:
            item["mixed_media_score"] = float(item.get("relevance_score") or 0) + media_bonus.get(str(item.get("media_type") or "image"), 0.0)
        ranked.sort(key=lambda x: float(x.get("mixed_media_score") or 0), reverse=True)
        vision_threshold = max(0.0, min(1.0, float(settings.get("vision_threshold") or 0.50)))
        results, vision_used = _vision_rerank(
            ranked[:vision_candidates], plan, self.api_key if use_ai else "", self.model, limit,
            threshold=vision_threshold,
        )
        if not vision_used:
            results = ranked[:limit]
        results = _ensure_media_mix(
            results,
            ranked,
            limit=limit,
            mode=mode,
        )
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        payload = {
            "ok": True,
            "engine_version": ENGINE_VERSION,
            "mode": mode,
            "raw_query": _normalize_query(raw_query),
            "resolved_query": " | ".join(queries),
            "queries": queries,
            "query_method": str(plan.get("method") or "rules_v3"),
            "intent": str(plan.get("intent") or "generic"),
            "vision_rerank": vision_used,
            "candidate_pool": len(collected),
            "ranked_pool": len(ranked),
            "provider_counts": provider_counts,
            "media_counts": {
                "video": sum(1 for x in results if x.get("media_type") == "video"),
                "gif": sum(1 for x in results if x.get("media_type") == "gif"),
                "image": sum(1 for x in results if x.get("media_type") == "image"),
            },
            "elapsed_ms": elapsed_ms,
            "results": results,
            "errors": errors,
            "cache_hit": False,
            "provider_status": self.status(),
        }
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
