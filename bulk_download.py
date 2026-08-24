"""Batch version of handler.py's search+download+trim, meant to run once
on a rented RunPod Pod's fast connection (not as a serverless request/
response worker): reads a list of queries, downloads a short clip for
each, and writes them all to one output folder plus a manifest.json
describing what each clip is - ready to zip and pull back to a slow home
connection as one bounded batch instead of per-video during generation.

Usage (inside the pod, from this directory):
    python bulk_download.py queries.txt out/
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend_lite.internet_library import InternetLibrary, InternetLibraryError
from backend_lite.internet_search import InternetSearchError, _search_youtube
from handler import _probe_duration, _trim, MAX_DURATION_DEFAULT, DOWNLOAD_TIMEOUT_SECONDS

CLIP_DURATION_SECONDS = 6.0  # a few seconds of headroom for BestMoment to pick from later
MAX_CANDIDATES_PER_QUERY = 3


def download_one(query: str, out_dir: Path, library: InternetLibrary) -> dict:
    try:
        results = _search_youtube(query, MAX_CANDIDATES_PER_QUERY * 2)
    except InternetSearchError as exc:
        return {"query": query, "ok": False, "error": f"search failed: {exc}"}

    attempts = 0
    last_error = ""
    for remote in results:
        if attempts >= MAX_CANDIDATES_PER_QUERY:
            break
        if str(remote.get("media_type") or "") != "video":
            continue
        duration = float(remote.get("duration") or 0)
        if duration <= 0 or duration > MAX_DURATION_DEFAULT:
            continue
        attempts += 1
        try:
            entry, _cache_hit = library.ingest(
                remote, search_query=query, beat_text=query,
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
        trim_duration = min(CLIP_DURATION_SECONDS, real_duration)
        safe_name = "".join(c if c.isalnum() else "_" for c in query)[:60]
        output = out_dir / f"{safe_name}_{attempts}.mp4"
        try:
            _trim(source_path, output, start=0.0, duration=trim_duration)
        except Exception as exc:  # noqa: BLE001 - keep the batch going on any one failure
            last_error = f"trim failed: {exc}"
            continue

        return {
            "query": query,
            "ok": True,
            "file": output.name,
            "duration": trim_duration,
            "title": str(remote.get("title") or ""),
            "source_page": str(entry.get("source_page") or remote.get("source_page") or ""),
        }

    return {"query": query, "ok": False, "error": f"no usable candidate after {attempts} attempt(s); last_error={last_error}"}


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python bulk_download.py queries.txt out_dir/")
        return 1

    queries = [
        line.strip() for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    library = InternetLibrary(out_dir / "_work")

    manifest_path = out_dir / "manifest.json"
    manifest: list[dict] = []
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = []
    already_ok = {row["query"] for row in manifest if isinstance(row, dict) and row.get("ok")}

    for i, query in enumerate(queries, start=1):
        if query in already_ok:
            print(f"[{i}/{len(queries)}] SKIP (already have it) — {query}")
            continue
        t0 = time.time()
        result = download_one(query, out_dir, library)
        result["elapsed_seconds"] = round(time.time() - t0, 1)
        manifest = [row for row in manifest if not (isinstance(row, dict) and row.get("query") == query)]
        manifest.append(result)
        status = "OK" if result["ok"] else "FAIL"
        print(f"[{i}/{len(queries)}] {status} ({result['elapsed_seconds']}s) — {query}"
              + ("" if result["ok"] else f" :: {result.get('error', '')[:200]}"))
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    ok_count = sum(1 for r in manifest if r["ok"])
    print(f"\nDone: {ok_count}/{len(queries)} clips downloaded to {out_dir}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
