"""Scrape ai-search.io/papers (theAIsearch's curated arXiv picks) → emit a clean
RSS feed + a structured sidecar for Yuna's `rss_aisearch` source.

ai-search.io is a Next.js SSR app behind a Vercel bot wall: plain httpx gets HTTP
429 + a Security Checkpoint, only a real headless browser returns 200 (verified
2026-06-15). So this bridge drives Playwright/Chromium to fetch each page, then
parses the `__NEXT_DATA__` blob — `props.pageProps.{initialpapers,hasMore,
currentPage}`, 12 papers/page. Each paper has exactly 10 fields: _id (URL slug),
title, authors (comma-separated, newline-terminated), abstract, arxiv (full URL),
summary, problem, solution, importance, date (YYYY-MM-DD, day-granular).

Runs on the GB10 box (its residential IP clears the wall) on a cron; run-aisearch.sh
git-commits the three output files back to this repo, and Yuna polls the raw GitHub
URL of aisearch.xml via its existing RSS path (see README).

Design authority: Yuna planning/designs/O11-main-design-20260615-160239.md + the
T-2c eng-review outcome:
  D-A1  bridge runtime = GB10 (residential), git-push the feed.
  D-A2  forward-only ingest by a seen-_id watermark (NOT date — dates are day-
        granular and the curator can reorder), walk from p=1, stop on the first
        fully-all-seen page. The new-item cap (not the page cap) bounds judge load;
        a burst larger than one run drains over several runs (pending-overflow).
  D-A5  rich body (abstract+summary+problem+solution) so the judge claim is not a
        bare title; the structured fields ride in a sidecar (prose fidelity).
  D-T1  politeness REQUIRED: rate-limit+jitter, a `From` contact header (a bot UA
        is walled by Vercel), item/page caps, a kill switch.
  D-T2  `importance` is the founder's reveal-after-commit grade seed — it lives in
        the sidecar ONLY, never in the judge-visible RSS body.

Loud-fail: on shape drift (missing __NEXT_DATA__ / initialpapers, or most records
failing validation) the scraper writes NOTHING to the feed/sidecar and exits
non-zero, so the cron run goes red and Yuna keeps serving the last-known-good feed.
The heartbeat is always written so "emits nothing" can't read as healthy.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape


# --- config (env-overridable) ------------------------------------------------

BASE_URL = os.environ.get("AISEARCH_URL", "https://ai-search.io/papers")
ROOT = Path(__file__).parent
FEED_PATH = ROOT / "aisearch.xml"
SIDECAR_PATH = ROOT / "aisearch.json"
HEARTBEAT_PATH = ROOT / "aisearch-heartbeat.json"

# MAX_NEW_ITEMS is the judge-load cap: at most this many NEW papers enter per run,
# so a curated trickle fits Yuna's ~13/h judge (a firehose would recreate the
# legacy-evaluate-storm backlog, D-A2). MAX_WALK_PAGES is only a fetch safety bound
# — it must NOT be the binding cap, or a burst larger than its reach silently
# orphans the overflow (already-seen pages cost a fetch but add zero judge load, so
# this can be generous). A burst > MAX_NEW_ITEMS drains across runs via the
# pending-overflow watermark in walk_pages.
MAX_NEW_ITEMS = int(os.environ.get("AISEARCH_MAX_NEW", "40"))
MAX_WALK_PAGES = int(os.environ.get("AISEARCH_MAX_PAGES", "12"))
RSS_MAX_ITEMS = int(os.environ.get("AISEARCH_RSS_MAX", "100"))

# A paper whose abstract+summary+problem+solution is shorter than this is too thin
# for the Academy judge (which reads it as the item body); Yuna's THIN_BODY_CHARS
# is 600, so we match it. Below it = drop the record (or, en masse, shape drift).
MIN_BODY_CHARS = int(os.environ.get("AISEARCH_MIN_BODY", "600"))
# If more than this fraction of a run's collected papers fail validation, the page
# shape almost certainly drifted — loud-fail rather than ingest a degraded feed.
MAX_INVALID_FRACTION = float(os.environ.get("AISEARCH_MAX_INVALID_FRACTION", "0.5"))

# A real-browser UA is REQUIRED, not a nicety: the Vercel wall 429s a bot-identifying
# UA (verified 2026-06-15 — "yuna-feeds-bridge/1.0 ..." gets the Security Checkpoint,
# a Chrome UA gets 200). D-T1's contact intent moves to the standard `From` header
# (also verified to pass the wall) + the rate limits/caps/kill-switch below.
CONTACT = os.environ.get("AISEARCH_CONTACT", "alex.kao92@gmail.com")
BROWSER_USER_AGENT = os.environ.get(
    "AISEARCH_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
SLEEP_MIN = float(os.environ.get("AISEARCH_SLEEP_MIN", "2.0"))
SLEEP_MAX = float(os.environ.get("AISEARCH_SLEEP_MAX", "5.0"))
NAV_TIMEOUT_MS = int(os.environ.get("AISEARCH_NAV_TIMEOUT_MS", "30000"))
NAV_RETRIES = int(os.environ.get("AISEARCH_NAV_RETRIES", "2"))

REQUIRED_FIELDS = (
    "_id", "title", "authors", "abstract", "arxiv",
    "summary", "problem", "solution", "importance", "date",
)

# Modern arXiv id (YYMM.NNNNN) with an optional version suffix; mirrors Yuna's
# curation/dedup.py canonicalize_arxiv_id so both sides key dedup on the same id.
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# C0 control chars XML 1.0 forbids even inside CDATA (all except \t \n \r). PDF-
# pasted abstracts carry stray form-feeds / vertical-tabs; left in, they make the
# hand-built XML un-parseable (ET.fromstring raises). Strip them at normalization.
_XML_BAD_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(text: str) -> str:
    """Strip XML-1.0-illegal control chars from a scraped string."""
    return _XML_BAD_CTRL_RE.sub("", text)


class BridgeShapeError(RuntimeError):
    """The page shape drifted (no __NEXT_DATA__ / no papers / mass invalidity).

    Raised so main() loud-fails instead of overwriting a working feed with junk.
    """


# --- pure transforms (no network; unit-tested) -------------------------------

def canonicalize_arxiv_id(raw: str | None) -> str | None:
    """Bare, version-stripped arXiv id from a URL or id, else None."""
    if not raw:
        return None
    match = _ARXIV_ID_RE.search(raw)
    return match.group(1) if match else None


def normalize_authors(raw: str | None) -> list[str]:
    """Comma-separated, newline-terminated author string → clean list of names."""
    if not raw:
        return []
    return [a.strip() for a in raw.replace("\n", " ").split(",") if a.strip()]


def build_body(paper: dict) -> str:
    """Rich judge body: abstract + summary + problem + solution.

    `importance` is deliberately excluded (D-T2 — it is the reveal-after-commit
    grade seed, kept in the sidecar only).
    """
    parts = [
        (paper.get("abstract") or "").strip(),
        (paper.get("summary") or "").strip(),
    ]
    problem = (paper.get("problem") or "").strip()
    solution = (paper.get("solution") or "").strip()
    if problem:
        parts.append(f"Problem: {problem}")
    if solution:
        parts.append(f"Solution: {solution}")
    return "\n\n".join(p for p in parts if p)


def validate_paper(paper: dict) -> list[str]:
    """Return a list of validation problems (empty = valid). Used for loud-fail."""
    errs: list[str] = []
    for field in REQUIRED_FIELDS:
        value = paper.get(field)
        if not isinstance(value, str) or not value.strip():
            errs.append(f"missing/empty:{field}")
    arxiv = paper.get("arxiv")
    if isinstance(arxiv, str) and arxiv.strip() and canonicalize_arxiv_id(arxiv) is None:
        errs.append("arxiv:no-canonical-id")
    date = paper.get("date")
    if isinstance(date, str) and date.strip():
        # Parse, don't just regex-match: the regex accepts impossible calendar dates
        # (2026-02-31) that would later crash date_to_rfc822 on the whole run.
        if not _DATE_RE.match(date.strip()):
            errs.append("date:bad-format")
        else:
            try:
                datetime.strptime(date.strip(), "%Y-%m-%d")
            except ValueError:
                errs.append("date:bad-format")
    body_len = len(build_body(paper))
    if body_len < MIN_BODY_CHARS:
        errs.append(f"body-too-thin:{body_len}")
    return errs


def sidecar_entry(paper: dict) -> dict:
    """Normalized full paper for the sidecar — the fields Yuna's rss.py merges into
    extracted_metadata (importance + arxiv_id + problem/solution) plus enough to
    rebuild the RSS body and render a vault DTO later (P1: never schema-merged)."""
    # _clean strips XML-illegal control chars so the sidecar — and the RSS built
    # from it — are always well-formed (the feed is derived from these entries).
    return {
        "_id": _clean(paper["_id"]),
        "title": _clean(paper["title"].strip()),
        "authors": [_clean(a) for a in normalize_authors(paper["authors"])],
        "abstract": _clean(paper["abstract"].strip()),
        "summary": _clean(paper["summary"].strip()),
        "problem": _clean(paper["problem"].strip()),
        "solution": _clean(paper["solution"].strip()),
        "importance": _clean(paper["importance"].strip()),
        "arxiv_url": _clean(paper["arxiv"].strip()),
        "arxiv_id": canonicalize_arxiv_id(paper["arxiv"]),
        "aisearch_url": f"https://ai-search.io/papers/{_clean(paper['_id'])}",
        "date": paper["date"].strip(),
    }


def date_to_rfc822(date_str: str) -> str:
    """'YYYY-MM-DD' → RFC822 pubDate at 00:00:00 UTC (day-granular source)."""
    dt = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def _cdata(text: str) -> str:
    """Wrap text in CDATA, splitting any literal ']]>' so it can't close early."""
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build_rss(sidecar: dict, max_items: int, build_date_rfc822: str) -> str:
    """RSS 2.0 from the sidecar (newest `date` first, capped). The body rides in a
    CDATA <description> so the rich prose isn't HTML-mangled (D-A5)."""
    entries = sorted(
        sidecar.values(), key=lambda e: (e.get("date", ""), e.get("_id", "")), reverse=True
    )[:max_items]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "  <channel>",
        "    <title>ai-search.io curated papers (yuna-feeds-bridge)</title>",
        f"    <link>{xml_escape(BASE_URL)}</link>",
        "    <description>theAIsearch's curated arXiv picks, bridged for Yuna's "
        "rss_aisearch source. Importance framing is withheld here (grade seed).</description>",
        f"    <lastBuildDate>{build_date_rfc822}</lastBuildDate>",
    ]
    for entry in entries:
        title = xml_escape(entry["title"])
        link = xml_escape(entry["arxiv_url"])
        guid = xml_escape(entry["_id"])
        pub = date_to_rfc822(entry["date"])
        body = build_body(entry)
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


def walk_pages(fetch_page, seen_ids: set[str], max_walk_pages: int, max_new: int,
               pending_overflow: bool = False):
    """Forward-only page walk (D-A2). Returns (new_papers, pages_walked, stop_reason,
    next_pending_overflow).

    `fetch_page(n)` returns {'initialpapers', 'hasMore', 'currentPage'} or None.
    Collects papers whose _id isn't already seen (NOT keyed on date — dates are
    day-granular and the curator can reorder). Stops on the first fully-all-seen
    page (everything below it is seen-or-older = known territory), or at the new-item
    cap, or at end-of-corpus, or at the page safety bound.

    Cross-run soundness: a run that ends at the new-item cap leaves overflow papers
    BELOW an already-seen prefix, so the next run must NOT trust the all-seen stop on
    that leading prefix. `pending_overflow` (persisted from the prior run) suppresses
    the all-seen stop until this run reaches the overflow (collects something), then
    the normal stop resumes. `next_pending_overflow` is True only when this run again
    ended at the item cap. Without this, a burst > one run's MAX_NEW reach would
    orphan the overflow forever (the all-seen prefix re-anchors the walk at page 1).

    Raises BridgeShapeError if page 1 yields no papers (drift / wall).
    """
    collected: list[dict] = []
    run_seen = set(seen_ids)  # copy: don't mutate the caller's watermark
    hit_unseen_this_run = False
    pages = 0
    stop_reason = "walk_cap"  # default if the for-loop runs to the safety bound
    for n in range(1, max_walk_pages + 1):
        page = fetch_page(n)
        pages = n
        papers = (page or {}).get("initialpapers") if page else None
        if n == 1 and not papers:
            raise BridgeShapeError("page 1 returned no __NEXT_DATA__ / initialpapers")
        if not papers:
            stop_reason = "fetch_gap"  # a later page failed/empty; catch up next run
            break
        current = page.get("currentPage")
        if current is not None and n > 1 and current != n:
            stop_reason = "end_clamp"  # site clamped ?p=N back to an earlier page
            break
        unseen = [p for p in papers if p.get("_id") and p["_id"] not in run_seen]
        for p in unseen:
            run_seen.add(p["_id"])
        collected.extend(unseen)
        if unseen:
            hit_unseen_this_run = True
        if len(collected) >= max_new:
            collected = collected[:max_new]
            stop_reason = "item_cap"  # overflow may remain below → stay pending
            break
        if not unseen:
            # All-seen page. Normally we've reached known territory and stop. But if
            # a prior run left overflow and we haven't reached it yet this run, this
            # is a LEADING seen prefix — walk through it to get to the overflow (only
            # if there's a page below; an all-seen LAST page means no overflow exists).
            if pending_overflow and not hit_unseen_this_run and page.get("hasMore"):
                continue
            stop_reason = "caught_up"
            break
        if not page.get("hasMore"):
            stop_reason = "end"
            break
    next_pending = stop_reason == "item_cap"
    return collected, pages, stop_reason, next_pending


def build_heartbeat(status: str, items_total: int, items_new: int,
                    pages_walked: int, error: str | None, now_iso: str,
                    stop_reason: str | None = None,
                    pending_overflow: bool = False) -> dict:
    return {
        "source": "rss_aisearch",
        "status": status,                # "ok" | "error"
        "last_run": now_iso,
        "last_success": now_iso if status == "ok" else None,
        "items_total": items_total,
        "items_new": items_new,
        "pages_walked": pages_walked,
        "stop_reason": stop_reason,      # caught_up | item_cap | end | end_clamp | fetch_gap | walk_cap
        "pending_overflow": pending_overflow,  # a burst is still draining across runs
        "error": error,
    }


# --- IO + browser (the impure edge) ------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(e: object) -> str:
    """First line of an error, capped — Playwright errors carry a huge ASCII banner
    we don't want bloating the committed heartbeat."""
    return str(e).splitlines()[0][:300] if str(e).strip() else repr(e)


def _load_json(path: Path) -> dict | None:
    """Load a JSON object, or None on missing/corrupt/non-dict (so callers' `or {}`
    fallback covers every bad-input case rather than crashing on .keys())."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write(path: Path, content: str) -> None:
    """Write to a temp sibling then os.replace — readers never see a half file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _write_heartbeat_only(hb: dict) -> None:
    _atomic_write(HEARTBEAT_PATH, json.dumps(hb, indent=2) + "\n")


def _publish(rss: str, sidecar: dict, hb: dict) -> None:
    """Validate then atomically write all three outputs (temp → rename). Order matters:
    sidecar before feed, so a mid-publish kill can only leave a sidecar entry with no
    RSS item (Yuna ingests nothing extra) rather than an RSS item with no metadata.
    Heartbeat last, so a partial write under-reports health (the safe direction)."""
    ET.fromstring(rss)  # raises on malformed XML before we touch the live feed
    sidecar_json = json.dumps(sidecar, ensure_ascii=False, sort_keys=True, indent=1)
    json.loads(sidecar_json)  # round-trip guard
    _atomic_write(SIDECAR_PATH, sidecar_json + "\n")
    _atomic_write(FEED_PATH, rss)
    _atomic_write(HEARTBEAT_PATH, json.dumps(hb, indent=2) + "\n")


def _make_browser_fetcher(page):
    """Real fetch_page over a Playwright page: navigate (with retries + politeness
    jitter) and read the parsed __NEXT_DATA__ pageProps."""
    def fetch(n: int):
        if n > 1:
            time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))  # politeness (D-T1)
        url = BASE_URL if n == 1 else f"{BASE_URL}?p={n}"
        last_err: Exception | None = None
        for attempt in range(NAV_RETRIES + 1):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                data = page.evaluate(
                    "() => { const e = document.getElementById('__NEXT_DATA__');"
                    " return e ? JSON.parse(e.textContent) : null; }"
                )
                if not data:
                    return None
                pp = (data.get("props") or {}).get("pageProps") or {}
                return {
                    "initialpapers": pp.get("initialpapers"),
                    "hasMore": pp.get("hasMore"),
                    "currentPage": pp.get("currentPage"),
                }
            except Exception as e:  # transient nav/timeout — back off and retry
                last_err = e
                if attempt < NAV_RETRIES:
                    time.sleep(2 ** attempt)
        print(f"warn: page {n} fetch failed after retries: {last_err}", file=sys.stderr)
        return None
    return fetch


def main() -> int:
    if os.environ.get("AISEARCH_BRIDGE_DISABLED"):
        print("AISEARCH_BRIDGE_DISABLED set; skipping scrape.", file=sys.stderr)
        return 0

    sidecar = _load_json(SIDECAR_PATH) or {}
    seen_ids = set(sidecar.keys())
    prior_hb = _load_json(HEARTBEAT_PATH) or {}
    pending_overflow = bool(prior_hb.get("pending_overflow"))
    now = _now_iso()

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    user_agent=BROWSER_USER_AGENT,
                    extra_http_headers={"From": CONTACT},  # polite contact (D-T1)
                )
                fetch = _make_browser_fetcher(ctx.new_page())
                collected, pages_walked, stop_reason, next_pending = walk_pages(
                    fetch, seen_ids, MAX_WALK_PAGES, MAX_NEW_ITEMS, pending_overflow
                )
            finally:
                browser.close()
    except BridgeShapeError as e:
        _write_heartbeat_only(build_heartbeat(
            "error", len(sidecar), 0, 0, f"shape: {_err(e)}", now,
            pending_overflow=pending_overflow))
        print(f"ERROR shape drift: {_err(e)}", file=sys.stderr)
        return 2
    except Exception as e:
        _write_heartbeat_only(build_heartbeat(
            "error", len(sidecar), 0, 0, f"scrape failed: {_err(e)}", now,
            pending_overflow=pending_overflow))
        print(f"ERROR scrape failed: {_err(e)}", file=sys.stderr)
        return 2

    # Everything past the scrape (validate → build → publish) is also guarded, so a
    # crash here writes an error heartbeat instead of dying with a stale "ok" one.
    try:
        valid, dropped = [], []
        for paper in collected:
            errs = validate_paper(paper)
            if errs:
                dropped.append((paper.get("_id"), errs))
            else:
                valid.append(paper)

        # Mass invalidity within a non-empty harvest = the page shape drifted.
        if collected and len(valid) < (1.0 - MAX_INVALID_FRACTION) * len(collected):
            _write_heartbeat_only(build_heartbeat(
                "error", len(sidecar), 0, pages_walked,
                f"shape drift: {len(dropped)}/{len(collected)} invalid; e.g. {dropped[:3]}",
                now, stop_reason=stop_reason, pending_overflow=pending_overflow))
            print(f"ERROR shape drift: {len(dropped)}/{len(collected)} invalid", file=sys.stderr)
            return 2

        added = 0
        for paper in valid:
            # Items are immutable once emitted (Codex #7): never overwrite an _id.
            if paper["_id"] not in sidecar:
                sidecar[paper["_id"]] = sidecar_entry(paper)
                added += 1

        rss = build_rss(sidecar, RSS_MAX_ITEMS, date_to_rfc822(now[:10]))
        hb = build_heartbeat("ok", len(sidecar), added, pages_walked, None, now,
                             stop_reason=stop_reason, pending_overflow=next_pending)
        _publish(rss, sidecar, hb)
    except Exception as e:
        _write_heartbeat_only(build_heartbeat(
            "error", len(sidecar), 0, pages_walked, f"publish failed: {_err(e)}", now,
            stop_reason=stop_reason, pending_overflow=pending_overflow))
        print(f"ERROR publish failed: {_err(e)}", file=sys.stderr)
        return 2

    print(f"ok: +{added} new ({len(dropped)} dropped), {len(sidecar)} total, "
          f"{pages_walked} page(s) walked, stop={stop_reason}"
          + (", draining-overflow" if next_pending else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
