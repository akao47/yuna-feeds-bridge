# yuna-feeds-bridge

RSS bridge for sources that don't publish a native feed. Currently:

- **Anthropic news** (`anthropic.xml`) — scraped from <https://www.anthropic.com/news> every 4 hours via GitHub Actions.

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

## License

Personal/scratch project. No license declared.
