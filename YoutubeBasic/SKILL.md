---
name: YoutubeBasic
description: Extract metadata and timestamped transcripts for YouTube videos.
---

# YoutubeBasic

This skill is intended for extracting metadata and timestamped transcripts for YouTube videos.

## Usage

When the user asks for video information or a transcript from a YouTube link, follow these steps:

1. **Run the script**: Use `python3 Scripts/Youtube_helper.py <VIDEO_URL>` to fetch the data.
2. **Process the result**: The script outputs JSON with the title, author, duration, description, and timestamped transcript.

### Example command
```bash
python3 Scripts/Youtube_helper.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

## Requirements
Python 3 only. No additional `pip install` is required.
