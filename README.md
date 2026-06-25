# yuna-feeds-bridge

RSS bridge for sources that don't publish a native feed. Currently:

- **Anthropic news** (`anthropic.xml`) — scraped from <https://www.anthropic.com/news> every 4 hours via GitHub Actions.
- **ai-search.io curated papers** (`aisearch.xml` + `aisearch.json`) — theAIsearch's curated arXiv picks, scraped from <https://ai-search.io/papers> on the GB10 box (see "ai-search.io curated papers" below).
- **theAIsearch YouTube** (`aisearch_yt.xml` + `aisearch_yt.json`) — theAIsearch's AI-news/tool roundup videos, segmented + subject-routed for Yuna's `rss_aisearch_yt` source on the GB10 box (see "theAIsearch YouTube" below).

Polled by [Yuna](https://github.com/akao47/Yuna) (founder-only, local-only) via the raw GitHub URL:

```
https://raw.githubusercontent.com/akao47/yuna-feeds-bridge/main/anthropic.xml
```

## Cadence

Cron schedule: `17 */4 * * *` (UTC). Aligned with Yuna's 4h RSS poll cadence, offset 17 minutes so the freshest XML is on disk when Yuna's next poll fires.

Manual trigger: Actions tab → "scrape-anthropic" → Run workflow.

## How the scraper works

1. `httpx` GET <https://www.anthropic.com/news>
2. Regex extract `<a href="/news/<slug>">…</a>` anchors + visible link text as title
3. Build RSS 2.0 XML with stable `<guid>` per slug (so Yuna's dedup works across scrapes)
4. Commit `anthropic.xml` back to `main` if changed

**Loud-fail design:** if the regex parses zero articles (almost certainly an upstream HTML restructure), the script exits non-zero **without** overwriting the existing `anthropic.xml`. The Actions run goes red; Yuna's poller continues serving the last-known-good feed. Watch the Actions tab if Anthropic restructures their newsroom.

## Why not a third-party RSS bridge (rss.app, feedity, etc.)?

External dependency on a vendor's uptime and free-tier limits. This repo costs $0 (well under the 2000 free-tier Actions minutes/month) and the scraper is 80 lines I can read.

## ai-search.io curated papers

theAIsearch's `ai-search.io/papers` is a curated arXiv feed (a human picks papers and writes a plain-language problem / solution / importance breakdown). Yuna treats it as a first-class `rss_aisearch` source. The curator's taste is the filter, so this is a trickle, not a firehose — which fits Yuna's throttled judge.

Unlike Anthropic news, this site **has no native feed and is bot-walled** (Next.js SSR behind a Vercel Security Checkpoint): plain HTTP gets `429`, only a real headless browser returns `200`. So the bridge drives Playwright/Chromium, page-walks `ai-search.io/papers?p=N`, and parses the `__NEXT_DATA__` JSON (`props.pageProps.initialpapers`, 12/page).

**Runs on the GB10 box, not GitHub Actions** — a datacenter IP gets bot-walled harder than the box's residential IP.

### Outputs (committed back, polled by Yuna)

| File | What |
|---|---|
| `aisearch.xml` | RSS 2.0. `<guid>`=paper slug, `<link>`=arxiv abs URL, `<pubDate>`=paper date, `<description>`=CDATA rich body (abstract+summary+problem+solution). **Importance is withheld here** — it is the founder's reveal-after-commit grade seed. |
| `aisearch.json` | Sidecar keyed by paper slug: `{arxiv_id, importance, problem, solution, summary, abstract, authors[], date, arxiv_url, aisearch_url}`. Yuna merges these into the item's `extracted_metadata`. |
| `aisearch-heartbeat.json` | `{status, last_run, last_success, items_total, items_new, pages_walked, error}` — Yuna surfaces staleness via `/health/sources`, so "emits nothing" can't read as healthy. |

Yuna polls the raw URL:

```
https://raw.githubusercontent.com/akao47/yuna-feeds-bridge/main/aisearch.xml
```

### How the scraper works

1. Headless Chromium GETs each `ai-search.io/papers?p=N`, reads `__NEXT_DATA__`.
2. **Forward-only**: walk from `p=1`, collect papers whose slug isn't already in `aisearch.json`, and stop after a full page that's entirely already-seen (dates are day-granular, so a fresh paper can sit after a seen one within a page). Hard page + item caps per run. No full backfill (the corpus is ~10k papers; that would bury the judge).
3. Build the RSS body from abstract+summary+problem+solution; the structured fields go to the sidecar.
4. Write all three files atomically (temp → rename), validating the XML/JSON first.

**Politeness** (this is one person's site): a `From:` contact header, 2-5s jittered gaps between pages, page/item caps, and a kill switch (`AISEARCH_BRIDGE_DISABLED=1` → skip). The plan also includes founder outreach to theAIsearch for a real feed/API. (Note: a bot-identifying *User-Agent* can't be used — the Vercel wall 429s it — so the request UA is a normal browser UA and the contact rides in the `From` header.)

**Loud-fail:** if `__NEXT_DATA__` is missing or most records fail validation (a shape drift), the scraper writes **nothing** to the feed/sidecar and exits non-zero — Yuna keeps serving the last-known-good feed. The heartbeat still updates with `status:error`.

**Config** (env): `AISEARCH_MAX_NEW` (40, the judge-load cap — new papers per run), `AISEARCH_MAX_PAGES` (12, a fetch *safety* bound, not the judge cap), `AISEARCH_RSS_MAX` (100), `AISEARCH_MIN_BODY` (600), `AISEARCH_SLEEP_MIN/MAX` (2/5), `AISEARCH_CONTACT`, `AISEARCH_BRIDGE_DISABLED`. A burst larger than `MAX_NEW` drains over several runs (tracked by `pending_overflow` in the heartbeat).

### Deploy on GB10

```bash
cd ~/yuna-feeds-bridge
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-aisearch.txt
playwright install chromium          # ARM64 Chromium — verify it lands
AISEARCH_PYTHON="$PWD/.venv/bin/python" ./run-aisearch.sh   # one manual run first
# then crontab -e:
#   17 */4 * * * AISEARCH_PYTHON=~/yuna-feeds-bridge/.venv/bin/python ~/yuna-feeds-bridge/run-aisearch.sh >> ~/aisearch-bridge.log 2>&1
```

> **Unverified until deployed:** the Vercel wall has only been confirmed to pass from a residential workstation, semi-interactively. Passing from GB10 specifically (ARM64 Chromium) and *unattended on a cron over time* is the open risk — the loud-fail + heartbeat exist so a regression is visible, not silent.

### Tests

```bash
pip install -r requirements-dev.txt && pytest
```

The page-walk takes an injected fetcher, so the watermark/normalize/emit logic is fully tested against a captured fixture (`tests/fixtures/`) with no network.

## theAIsearch YouTube

theAIsearch also publishes AI-news/tool **roundup videos** — one video covers several tools, each in its own chapter. Yuna's 2026-06-24 pivot treats this channel as the `rss_aisearch_yt` source: the bridge segments each video into per-tool knowledge units and **routes each to one of 7 subjects**, so Yuna files subject-organized knowledge (it no longer grades).

The key shape fact (confirmed with the founder): **the description is only a skeleton** — a timestamp TOC of tool names + links, with *no* explanation. The actual content is in the video. So the bridge needs both: the description gives the segment **boundaries**, the **transcript** gives the content.

**Runs on the GB10 box** — for the residential IP *and* because the per-segment routing call goes to the box's local engine-router (Ultra), so no external LLM key is needed.

### Pipeline (per video)

1. `yt-dlp` lists the channel's recent videos + fetches each one's metadata + transcript (manual/auto **captions**; **faster-whisper ASR fallback** when a video has none).
2. `parse_toc(description)` → segment boundaries (start time + tool name + link). Segment *i* ends where *i+1* starts.
3. The transcript is **sliced** by those boundaries, so each pre-bounded segment carries the words spoken about it.
4. **Ultra** (local engine-router) summarizes each segment + routes it to a subject (or `unsorted`) + decides keep/drop (intros, outros, sponsor reads → dropped).
5. Emit **one RSS item per video** (`<guid>`=video_id) + a sidecar entry keyed by video_id carrying `segments[]`.

### Outputs (committed back, polled by Yuna)

| File | What |
|---|---|
| `aisearch_yt.xml` | RSS 2.0, one item per video. `<guid>`=video_id, `<link>`=watch URL, `<pubDate>`=upload date, `<description>`=CDATA digest of the routed segments. |
| `aisearch_yt.json` | Sidecar keyed by video_id: `{video_id, title, video_url, channel, upload_date, segments[], subjects[], transcript_source, schema_version}`. Each segment = `{start, end, tool_name, tool_url, subject, sub_subject, summary}`. Yuna merges `segments/subjects/transcript_source/schema_version` into the item's `extracted_metadata`. |
| `aisearch_yt-heartbeat.json` | `{status, last_run, last_success, items_total, items_new, videos_listed, empty_videos, error}` — Yuna surfaces staleness via `/health/sources` (`aisearch_yt-heartbeat.json`). |

The routing taxonomy is **2-level** (per the 2026-06-25 taxonomy ADR + conventions C1–C7 in `build_routing_prompt`): a `subject` plus a `sub_subject`, both in `scrape_aisearch_yt.py` (`SUBJECT_SLUGS` + `SUB_SUBJECTS`). The 7 subjects: `claude-code`, `ai-dev-tooling`, `llms-foundation-models`, `retrieval-rag-memory`, `ai-content-generation`, `eval-model-quality`, `local-models-infra`, plus `unsorted`. Each subject has its own sub_subjects (e.g. `ai-content-generation` → image / video / audio-music-tts / 3d-world-motion / generation-setup).

**Loud-fail:** if the channel listing is empty, or more than `AISEARCH_YT_MAX_EMPTY_FRACTION` of a run's new videos yield no usable segments (TOC/transcript drift), the scraper writes **nothing** and exits non-zero (`status:error` heartbeat still written). A single video failing is swallowed to a warning so it can't sink the run.

**Config** (env): `AISEARCH_YT_CHANNEL` (**confirm the exact handle on first deploy**), `AISEARCH_YT_MAX_NEW` (5, the Ultra-cost cap — new videos per run), `AISEARCH_YT_MAX_LIST` (30), `AISEARCH_YT_RSS_MAX` (100), `AISEARCH_YT_MAX_EMPTY_FRACTION` (0.75), `AISEARCH_YT_LLM_BASE_URL` (`http://localhost:18765/v1`), `AISEARCH_YT_LLM_MODEL` (`ultra/default`), `AISEARCH_YT_WHISPER_MODEL` (`base.en`), `AISEARCH_YT_BRIDGE_DISABLED`.

### Deploy on GB10

```bash
cd ~/yuna-feeds-bridge
python3 -m venv .venv-yt && . .venv-yt/bin/activate
pip install -r requirements-aisearch-yt.txt
# ffmpeg must be on PATH (audio extract + ASR). On the box: apt-get install ffmpeg
AISEARCH_YT_CHANNEL="https://www.youtube.com/@theAIsearch/videos" \
  AISEARCH_YT_PYTHON="$PWD/.venv-yt/bin/python" ./run-aisearch-yt.sh   # one manual run first
# then crontab -e (offset from the arXiv bridge so the two don't push the same minute):
#   23 */4 * * * AISEARCH_YT_PYTHON=~/yuna-feeds-bridge/.venv-yt/bin/python ~/yuna-feeds-bridge/run-aisearch-yt.sh >> ~/aisearch-yt-bridge.log 2>&1
```

> **Unverified until deployed:** the TOC parser, yt-dlp caption shape, and Ultra routing have only been tested against fixtures. The first GB10 run on a real video is the founder's routing-confirmation step (does each tool land in the right subject?) — the loud-fail + heartbeat make a regression visible, not silent. `faster-whisper` on GB10 (ARM64/Blackwell) is the other open risk; captions cover most theAIsearch videos, so ASR is a fallback.

### Tests

```bash
pip install -r requirements-dev.txt && pytest tests/test_aisearch_yt.py
```

The impure edge (yt-dlp / faster-whisper / Ultra) is imported inside the functions that use it, so the pure transforms (TOC parse, VTT parse, slice, routing parse/assemble, RSS/sidecar/heartbeat) are fully tested with no network or model deps — including an end-to-end pipeline asserting the sidecar shape matches Yuna's `rss.py` contract.

## License

Personal/scratch project. No license declared.
