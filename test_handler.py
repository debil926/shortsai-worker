"""Minimal tests for the RunPod worker. Not part of the main project's
pytest suite (the main venv doesn't have the `runpod` package installed,
and shouldn't need to) - run directly: `python test_handler.py` from
inside runpod_worker/, or via pytest with `runpod` installed.

Live-verified separately (search -> download -> trim -> base64 return,
against a real YouTube result, real ffmpeg trim, real output file
checked with ffprobe and a decoded frame) before this was written - these
tests cover the parts that don't need network/ffmpeg at all.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest import mock

# Stub runpod so importing handler.py doesn't require the package or
# start a serverless listener - matches how this was verified live.
if "runpod" not in sys.modules:
    fake_runpod = types.ModuleType("runpod")
    fake_serverless = types.ModuleType("runpod.serverless")
    fake_serverless.start = lambda config: None
    fake_runpod.serverless = fake_serverless
    sys.modules["runpod"] = fake_runpod
    sys.modules["runpod.serverless"] = fake_serverless

sys.path.insert(0, str(Path(__file__).resolve().parent))

import handler  # noqa: E402


def test_empty_query_returns_an_error_without_searching():
    result = handler._search_and_fetch({"query": ""}, Path("."))
    assert "error" in result


def test_skips_candidates_that_are_not_video_or_too_long():
    calls = []

    def fake_search(query, count):
        return [
            {"media_type": "image", "duration": 10},
            {"media_type": "video", "duration": 999},
            {"media_type": "video", "duration": 8, "title": "ok"},
        ]

    def fake_ingest(self, remote, **kwargs):
        calls.append(remote)
        raise handler.InternetLibraryError("stop here, this test only checks filtering")

    with mock.patch.object(handler, "_search_youtube", fake_search), \
         mock.patch.object(handler.InternetLibrary, "ingest", fake_ingest):
        handler._search_and_fetch(
            {"query": "test", "max_duration": 60, "max_candidates": 3},
            Path("."),
        )

    assert len(calls) == 1
    assert calls[0]["duration"] == 8


def test_respects_max_candidates_even_when_more_results_exist():
    calls = []

    def fake_search(query, count):
        return [{"media_type": "video", "duration": 5, "title": f"v{i}"} for i in range(5)]

    def fake_ingest(self, remote, **kwargs):
        calls.append(remote)
        raise handler.InternetLibraryError("stop here, this test only checks the attempt cap")

    with mock.patch.object(handler, "_search_youtube", fake_search), \
         mock.patch.object(handler.InternetLibrary, "ingest", fake_ingest):
        handler._search_and_fetch(
            {"query": "test", "max_duration": 60, "max_candidates": 2},
            Path("."),
        )

    assert len(calls) == 2


if __name__ == "__main__":
    test_empty_query_returns_an_error_without_searching()
    test_skips_candidates_that_are_not_video_or_too_long()
    test_respects_max_candidates_even_when_more_results_exist()
    print("All tests passed.")
