import html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PLAYER_URL = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
CLIENT_VERSION = "20.10.38"
USER_AGENT = f"com.google.android.youtube/{CLIENT_VERSION} (Linux; U; Android 14)"
REQUEST_TIMEOUT = 15
VIDEO_ID_PATTERN = r"[0-9A-Za-z_-]{11}"


def is_valid_video_id(value):
    return bool(re.fullmatch(VIDEO_ID_PATTERN, value))


def make_request(url, *, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
        return response.read()


def extract_video_id(url):
    candidate = url.strip()
    if is_valid_video_id(candidate):
        return candidate

    parsed = urllib.parse.urlparse(candidate)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")

    if host in {"youtu.be", "www.youtu.be"}:
        video_id = path.split("/", 1)[0]
        return video_id if is_valid_video_id(video_id) else None

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        query = urllib.parse.parse_qs(parsed.query)
        if "v" in query:
            video_id = query["v"][0]
            return video_id if is_valid_video_id(video_id) else None

        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in {"embed", "shorts", "live"}:
            video_id = parts[1]
            return video_id if is_valid_video_id(video_id) else None

    return None


def format_time(seconds):
    try:
        return f"[{format_duration(seconds)}]"
    except (ValueError, TypeError):
        return "[00:00]"


def _node_text(node):
    text = "".join(node.itertext())
    return html.unescape(text).replace("\n", " ").strip()


def parse_transcript_xml(xml_content):
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return "Failed to parse subtitles XML."

    lines = []
    for node in root.findall(".//text"):
        start = node.attrib.get("start")
        text = _node_text(node)
        if start and text:
            lines.append(f"{format_time(start)} {text}")

    if not lines:
        for node in root.findall(".//p"):
            t_ms = node.attrib.get("t")
            text = _node_text(node)
            if t_ms and text:
                lines.append(f"{format_time(int(t_ms) / 1000)} {text}")

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
    total = int(float(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
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
        )
    except Exception as e:
        return {"error": f"YouTube request failed: {e}"}

    try:
        data = json.loads(raw_response.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"error": "YouTube returned invalid response."}

    status = data.get("playabilityStatus", {}).get("status")
    if status != "OK":
        reason = data.get("playabilityStatus", {}).get("reason", "Unknown error")
        return {"error": f"YouTube: {status} ({reason})"}

    details = data.get("videoDetails", {})
    length_seconds = int(details.get("lengthSeconds", 0))
    metadata = {
        "title": details.get("title", "N/A"),
        "author": details.get("author", "N/A"),
        "duration": format_duration(length_seconds),
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

    try:
        xml_content = make_request(sub_url, headers={"User-Agent": USER_AGENT}).decode(
            "utf-8"
        )
    except Exception as e:
        metadata["transcript"] = f"Failed to fetch subtitles: {e}"
        return metadata

    metadata["transcript"] = parse_transcript_xml(xml_content)
    return metadata


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python Scripts/Youtube_helper.py <URL>")
        sys.exit(1)

    url = sys.argv[1]
    video_id = extract_video_id(url)

    if not video_id:
        print("Error: Failed to extract video ID from URL.")
        sys.exit(1)

    result = get_youtube_data(video_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if "error" in result:
        sys.exit(1)
