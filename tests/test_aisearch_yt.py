"""Unit tests for the theAIsearch YouTube bridge. No network: the impure edge
(yt-dlp / faster-whisper / Ultra) is never called — these exercise the PURE
transforms (TOC parse, VTT parse, transcript slice, routing parse/assemble, RSS/
sidecar/heartbeat build) and an end-to-end pipeline with an injected fake router.
"""

import json
import xml.etree.ElementTree as ET

import pytest

import scrape_aisearch_yt as yt


# Realistic theAIsearch-style description: a timestamp TOC of tool names + links,
# with intro/outro lines that carry no link. The TOC is the SKELETON; the words
# live in the transcript (the VTT below).
DESCRIPTION = """\
This week's biggest AI tool drops, explained.

00:00 Intro
00:45 Claude Code - https://claude.com/claude-code
04:30 Cursor https://cursor.com
09:15 - Perplexity
13:00 Outro

Subscribe for weekly AI news and tool deep dives!
"""

VTT = """\
WEBVTT

00:00:01.000 --> 00:00:03.000
welcome back to the channel

00:00:46.000 --> 00:00:49.000
Claude Code is Anthropic's agentic CLI for editing real codebases

00:04:31.000 --> 00:04:34.000
Cursor is an AI first IDE fork of vs code

00:09:16.000 --> 00:09:19.000
Perplexity is an AI answer engine over the web

00:13:01.000 --> 00:13:03.000
thanks for watching see you next week
"""


# --- timestamp helpers -------------------------------------------------------

def test_ts_to_seconds():
    assert yt.ts_to_seconds("00:45") == 45
    assert yt.ts_to_seconds("04:30") == 270
    assert yt.ts_to_seconds("1:02:03") == 3723
    assert yt.ts_to_seconds("13:00") == 780
    assert yt.ts_to_seconds("garbage") is None


def test_seconds_to_ts():
    assert yt.seconds_to_ts(45) == "0:45"
    assert yt.seconds_to_ts(780) == "13:00"
    assert yt.seconds_to_ts(3723) == "1:02:03"


# --- parse_toc ---------------------------------------------------------------

def test_parse_toc_extracts_boundaries_names_links():
    toc = yt.parse_toc(DESCRIPTION)
    starts = [e["start"] for e in toc]
    assert starts == [0, 45, 270, 555, 780]
    by_start = {e["start"]: e for e in toc}
    assert by_start[45]["tool_name"] == "Claude Code"
    assert by_start[45]["tool_url"] == "https://claude.com/claude-code"
    # URL with no separator still parsed; name strips the trailing URL.
    assert by_start[270]["tool_name"] == "Cursor"
    assert by_start[270]["tool_url"] == "https://cursor.com"
    # No link → tool_url None, name from the label.
    assert by_start[555]["tool_name"] == "Perplexity"
    assert by_start[555]["tool_url"] is None
    assert by_start[0]["tool_name"] == "Intro"


def test_parse_toc_empty_when_no_timestamps():
    assert yt.parse_toc("just a plain description with no chapters") == []


# --- parse_vtt ---------------------------------------------------------------

def test_parse_vtt_cues():
    cues = yt.parse_vtt(VTT)
    assert len(cues) == 5
    assert cues[0]["start"] == pytest.approx(1.0)
    assert "welcome back" in cues[0]["text"]
    assert cues[1]["start"] == pytest.approx(46.0)
    assert "agentic CLI" in cues[1]["text"]


def test_parse_vtt_collapses_rolling_duplication():
    rolling = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:02.000\nhello\n\n"
        "00:00:02.000 --> 00:00:03.000\nhello there\n\n"
        "00:00:03.000 --> 00:00:04.000\nhello there friend\n"
    )
    cues = yt.parse_vtt(rolling)
    assert len(cues) == 1
    assert cues[0]["text"] == "hello there friend"


# --- slice_transcript --------------------------------------------------------

def test_slice_transcript_windows_by_toc():
    toc = yt.parse_toc(DESCRIPTION)
    cues = yt.parse_vtt(VTT)
    segs = yt.slice_transcript(cues, toc, duration=800)
    assert len(segs) == 5
    claude = next(s for s in segs if s["tool_name"] == "Claude Code")
    assert "agentic CLI" in claude["text"]
    assert claude["start"] == 45 and claude["end"] == 270
    # last segment runs to duration
    assert segs[-1]["end"] == 800


def test_slice_transcript_no_toc_is_single_segment():
    cues = yt.parse_vtt(VTT)
    segs = yt.slice_transcript(cues, [], duration=800)
    assert len(segs) == 1
    assert segs[0]["tool_name"] is None
    assert "welcome back" in segs[0]["text"] and "Perplexity" in segs[0]["text"]


# --- routing prompt + parse + assemble ---------------------------------------

def test_build_routing_prompt_lists_subjects_and_segments():
    segs = [{"tool_name": "Claude Code", "text": "agentic cli"}]
    prompt = yt.build_routing_prompt("Week in AI", segs)
    assert "claude-code" in prompt and "unsorted" in prompt
    assert "coding-build-demo" in prompt  # 2-level taxonomy renders sub_subjects
    assert "[0] tool/topic: Claude Code" in prompt
    assert "JSON array" in prompt


def test_parse_routing_response_aligns_and_coerces_unknown_subject():
    raw = (
        'Here you go:\n'
        '[{"index":0,"subject":"claude-code","summary":"agentic cli","keep":true},'
        '{"index":1,"subject":"made-up","summary":"x","keep":true}]'
    )
    out = yt.parse_routing_response(raw, 2)
    assert out[0]["subject"] == "claude-code"
    assert out[1]["subject"] == "unsorted"  # unknown slug → escape hatch
    assert out[0]["sub_subject"] is None  # absent sub_subject → null


def test_parse_routing_response_validates_sub_subject():
    raw = (
        '[{"index":0,"subject":"claude-code","sub_subject":"build-demo","summary":"x","keep":true},'
        '{"index":1,"subject":"claude-code","sub_subject":"not-a-sub","summary":"y","keep":true}]'
    )
    out = yt.parse_routing_response(raw, 2)
    assert out[0]["sub_subject"] == "build-demo"
    assert out[1]["sub_subject"] is None  # invalid sub for this subject → null


def test_parse_routing_response_missing_index_raises():
    with pytest.raises(yt.BridgeShapeError):
        yt.parse_routing_response('[{"index":0,"subject":"claude-code","keep":true}]', 2)


def test_parse_routing_response_no_array_raises():
    with pytest.raises(yt.BridgeShapeError):
        yt.parse_routing_response("sorry, I cannot do that", 1)


def test_assemble_routed_drops_keep_false():
    segs = [
        {"start": 0, "end": 45, "tool_name": "Intro", "tool_url": None, "text": "hi"},
        {"start": 45, "end": 270, "tool_name": "Claude Code",
         "tool_url": "https://claude.com/claude-code", "text": "cli"},
    ]
    routings = [
        {"subject": "unsorted", "summary": "", "keep": False},
        {"subject": "claude-code", "summary": "agentic cli", "keep": True},
    ]
    out = yt.assemble_routed(segs, routings)
    assert len(out) == 1
    assert out[0]["tool_name"] == "Claude Code"
    assert out[0]["subject"] == "claude-code"
    assert out[0]["start"] == "0:45" and out[0]["end"] == "4:30"


# --- end-to-end pipeline (pure, fake router) ---------------------------------

def _fake_route(video_title, segments):
    """Deterministic router: route by tool name, drop intro/outro."""
    mapping = {
        "Claude Code": "claude-code",
        "Cursor": "ai-dev-tooling",
        "Perplexity": "retrieval-rag-memory",
    }
    out = []
    for s in segments:
        name = s.get("tool_name")
        keep = name in mapping
        out.append({
            "subject": mapping.get(name, "unsorted"),
            "summary": f"{name} summary" if keep else "",
            "keep": keep,
        })
    return out


def test_end_to_end_sidecar_and_rss_match_yuna_contract():
    video = {
        "video_id": "VIDEOID1",
        "title": "This Week in AI: New Dev Tools",
        "url": "https://www.youtube.com/watch?v=VIDEOID1",
        "channel": "theAIsearch",
        "upload_date": "20260622",
        "duration": 800,
    }
    toc = yt.parse_toc(DESCRIPTION)
    cues = yt.parse_vtt(VTT)
    sliced = yt.slice_transcript(cues, toc, video["duration"])
    routings = _fake_route(video["title"], sliced)
    routed = yt.assemble_routed(sliced, routings)
    entry = yt.build_sidecar_entry(video, routed, "captions")

    # Sidecar shape MUST match Yuna rss.py _SIDECAR_YT_FIELDS + the segment keys
    # Yuna's /api/subjects reads.
    assert set(("segments", "subjects", "transcript_source", "schema_version")).issubset(entry)
    assert entry["transcript_source"] == "captions"
    assert entry["schema_version"] == 2
    assert entry["subjects"] == ["ai-dev-tooling", "claude-code", "retrieval-rag-memory"]
    assert len(entry["segments"]) == 3  # intro + outro dropped
    seg0 = entry["segments"][0]
    assert set(seg0) == {"start", "end", "tool_name", "tool_url", "subject", "sub_subject", "summary"}

    sidecar = {video["video_id"]: entry}
    rss = yt.build_rss(sidecar, 100, yt._upload_date_to_rfc822(None, "2026-06-24T00:00:00Z"),
                       "2026-06-24T00:00:00Z")
    root = ET.fromstring(rss)  # well-formed
    items = root.findall(".//item")
    assert len(items) == 1  # ONE item per video
    guid = items[0].find("guid").text
    assert guid == "VIDEOID1"

    # Sidecar JSON round-trips (the publish guard).
    assert json.loads(json.dumps(sidecar))


def test_build_heartbeat_shape():
    hb = yt.build_heartbeat("ok", 12, 2, 30, None, "2026-06-24T00:00:00Z", empty_videos=1)
    assert hb["source"] == "rss_aisearch_yt"
    assert hb["status"] == "ok" and hb["last_success"] == "2026-06-24T00:00:00Z"
    assert hb["items_total"] == 12 and hb["items_new"] == 2
    err = yt.build_heartbeat("error", 12, 0, 0, "boom", "2026-06-24T00:00:00Z")
    assert err["status"] == "error" and err["last_success"] is None
