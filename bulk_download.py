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

import concurrent.futures
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend_lite.internet_library import InternetLibrary, InternetLibraryError
from backend_lite.internet_search import InternetSearchError, _search_youtube
from handler import _probe_duration, _trim, MAX_DURATION_DEFAULT, DOWNLOAD_TIMEOUT_SECONDS

CLIP_DURATION_SECONDS = 6.0  # a few seconds of headroom for BestMoment to pick from later
MAX_CANDIDATES_PER_QUERY = 3
# A residential proxy makes each download slow (real home-connection latency,
# not RunPod's own fast link) - one at a time made the 96-query batch take
# hours. These are network-bound, not CPU-bound, so run several in parallel.
# Each worker gets its own InternetLibrary pointed at a separate subfolder to
# avoid concurrent writes to one shared index.json.
PARALLEL_WORKERS = 10

POT_SERVER_SCRIPT = Path("/shortsai-worker/bgutil-ytdlp-pot-provider/server/build/main.js")
POT_SERVER_PORT = 4416
POT_SERVER_LOG = Path("/shortsai-worker/pot_server.log")


def _pot_server_alive() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{POT_SERVER_PORT}/ping", timeout=2)
        return True
    except Exception:
        return False


def ensure_pot_server() -> None:
    """The PO-token server has no supervisor, so a terminal/session reset
    silently kills it and every YouTube download starts failing with
    "No title found in player responses" again. Self-heal on every run
    instead of depending on someone noticing and restarting it by hand."""
    if _pot_server_alive():
        print("POT server: already running.")
        return
    if not POT_SERVER_SCRIPT.is_file():
        print(f"POT server: script not found at {POT_SERVER_SCRIPT} - skipping "
              f"(YouTube downloads may hit auth blocks without it).")
        return
    with open(POT_SERVER_LOG, "ab") as log_file:
        subprocess.Popen(
            ["node", str(POT_SERVER_SCRIPT.name)],
            cwd=str(POT_SERVER_SCRIPT.parent),
            stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    for _ in range(10):
        time.sleep(1)
        if _pot_server_alive():
            print("POT server: started.")
            return
    print("POT server: did not respond after starting - continuing anyway, check pot_server.log.")


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

    ensure_pot_server()

    queries = [
        line.strip() for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    libraries = [InternetLibrary(out_dir / f"_work{i}") for i in range(PARALLEL_WORKERS)]

    manifest_path = out_dir / "manifest.json"
    manifest: list[dict] = []
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = []
    already_ok = {row["query"] for row in manifest if isinstance(row, dict) and row.get("ok")}
    manifest_lock = threading.Lock()

    def run_one(i: int, query: str) -> None:
        library = libraries[i % PARALLEL_WORKERS]
        t0 = time.time()
        result = download_one(query, out_dir, library)
        result["elapsed_seconds"] = round(time.time() - t0, 1)
        status = "OK" if result["ok"] else "FAIL"
        print(f"[{i}/{len(queries)}] {status} ({result['elapsed_seconds']}s) — {query}"
              + ("" if result["ok"] else f" :: {result.get('error', '')[:200]}"))
        with manifest_lock:
            manifest[:] = [row for row in manifest if not (isinstance(row, dict) and row.get("query") == query)]
            manifest.append(result)
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
            )

    pending = [(i, q) for i, q in enumerate(queries, start=1) if q not in already_ok]
    for i, query in enumerate(queries, start=1):
        if query in already_ok:
            print(f"[{i}/{len(queries)}] SKIP (already have it) — {query}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
        list(pool.map(lambda pair: run_one(*pair), pending))

    ok_count = sum(1 for r in manifest if r["ok"])
    print(f"\nDone: {ok_count}/{len(queries)} clips downloaded to {out_dir}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
