"""Unit tests for core.youtube pure parsing helpers.

These hit no network: parse_uploads_rss takes a feed string and
video_id_from_url takes a URL. The live calls (resolve_channel_id,
fetch_uploads, fetch_transcript) are exercised against real YouTube
from scripts/, not here.
"""

from __future__ import annotations

from core.youtube import parse_uploads_rss, rss_url, video_id_from_url

_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <title>Uploads from Test Channel</title>
  <entry>
    <id>yt:video:abcdefghijk</id>
    <yt:videoId>abcdefghijk</yt:videoId>
    <title>How I automate everything with one prompt</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abcdefghijk"/>
    <author><name>Test Creator</name></author>
    <published>2026-05-27T12:00:00+00:00</published>
  </entry>
  <entry>
    <id>yt:video:LMNOPQRSTUV</id>
    <yt:videoId>LMNOPQRSTUV</yt:videoId>
    <title>Second video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=LMNOPQRSTUV"/>
    <author><name>Test Creator</name></author>
    <published>2026-05-26T09:30:00+00:00</published>
  </entry>
</feed>"""


def test_parse_uploads_rss_extracts_entries():
    items = parse_uploads_rss(_FEED)
    assert len(items) == 2
    first = items[0]
    assert first["video_id"] == "abcdefghijk"
    assert first["title"] == "How I automate everything with one prompt"
    assert first["author"] == "Test Creator"
    assert first["published"] == "2026-05-27T12:00:00+00:00"
    assert first["url"].endswith("v=abcdefghijk")


def test_parse_uploads_rss_handles_garbage():
    assert parse_uploads_rss("not xml at all") == []
    assert parse_uploads_rss("") == []


def test_video_id_from_url_shapes():
    cases = {
        "https://www.youtube.com/watch?v=abcdefghijk": "abcdefghijk",
        "https://youtu.be/abcdefghijk": "abcdefghijk",
        "https://www.youtube.com/shorts/abcdefghijk": "abcdefghijk",
        "https://www.youtube.com/embed/abcdefghijk?rel=0": "abcdefghijk",
        "abcdefghijk": "abcdefghijk",
        "https://example.com/no-video-here": None,
        "": None,
    }
    for url, expected in cases.items():
        assert video_id_from_url(url) == expected, url


def test_rss_url():
    assert rss_url("UC123") == "https://www.youtube.com/feeds/videos.xml?channel_id=UC123"
