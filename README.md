# yuna-feeds-bridge

RSS bridge for sources that don't publish a native feed. Currently:

- **Anthropic news** (`anthropic.xml`) — scraped from <https://www.anthropic.com/news> every 4 hours via GitHub Actions.
- **ai-search.io curated papers** (`aisearch.xml` + `aisearch.json`) — theAIsearch's curated arXiv picks, scraped from <https://ai-search.io/papers> on the GB10 box (see "ai-search.io curated papers" below).

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

## License

Personal/scratch project. No license declared.
