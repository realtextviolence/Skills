import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PLAYER_URL = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
CLIENT_VERSION = "20.10.38"
USER_AGENT = f"com.google.android.youtube/{CLIENT_VERSION} (Linux; U; Android 14)"
REQUEST_TIMEOUT = 15


def make_request(url, *, data=None, headers=None, method="GET"):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
        return response.read()


def extract_video_id(url):
    candidate = url.strip()
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", candidate):
        return candidate

    parsed = urllib.parse.urlparse(candidate)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")

    if host in {"youtu.be", "www.youtu.be"}:
        video_id = path.split("/", 1)[0]
        return video_id if re.fullmatch(r"[0-9A-Za-z_-]{11}", video_id) else None

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        query = urllib.parse.parse_qs(parsed.query)
        if "v" in query:
            video_id = query["v"][0]
            return video_id if re.fullmatch(r"[0-9A-Za-z_-]{11}", video_id) else None

        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in {"embed", "shorts", "live"}:
            video_id = parts[1]
            return video_id if re.fullmatch(r"[0-9A-Za-z_-]{11}", video_id) else None

    return None


def format_time(seconds):
    try:
        seconds = float(seconds)
        total_seconds = int(seconds)
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        if hours > 0:
            return f"[{hours:02d}:{minutes:02d}:{secs:02d}]"
        return f"[{minutes:02d}:{secs:02d}]"
    except (ValueError, TypeError):
        return "[00:00]"


def parse_transcript_xml(xml_content):
    lines = []
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return "Failed to parse subtitles XML."

    for node in root.findall(".//text"):
        start = node.attrib.get("start")
        if not start:
            continue

        clean_text = "".join(node.itertext())
        clean_text = html.unescape(clean_text).replace("\n", " ").strip()
        if clean_text:
            lines.append(f"{format_time(start)} {clean_text}")

    if not lines:
        for node in root.findall(".//p"):
            t_ms = node.attrib.get("t")
            if not t_ms:
                continue

            clean_text = "".join(node.itertext())
            clean_text = html.unescape(clean_text).replace("\n", " ").strip()
            if clean_text:
                start_sec = int(t_ms) / 1000
                lines.append(f"{format_time(start_sec)} {clean_text}")

    return "\n".join(lines) if lines else "Transcript is empty after parsing."


def pick_caption_track(captions):
    for language_code in ("ru", "en"):
        track = next(
            (track for track in captions if track.get("languageCode") == language_code),
            None,
        )
        if track:
            return track
    return captions[0]


def format_duration(seconds):
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return (
        f"{hours:02d}:{minutes:02d}:{secs:02d}"
        if hours > 0
        else f"{minutes:02d}:{secs:02d}"
    )


def get_youtube_data(video_id) -> dict:
    payload = {
        "context": {
            "client": {
                "clientName": "ANDROID",
                "clientVersion": CLIENT_VERSION,
            },
        },
        "videoId": video_id,
    }

    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    try:
        raw_response = make_request(
            PLAYER_URL,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        data = json.loads(raw_response.decode())

        status = data.get("playabilityStatus", {}).get("status")
        if status != "OK":
            reason = data.get("playabilityStatus", {}).get("reason", "Unknown error")
            raise ValueError(f"YouTube: {status} ({reason})")

        details = data.get("videoDetails", {})
        metadata = {
            "title": details.get("title", "N/A"),
            "author": details.get("author", "N/A"),
            "length": int(details.get("lengthSeconds", 0)),
            "description": details.get("shortDescription", "N/A"),
        }

        captions = (
            data.get("captions", {})
            .get("playerCaptionsTracklistRenderer", {})
            .get("captionTracks", [])
        )
        if not captions:
            metadata["transcript"] = "No subtitles available for this video."
            return metadata

        track = pick_caption_track(captions)

        sub_url = track.get("baseUrl")
        if not sub_url:
            metadata["transcript"] = "Subtitles URL is missing."
            return metadata

        xml_content = make_request(sub_url, headers={"User-Agent": USER_AGENT}).decode(
            "utf-8"
        )
        metadata["transcript"] = parse_transcript_xml(xml_content)

        return metadata

    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"YouTube error: {e}") from e


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python youtube_helper.py <URL>")
        sys.exit(1)

    url = sys.argv[1]
    video_id = extract_video_id(url)

    if not video_id:
        print("Error: Failed to extract video ID from URL.")
        sys.exit(1)

    try:
        result = get_youtube_data(video_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(e)
        sys.exit(1)
