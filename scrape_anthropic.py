"""Scrape anthropic.com/news → emit RSS XML to anthropic.xml.

Yuna polls the raw GitHub URL of anthropic.xml every 4h. This script runs on
a GitHub Actions cron (17 */4 * * *) and commits the updated XML back.

Why: anthropic.com/news has no native RSS feed (verified 2026-05-20 — tried
/rss.xml, /news/feed, /feed; all 404; no <link rel="alternate"> declared).

Caveats:
- pubDate is scrape time, not actual publish date. Each individual article
  page might have a date, but fetching N pages per scrape would 4x bandwidth.
  Acceptable trade-off; Yuna's dedup uses GUID = slug (stable per article).
- Title is extracted from the anchor's visible text. If Anthropic restructures
  the page, the regex breaks and the XML becomes empty — Yuna's poller will
  record consecutive_failures > 0, surfacing the regression.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import sys
from pathlib import Path

import httpx


NEWS_URL = "https://www.anthropic.com/news"
OUTPUT_PATH = Path(__file__).parent / "anthropic.xml"
REQUEST_TIMEOUT = 30.0
USER_AGENT = "yuna-feeds-bridge/1.0 (+https://github.com/akao47/yuna-feeds-bridge)"


# Matches an anchor pointing to an Anthropic news article. The visible text is
# captured as a rough title proxy.
_ARTICLE_RE = re.compile(
    r'<a[^>]+href="(/news/[a-z0-9][a-z0-9-]*)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _slug_to_title(slug: str) -> str:
    """Fallback when we can't pull a clean title from the anchor."""
    return slug.replace("-", " ").title()


def _clean_text(raw: str) -> str:
    """Strip nested tags + collapse whitespace."""
    no_tags = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def scrape() -> list[dict]:
    """Return de-duped list of {slug, title, url} for current /news/ articles."""
    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        r = client.get(NEWS_URL, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
    seen: dict[str, dict] = {}
    for match in _ARTICLE_RE.finditer(r.text):
        path = match.group(1).rstrip("/")
        slug = path.rsplit("/", 1)[-1]
        if slug in seen:
            continue
        title = _clean_text(match.group(2)) or _slug_to_title(slug)
        seen[slug] = {
            "slug": slug,
            "title": title[:240],  # cap absurd titles
            "url": f"https://www.anthropic.com{path}",
        }
    return list(seen.values())


def build_rss(items: list[dict], now: dt.datetime) -> str:
    """Produce RSS 2.0 XML. UTC pubDate on each item is the scrape time —
    Yuna dedups via stable <guid>=slug so this doesn't double-ingest.
    """
    pub = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "  <channel>",
        "    <title>Anthropic News (yuna-feeds-bridge)</title>",
        f"    <link>{NEWS_URL}</link>",
        "    <description>RSS bridge for anthropic.com/news, scraped 4-hourly by GitHub Actions.</description>",
        f"    <lastBuildDate>{pub}</lastBuildDate>",
    ]
    for item in items:
        title = html.escape(item["title"], quote=False)
        url = html.escape(item["url"], quote=False)
        guid = html.escape(item["slug"], quote=False)
        lines += [
            "    <item>",
            f"      <title>{title}</title>",
            f"      <link>{url}</link>",
            f'      <guid isPermaLink="false">{guid}</guid>',
            f"      <pubDate>{pub}</pubDate>",
            "    </item>",
        ]
    lines += ["  </channel>", "</rss>", ""]
    return "\n".join(lines)


def main() -> int:
    items = scrape()
    if not items:
        # Loud-fail: empty article list almost certainly means the regex
        # broke after an upstream restructure. Don't overwrite a working
        # XML file with empty content.
        print("ERROR: no articles parsed from /news. Refusing to overwrite anthropic.xml.", file=sys.stderr)
        return 2
    xml = build_rss(items, dt.datetime.now(dt.timezone.utc))
    OUTPUT_PATH.write_text(xml, encoding="utf-8", newline="\n")
    print(f"wrote {len(items)} items to {OUTPUT_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
