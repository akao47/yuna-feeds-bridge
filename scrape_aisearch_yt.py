"""Scrape theAIsearch's YouTube channel (curated AI-news/tool roundups) → emit a
clean RSS feed + a structured `segments[]` sidecar for Yuna's `rss_aisearch_yt`
source (the 2026-06-24 pivot, slice 1a: YouTube intake/router).

Pipeline per video (the "corrected input model", confirmed 2026-06-24):
  1. yt-dlp lists the channel's recent videos and fetches each one's metadata +
     transcript. The DESCRIPTION is only a skeleton — a timestamp TOC of tool
     names + links, no explanation. The real content is in the TRANSCRIPT.
  2. parse_toc(description) → the segment BOUNDARIES (start time + tool name +
     tool link). The end of segment i is the start of segment i+1.
  3. the transcript (captions, or faster-whisper ASR fallback) is SLICED by those
     boundaries, so each pre-bounded segment carries the words spoken about it.
  4. Ultra (the GB10 engine-router) SUMMARIZES each segment and ROUTES it to one
     of the 7 subjects (or 'unsorted'), and decides keep/drop (intros, outros,
     sponsor reads → dropped).
  5. emit ONE RSS item per video (guid = video_id) + a sidecar entry keyed by
     video_id carrying segments[] — exactly the shape Yuna's rss.py merges into
     extracted_metadata (segments / subjects / transcript_source / schema_version).

Runs on the GB10 box on a cron (its residential IP + the local engine-router);
run-aisearch-yt.sh git-commits the three outputs back to this repo, and Yuna polls
the raw GitHub URL of aisearch_yt.xml via its existing RSS path.

Design authority: Yuna planning/implementations/IMP-2026-06-24-yt-intake-router/
01-PLAN.md (Lane A: T1 scraper, T2 Ultra route/emit) + the pivot design
planning/designs/O11-main-design-20260624-181304.md.

Mirrors scrape_aisearch.py's contract: PURE transforms (TOC parse, slice, routing
assembly, RSS/sidecar/heartbeat build — all unit-tested, no network) + an IMPURE
edge (yt-dlp / faster-whisper / Ultra — imported inside the functions that use
them, so this module imports cleanly for testing without those deps).

Loud-fail: on acquisition failure or TOC-format mass-drift the scraper writes
NOTHING to the feed/sidecar and exits non-zero, so Yuna keeps serving the
last-known-good feed. The heartbeat is ALWAYS written so "emits nothing" can't
read as healthy (Codex #10).
"""

from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape


# --- config (env-overridable) ------------------------------------------------

# The channel /videos URL. FOUNDER: confirm the exact handle on first deploy.
CHANNEL_URL = os.environ.get("AISEARCH_YT_CHANNEL", "https://www.youtube.com/@theAIsearch/videos")
ROOT = Path(__file__).parent
FEED_PATH = ROOT / "aisearch_yt.xml"
SIDECAR_PATH = ROOT / "aisearch_yt.json"
HEARTBEAT_PATH = ROOT / "aisearch_yt-heartbeat.json"

# MAX_NEW_VIDEOS bounds Ultra cost per run (each new video = one routing call).
# A burst larger than the cap drains across runs (the seen-video_id watermark is
# the sidecar keys, so unseen videos remain unseen until ingested). MAX_LIST is a
# fetch safety bound on the flat channel listing.
MAX_NEW_VIDEOS = int(os.environ.get("AISEARCH_YT_MAX_NEW", "5"))
MAX_LIST = int(os.environ.get("AISEARCH_YT_MAX_LIST", "30"))
RSS_MAX_ITEMS = int(os.environ.get("AISEARCH_YT_RSS_MAX", "100"))

# If more than this fraction of a run's NEW videos yield zero usable segments, the
# description TOC format almost certainly drifted (or transcripts are unavailable)
# → loud-fail rather than publish a degraded feed.
MAX_EMPTY_FRACTION = float(os.environ.get("AISEARCH_YT_MAX_EMPTY_FRACTION", "0.75"))

# Ultra (GB10 engine-router). The bridge runs ON the box, so localhost by default.
LLM_BASE_URL = os.environ.get("AISEARCH_YT_LLM_BASE_URL", "http://localhost:18765/v1")
LLM_MODEL = os.environ.get("AISEARCH_YT_LLM_MODEL", "ultra/default")
LLM_API_KEY = os.environ.get("AISEARCH_YT_LLM_API_KEY", "x")  # router accepts any non-empty key
LLM_TIMEOUT_S = float(os.environ.get("AISEARCH_YT_LLM_TIMEOUT", "120"))

CONTACT = os.environ.get("AISEARCH_CONTACT", "alex.kao92@gmail.com")

# The 7 subjects (2026-06-24 pivot) + the 'unsorted' escape hatch. This is the
# routing taxonomy SOT — the bridge (producer) owns it; Yuna groups by whatever
# slug appears, and the D4 eval scores routing against these slugs.
SUBJECT_SLUGS = (
    "claude-code",
    "ai-dev-tooling",
    "llms-foundation-models",
    "retrieval-rag-memory",
    "ai-content-generation",
    "eval-model-quality",
    "local-models-infra",
)
UNSORTED = "unsorted"
SCHEMA_VERSION = 1

# A timestamp at line start: MM:SS or H:MM:SS / HH:MM:SS, optionally bracketed.
_TOC_LINE_RE = re.compile(r"^\s*\[?\(?\s*((?:\d{1,2}:)?\d{1,2}:\d{2})\s*\)?\]?\s*[-–—)\.]*\s*(.*\S)?\s*$")
_URL_RE = re.compile(r"https?://[^\s<>()\]]+")
# C0 control chars XML 1.0 forbids even inside CDATA (all except \t \n \r).
_XML_BAD_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# WEBVTT cue timing line: HH:MM:SS.mmm --> HH:MM:SS.mmm (with optional cue settings).
_VTT_TIMING_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})"
)
_VTT_TAG_RE = re.compile(r"<[^>]+>")


class BridgeShapeError(RuntimeError):
    """Acquisition / format drift (no channel videos, mass-empty TOCs). Raised so
    main() loud-fails instead of overwriting a working feed with junk."""


# --- pure transforms (no network; unit-tested) -------------------------------

def _clean(text: str) -> str:
    return _XML_BAD_CTRL_RE.sub("", text)


def ts_to_seconds(ts: str) -> int | None:
    """'MM:SS' or 'H:MM:SS' / 'HH:MM:SS' → integer seconds, else None."""
    if not ts:
        return None
    parts = ts.strip().replace(",", ":").split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        m, s = nums
        return m * 60 + s
    if len(nums) == 3:
        h, m, s = nums
        return h * 3600 + m * 60 + s
    return None


def seconds_to_ts(seconds: float) -> str:
    """Integer-second 'M:SS' (or 'H:MM:SS' past an hour) for human-readable TOC echo."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _vtt_ts_to_seconds(ts: str) -> float:
    """'HH:MM:SS.mmm' → float seconds."""
    ts = ts.replace(",", ".")
    hms, _, frac = ts.partition(".")
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s + (float("0." + frac) if frac else 0.0)


def parse_toc(description: str) -> list[dict]:
    """Extract the timestamp TOC from a video description → segment boundaries.

    Each entry: {start (seconds), tool_name, tool_url|None, label (raw)}. A line
    qualifies only if it starts with a timestamp. The first URL on the line is the
    tool link; the text before it (minus the timestamp and separators) is the tool
    name. Entries are returned sorted by start; duplicate/again-zero timestamps are
    de-duplicated keeping the first. An empty list means "no TOC" (not an error)."""
    entries: list[dict] = []
    seen_starts: set[int] = set()
    for raw_line in description.splitlines():
        m = _TOC_LINE_RE.match(raw_line)
        if not m:
            continue
        start = ts_to_seconds(m.group(1))
        if start is None or start in seen_starts:
            continue
        rest = (m.group(2) or "").strip()
        url_match = _URL_RE.search(rest)
        tool_url = url_match.group(0).rstrip(".,);") if url_match else None
        label = rest
        name = _URL_RE.sub("", rest).strip(" -–—|:·•\t")
        if not name:
            name = tool_url or seconds_to_ts(start)
        entries.append({
            "start": start,
            "tool_name": _clean(name),
            "tool_url": _clean(tool_url) if tool_url else None,
            "label": _clean(label),
        })
        seen_starts.add(start)
    entries.sort(key=lambda e: e["start"])
    return entries


def parse_vtt(vtt: str) -> list[dict]:
    """WEBVTT (or auto-caption VTT) → cues [{start (seconds), text}].

    Strips inline <...> timing tags and drops the rolling-window duplication that
    YouTube auto-captions emit (a cue whose text is a prefix/superset of the prior
    kept cue's text is collapsed), so sliced segments aren't choked with repeats."""
    cues: list[dict] = []
    blocks = re.split(r"\n\s*\n", vtt.replace("\r\n", "\n"))
    last_text = ""
    for block in blocks:
        timing = _VTT_TIMING_RE.search(block)
        if not timing:
            continue
        start = _vtt_ts_to_seconds(timing.group(1))
        lines = block.split("\n")
        text_lines = [ln for ln in lines if not _VTT_TIMING_RE.search(ln) and "-->" not in ln]
        text = _VTT_TAG_RE.sub("", " ".join(text_lines)).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            continue
        # Collapse auto-caption rolling repetition.
        if text == last_text or (last_text and text.startswith(last_text)):
            if cues:
                cues[-1]["text"] = text
                last_text = text
                continue
        if last_text and last_text.endswith(text):
            continue
        cues.append({"start": start, "text": text})
        last_text = text
    return cues


def slice_transcript(cues: list[dict], toc: list[dict], duration: float | None) -> list[dict]:
    """Slice the transcript into the TOC's pre-bounded segments.

    Segment i spans [toc[i].start, toc[i+1].start); the last runs to `duration`
    (or open-ended if unknown). Each output segment carries the concatenated cue
    text within its window. Returns [{start, end, tool_name, tool_url, text}].
    With no TOC, returns a single whole-video segment so a TOC-less video can still
    be routed (Ultra gets the full transcript)."""
    if not cues:
        return []
    if not toc:
        end = duration if duration else cues[-1]["start"] + 1
        text = " ".join(c["text"] for c in cues).strip()
        return [{"start": 0, "end": end, "tool_name": None, "tool_url": None, "text": text}]
    segments: list[dict] = []
    for i, entry in enumerate(toc):
        start = entry["start"]
        end = toc[i + 1]["start"] if i + 1 < len(toc) else (duration if duration else float("inf"))
        text = " ".join(c["text"] for c in cues if start <= c["start"] < end).strip()
        segments.append({
            "start": start,
            "end": None if end == float("inf") else int(end),
            "tool_name": entry["tool_name"],
            "tool_url": entry["tool_url"],
            "text": text,
        })
    return segments


def build_routing_prompt(video_title: str, segments: list[dict]) -> str:
    """The Ultra instruction: route + summarize + keep each pre-bounded segment.
    Returns a single prompt asking for a strict JSON array aligned to segment index."""
    subjects = "\n".join(f"  - {s}" for s in SUBJECT_SLUGS)
    blocks = []
    for i, seg in enumerate(segments):
        name = seg.get("tool_name") or "(untitled segment)"
        body = (seg.get("text") or "").strip()
        if len(body) > 4000:
            body = body[:4000] + " …"
        blocks.append(f'[{i}] tool/topic: {name}\ntranscript:\n{body or "(no transcript for this segment)"}')
    joined = "\n\n".join(blocks)
    return (
        "You are routing segments of an AI-news YouTube video into a personal "
        "knowledge base. For EACH numbered segment below, decide:\n"
        "  - subject: the single best-fit slug from this list, or \"unsorted\" if "
        "none fits:\n" + subjects + "\n  - " + UNSORTED + "\n"
        "  - summary: 1-2 plain sentences on what the tool/topic is and why it "
        "matters, grounded ONLY in the transcript.\n"
        "  - keep: true if the segment is about a real AI tool/topic; false for "
        "intros, outros, sponsor reads, channel plugs, or filler.\n\n"
        f"Video title: {video_title}\n\n"
        f"{joined}\n\n"
        "Respond with ONLY a JSON array of objects, one per segment, in order, each "
        '{\"index\": <int>, \"subject\": <slug>, \"summary\": <string>, \"keep\": <bool>}. '
        "No prose, no markdown fences."
    )


def parse_routing_response(raw: str, expected_n: int) -> list[dict]:
    """Parse Ultra's routing reply into a list aligned to segment index 0..n-1.

    Tolerant of stray prose/fences around the JSON array; fail-loud (raise) only if
    no JSON array can be found or it doesn't cover every index. Unknown subject slugs
    are coerced to 'unsorted' (the escape hatch) rather than rejected."""
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise BridgeShapeError("routing reply has no JSON array")
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as e:
        raise BridgeShapeError(f"routing reply not valid JSON: {e}") from e
    if not isinstance(data, list):
        raise BridgeShapeError("routing reply JSON is not an array")
    by_index: dict[int, dict] = {}
    for obj in data:
        if not isinstance(obj, dict) or "index" not in obj:
            continue
        try:
            idx = int(obj["index"])
        except (TypeError, ValueError):
            continue
        subject = obj.get("subject")
        if subject not in SUBJECT_SLUGS:
            subject = UNSORTED
        by_index[idx] = {
            "subject": subject,
            "summary": (obj.get("summary") or "").strip(),
            "keep": bool(obj.get("keep", True)),
        }
    missing = [i for i in range(expected_n) if i not in by_index]
    if missing:
        raise BridgeShapeError(f"routing reply missing indices {missing[:5]} of {expected_n}")
    return [by_index[i] for i in range(expected_n)]


def assemble_routed(segments: list[dict], routings: list[dict]) -> list[dict]:
    """Combine sliced segments with their routing results, dropping keep=false.
    Returns the sidecar segment shape Yuna files: {start, end, tool_name, tool_url,
    subject, summary}."""
    out: list[dict] = []
    for seg, routing in zip(segments, routings):
        if not routing.get("keep", True):
            continue
        out.append({
            "start": seconds_to_ts(seg["start"]),
            "end": seconds_to_ts(seg["end"]) if seg.get("end") is not None else None,
            "tool_name": seg.get("tool_name"),
            "tool_url": seg.get("tool_url"),
            "subject": routing.get("subject") or UNSORTED,
            "summary": _clean(routing.get("summary") or ""),
        })
    return out


def build_sidecar_entry(video: dict, routed_segments: list[dict], transcript_source: str) -> dict:
    """The sidecar entry Yuna keys by video_id. segments/subjects/transcript_source/
    schema_version are the fields rss.py merges into extracted_metadata; the rest is
    for re-render / debugging (Yuna ignores unknown keys)."""
    subjects = sorted({s["subject"] for s in routed_segments}) if routed_segments else []
    return {
        "video_id": video["video_id"],
        "title": _clean(video.get("title") or video["video_id"]),
        "video_url": video.get("url") or f"https://www.youtube.com/watch?v={video['video_id']}",
        "channel": _clean(video.get("channel") or "theAIsearch"),
        "upload_date": video.get("upload_date"),
        "segments": routed_segments,
        "subjects": subjects,
        "transcript_source": transcript_source,
        "schema_version": SCHEMA_VERSION,
    }


def _cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _upload_date_to_rfc822(upload_date: str | None, fallback_iso: str) -> str:
    """yt-dlp 'YYYYMMDD' → RFC822 at 00:00:00 UTC; fallback to the run time's date."""
    if upload_date and re.fullmatch(r"\d{8}", upload_date):
        dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
    else:
        dt = datetime.strptime(fallback_iso[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def build_rss(sidecar: dict, max_items: int, build_date_rfc822: str, now_iso: str) -> str:
    """RSS 2.0 from the sidecar, ONE item per video (newest upload first, capped).
    guid = video_id (stable); link = watch URL; description = a routed-segment
    digest (CDATA) so the feed body reflects what was filed."""
    entries = sorted(
        sidecar.values(),
        key=lambda e: (e.get("upload_date") or "", e.get("video_id", "")),
        reverse=True,
    )[:max_items]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "  <channel>",
        "    <title>theAIsearch YouTube (yuna-feeds-bridge)</title>",
        f"    <link>{xml_escape(CHANNEL_URL)}</link>",
        "    <description>theAIsearch AI-news/tool roundups, segmented + subject-routed "
        "for Yuna's rss_aisearch_yt source. Segments ride in the sidecar.</description>",
        f"    <lastBuildDate>{build_date_rfc822}</lastBuildDate>",
    ]
    for entry in entries:
        title = xml_escape(entry["title"])
        link = xml_escape(entry["video_url"])
        guid = xml_escape(entry["video_id"])
        pub = _upload_date_to_rfc822(entry.get("upload_date"), now_iso)
        body = _segment_digest(entry)
        lines += [
            "    <item>",
            f"      <title>{title}</title>",
            f"      <link>{link}</link>",
            f'      <guid isPermaLink="false">{guid}</guid>',
            f"      <pubDate>{pub}</pubDate>",
            f"      <description>{_cdata(body)}</description>",
            "    </item>",
        ]
    lines += ["  </channel>", "</rss>", ""]
    return "\n".join(lines)


def _segment_digest(entry: dict) -> str:
    """A human-readable body listing each routed segment (tool — subject — summary)."""
    parts = [f"{entry['title']} — segmented AI-news roundup ({len(entry.get('segments', []))} segments)."]
    for seg in entry.get("segments", []):
        name = seg.get("tool_name") or "(segment)"
        parts.append(f"[{seg.get('subject')}] {name}: {seg.get('summary') or ''}".strip())
    return "\n\n".join(parts)


def build_heartbeat(status: str, items_total: int, items_new: int, videos_listed: int,
                    error: str | None, now_iso: str, empty_videos: int = 0) -> dict:
    return {
        "source": "rss_aisearch_yt",
        "status": status,                 # "ok" | "error"
        "last_run": now_iso,
        "last_success": now_iso if status == "ok" else None,
        "items_total": items_total,
        "items_new": items_new,
        "videos_listed": videos_listed,
        "empty_videos": empty_videos,     # new videos that yielded no usable segments
        "schema_version": SCHEMA_VERSION,
        "error": error,
    }


# --- IO + acquisition (the impure edge; imports its deps inside) --------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(e: object) -> str:
    return str(e).splitlines()[0][:300] if str(e).strip() else repr(e)


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _write_heartbeat_only(hb: dict) -> None:
    _atomic_write(HEARTBEAT_PATH, json.dumps(hb, indent=2) + "\n")


def _publish(rss: str, sidecar: dict, hb: dict) -> None:
    """Validate, then atomically write all three (sidecar → feed → heartbeat), so a
    mid-publish kill leaves at worst a sidecar entry with no RSS item (Yuna ingests
    nothing extra), never an RSS item with no segments."""
    ET.fromstring(rss)  # raises on malformed XML before touching the live feed
    sidecar_json = json.dumps(sidecar, ensure_ascii=False, sort_keys=True, indent=1)
    json.loads(sidecar_json)  # round-trip guard
    _atomic_write(SIDECAR_PATH, sidecar_json + "\n")
    _atomic_write(FEED_PATH, rss)
    _atomic_write(HEARTBEAT_PATH, json.dumps(hb, indent=2) + "\n")


def _list_channel_videos(channel_url: str, limit: int) -> list[dict]:
    """yt-dlp flat listing of the channel's recent videos → [{video_id, title, url}]."""
    import yt_dlp

    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": limit,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
    entries = (info or {}).get("entries") or []
    out: list[dict] = []
    for e in entries:
        vid = e.get("id")
        if not vid:
            continue
        out.append({
            "video_id": vid,
            "title": e.get("title") or vid,
            "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
        })
    return out[:limit]


def _fetch_video_detail(video_id: str) -> dict:
    """yt-dlp metadata + the en caption VTT (manual subs preferred, else auto)."""
    import httpx
    import yt_dlp

    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {"quiet": True, "skip_download": True, "writesubtitles": False}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    detail = {
        "video_id": video_id,
        "title": info.get("title") or video_id,
        "description": info.get("description") or "",
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "channel": info.get("channel") or info.get("uploader"),
        "url": url,
        "captions_vtt": None,
        "transcript_source": "none",
    }
    caption_url = _pick_caption_url(info.get("subtitles") or {}) \
        or _pick_caption_url(info.get("automatic_captions") or {})
    if caption_url:
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True,
                              headers={"From": CONTACT}) as client:
                resp = client.get(caption_url)
            if resp.status_code == 200 and resp.text.strip():
                detail["captions_vtt"] = resp.text
                detail["transcript_source"] = (
                    "captions" if info.get("subtitles") else "auto-captions"
                )
        except Exception:  # transient → falls through to ASR
            pass
    return detail


def _pick_caption_url(tracks: dict) -> str | None:
    """From a yt-dlp subtitles/automatic_captions dict, the en VTT track URL."""
    for lang in ("en", "en-US", "en-GB", "en-orig"):
        for fmt in tracks.get(lang, []):
            if fmt.get("ext") == "vtt" and fmt.get("url"):
                return fmt["url"]
    return None


def _asr_transcribe(video_id: str) -> list[dict]:
    """faster-whisper ASR fallback when no captions exist → cues [{start, text}]."""
    import tempfile

    import yt_dlp
    from faster_whisper import WhisperModel

    model_size = os.environ.get("AISEARCH_YT_WHISPER_MODEL", "base.en")
    with tempfile.TemporaryDirectory() as tmp:
        out_tmpl = str(Path(tmp) / "%(id)s.%(ext)s")
        opts = {
            "quiet": True,
            "format": "bestaudio/best",
            "outtmpl": out_tmpl,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        audio = next(Path(tmp).glob(f"{video_id}.*"))
        model = WhisperModel(model_size, device="auto", compute_type="auto")
        segments, _ = model.transcribe(str(audio))
        return [{"start": float(s.start), "text": s.text.strip()} for s in segments]


def _make_ultra_router():
    """route(video_title, segments) → routings (list aligned to segment index) via
    one Ultra chat completion per video."""
    from openai import OpenAI

    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=LLM_TIMEOUT_S)

    def route(video_title: str, segments: list[dict]) -> list[dict]:
        prompt = build_routing_prompt(video_title, segments)
        # The engine-router Ultra engine always streams (prefer_stream); a non-stream
        # create() hands back the raw SSE body as a str. Stream and accumulate content
        # deltas (skip reasoning_content; Nemotron Ultra is a reasoning model).
        stream = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            stream=True,
        )
        content = "".join(
            chunk.choices[0].delta.content
            for chunk in stream
            if chunk.choices and chunk.choices[0].delta.content
        )
        return parse_routing_response(content, len(segments))

    return route


def _transcript_for(detail: dict) -> tuple[list[dict], str]:
    """Cues + their source for a video: captions if present, else ASR fallback."""
    if detail.get("captions_vtt"):
        return parse_vtt(detail["captions_vtt"]), detail.get("transcript_source", "captions")
    return _asr_transcribe(detail["video_id"]), "asr"


def _process_video(video: dict, route) -> dict | None:
    """Full per-video pipeline → sidecar entry, or None if it yields no segments.
    Per-video failures are swallowed to a warning so one bad video can't sink a run;
    the empty-fraction guard in main() catches systematic drift."""
    try:
        detail = _fetch_video_detail(video["video_id"])
        cues, source = _transcript_for(detail)
        toc = parse_toc(detail["description"])
        sliced = slice_transcript(cues, toc, detail.get("duration"))
        if not sliced:
            print(f"warn: {video['video_id']} no transcript/segments", file=sys.stderr)
            return None
        routings = route(detail["title"], sliced)
        routed = assemble_routed(sliced, routings)
        if not routed:
            print(f"warn: {video['video_id']} all segments dropped", file=sys.stderr)
            return None
        merged = {**video, **detail}
        return build_sidecar_entry(merged, routed, source)
    except Exception as e:  # one video failing must not poison the run
        print(f"warn: {video['video_id']} failed: {_err(e)}", file=sys.stderr)
        return None


def main() -> int:
    if os.environ.get("AISEARCH_YT_BRIDGE_DISABLED"):
        print("AISEARCH_YT_BRIDGE_DISABLED set; skipping scrape.", file=sys.stderr)
        return 0

    sidecar = _load_json(SIDECAR_PATH) or {}
    seen_ids = set(sidecar.keys())
    now = _now_iso()

    try:
        listed = _list_channel_videos(CHANNEL_URL, MAX_LIST)
        if not listed:
            raise BridgeShapeError("channel listing returned no videos")
    except Exception as e:
        _write_heartbeat_only(build_heartbeat("error", len(sidecar), 0, 0,
                                              f"list failed: {_err(e)}", now))
        print(f"ERROR list failed: {_err(e)}", file=sys.stderr)
        return 2

    new_videos = [v for v in listed if v["video_id"] not in seen_ids][:MAX_NEW_VIDEOS]

    try:
        route = _make_ultra_router()
        added, empty = 0, 0
        for video in new_videos:
            entry = _process_video(video, route)
            if entry is None:
                empty += 1
                continue
            if video["video_id"] not in sidecar:  # items immutable once emitted
                sidecar[video["video_id"]] = entry
                added += 1

        # Systematic emptiness within a non-empty new-video batch = TOC/transcript drift.
        if new_videos and empty > MAX_EMPTY_FRACTION * len(new_videos):
            _write_heartbeat_only(build_heartbeat(
                "error", len(sidecar), 0, len(listed),
                f"drift: {empty}/{len(new_videos)} new videos yielded no segments",
                now, empty_videos=empty))
            print(f"ERROR drift: {empty}/{len(new_videos)} empty", file=sys.stderr)
            return 2

        rss = build_rss(sidecar, RSS_MAX_ITEMS, _upload_date_to_rfc822(None, now), now)
        hb = build_heartbeat("ok", len(sidecar), added, len(listed), None, now,
                             empty_videos=empty)
        _publish(rss, sidecar, hb)
    except Exception as e:
        _write_heartbeat_only(build_heartbeat(
            "error", len(sidecar), 0, len(listed), f"publish failed: {_err(e)}", now))
        print(f"ERROR publish failed: {_err(e)}", file=sys.stderr)
        return 2

    print(f"ok: +{added} new ({empty} empty), {len(sidecar)} total, "
          f"{len(listed)} listed, {len(new_videos)} new considered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
