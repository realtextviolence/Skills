"""Extract YouTube metadata and transcript as JSON for an agent caller

Contract: stdout is always JSON Exit 0 when metadata was retrieved (transcript
may still be null with a reason) Exit 1 only when metadata itself is
unreachable Agent-readable error codes live in the `error` or
`transcript_error` strings, prefixed by a machine code
"""

from __future__ import annotations

import html
import json
import re
import sys
import traceback
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse


# Checked 2026-04-17 against a live Android client capture When YouTube
# rotates the protocol these three constants are the first to update
PLAYER_URL = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
CLIENT_VERSION = "20.10.38"
USER_AGENT = f"com.google.android.youtube/{CLIENT_VERSION} (Linux; U; Android 14)"

REQUEST_TIMEOUT = 15
PREFERRED_LANGUAGES = ("ru", "en")

_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


class FatalError(Exception):
    """Metadata could not be obtained; nothing useful to return"""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail


class TranscriptError(Exception):
    """Transcript retrieval failed but metadata is still valid"""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail


def extract_video_id(raw: str) -> str | None:
    """Accept a bare 11-char ID or any of YouTube's public URL shapes"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if _is_valid_id(raw):
        return raw

    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = parsed.netloc.lower()
    path = parsed.path or ""

    if host.endswith("youtu.be"):
        return _first_path_segment(path)

    if "youtube.com" in host or "youtube-nocookie.com" in host:
        if path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [None])[0]
            return candidate if candidate and _is_valid_id(candidate) else None
        for prefix in ("/embed/", "/shorts/", "/live/", "/v/"):
            if path.startswith(prefix):
                return _first_path_segment(path[len(prefix):])
    return None


def _first_path_segment(path: str) -> str | None:
    segment = path.lstrip("/").split("/", 1)[0]
    return segment if _is_valid_id(segment) else None


def _is_valid_id(candidate: str) -> bool:
    return bool(_VIDEO_ID_RE.fullmatch(candidate))


def fetch_player(video_id: str) -> dict:
    """Ask the Android InnerTube endpoint for the full player response"""
    body = json.dumps({
        "videoId": video_id,
        "context": {
            "client": {
                "clientName": "ANDROID",
                "clientVersion": CLIENT_VERSION,
                "hl": "en",
                "gl": "US",
            }
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        PLAYER_URL,
        data=body,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FatalError("request_failed", str(e))
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise FatalError("invalid_response", str(e))


def check_playability(payload: dict) -> None:
    """Bail out early if YouTube refuses to serve the video"""
    status_block = payload.get("playabilityStatus") or {}
    status = status_block.get("status")
    if status and status != "OK":
        detail = status_block.get("reason") or status_block.get("errorScreen") or "not playable"
        if isinstance(detail, dict):
            detail = json.dumps(detail, ensure_ascii=False)
        raise FatalError(f"playability_{status}", str(detail))


def build_metadata(video_id: str, payload: dict) -> dict:
    details = payload.get("videoDetails") or {}
    try:
        duration = int(details.get("lengthSeconds") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "video_id": video_id,
        "title": details.get("title"),
        "author": details.get("author"),
        "duration": duration,
        "description": _full_description(payload),
        "transcript": None,
        "transcript_language": None,
        "transcript_error": None,
    }


def _full_description(payload: dict) -> str | None:
    """Prefer the microformat block; videoDetails.shortDescription is truncated"""
    block = (
        payload.get("microformat", {})
        .get("playerMicroformatRenderer", {})
        .get("description")
    )
    if isinstance(block, dict):
        full = block.get("simpleText")
        if full:
            return full
    details = payload.get("videoDetails") or {}
    return details.get("shortDescription")


def pick_caption_track(payload: dict) -> dict:
    """Choose a track by PREFERRED_LANGUAGES priority, else raise"""
    tracks = (
        payload.get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
        .get("captionTracks")
        or []
    )
    if not tracks:
        raise TranscriptError("no_subtitles", "no captionTracks in response")

    by_lang: dict[str, dict] = {}
    for track in tracks:
        code = (track.get("languageCode") or "").split("-", 1)[0].lower()
        by_lang.setdefault(code, track)

    for lang in PREFERRED_LANGUAGES:
        if lang in by_lang:
            return by_lang[lang]

    available = ",".join(sorted(by_lang)) or "none"
    raise TranscriptError("no_preferred_language", f"available={available}")


def fetch_transcript_xml(track: dict) -> bytes:
    url = track.get("baseUrl")
    if not url:
        raise TranscriptError("missing_baseurl", "track has no baseUrl")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TranscriptError("fetch_failed", str(e))


def parse_transcript(xml_bytes: bytes, duration: int) -> list[str]:
    """Turn a modern timedtext <p t="ms"> document into display lines"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise TranscriptError("parse_failed", str(e))

    entries: list[tuple[int, str]] = []
    max_ms = 0
    for p in root.iter("p"):
        t_attr = p.get("t")
        if t_attr is None:
            continue
        try:
            ms = int(t_attr)
        except ValueError:
            continue
        raw_text = html.unescape("".join(p.itertext()))
        text = " ".join(raw_text.split())
        if not text:
            continue
        entries.append((ms, text))
        if ms > max_ms:
            max_ms = ms

    if not entries:
        raise TranscriptError("empty_after_parse", "no usable <p t=...> elements")

    use_hours = duration >= 3600 or max_ms >= 3_600_000
    return [f"[{_format_timestamp(ms, use_hours)}] {text}" for ms, text in entries]


def _format_timestamp(ms: int, use_hours: bool) -> str:
    total = max(ms, 0) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if use_hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours * 60 + minutes:02d}:{seconds:02d}"


def load_transcript(payload: dict, result: dict) -> None:
    """Fill transcript fields in-place; transcript errors are non-fatal"""
    try:
        track = pick_caption_track(payload)
        xml_bytes = fetch_transcript_xml(track)
        lines = parse_transcript(xml_bytes, result["duration"])
    except TranscriptError as e:
        result["transcript_error"] = f"{e.code}: {e.detail}"
        return
    result["transcript"] = "\n".join(lines)
    result["transcript_language"] = (track.get("languageCode") or "").split("-", 1)[0].lower()


def run(raw: str) -> tuple[dict, int]:
    video_id = extract_video_id(raw)
    if not video_id:
        return {"error": f"invalid_video_id: {raw!r}"}, 1
    try:
        payload = fetch_player(video_id)
        check_playability(payload)
    except FatalError as e:
        return {"error": f"{e.code}: {e.detail}"}, 1
    result = build_metadata(video_id, payload)
    load_transcript(payload, result)
    return result, 0


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "invalid_video_id: missing argument"}, ensure_ascii=False))
        sys.exit(1)
    payload, code = run(sys.argv[1])
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


if __name__ == "__main__":
    # Expected network/parsing failures are already wrapped into JSON by run()
    # Anything escaping here is a programmer bug — dump a traceback for
    # debugging and exit 2 so callers can tell it apart from contract errors
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        sys.exit(2)
