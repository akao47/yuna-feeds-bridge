"""Unit tests for the ai-search.io bridge. No network: the page-walk takes an
injected fetcher, and the transforms run against a real captured fixture
(tests/fixtures/aisearch-page1.json)."""

import json
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest

import scrape_aisearch as bridge


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "aisearch-page1.json").read_text("utf-8")
)


def fake_fetcher(pages):
    """fetch(n) → pages[n-1] (1-indexed) or None past the end."""
    def fetch(n):
        return pages[n - 1] if 1 <= n <= len(pages) else None
    return fetch


def page(papers, has_more=False, current=1):
    return {"initialpapers": papers, "hasMore": has_more, "currentPage": current}


# --- fixture sanity ----------------------------------------------------------

def test_fixture_papers_all_valid():
    assert len(FIXTURE) == 2
    for paper in FIXTURE:
        assert bridge.validate_paper(paper) == []


# --- canonicalize_arxiv_id ---------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("https://arxiv.org/abs/2605.00553", "2605.00553"),
    ("https://arxiv.org/pdf/2401.12345v3", "2401.12345"),
    ("2605.00809", "2605.00809"),
    ("arXiv:2401.12345v2", "2401.12345"),
    ("not-an-arxiv-id", None),
    ("", None),
    (None, None),
])
def test_canonicalize_arxiv_id(raw, expected):
    assert bridge.canonicalize_arxiv_id(raw) == expected


def test_canonicalize_matches_fixture():
    assert bridge.canonicalize_arxiv_id(FIXTURE[0]["arxiv"]) == "2605.00553"


# --- normalize_authors -------------------------------------------------------

def test_normalize_authors_comma_newline():
    assert bridge.normalize_authors("Ada Lovelace, Alan Turing\n") == [
        "Ada Lovelace", "Alan Turing"
    ]


def test_normalize_authors_empty():
    assert bridge.normalize_authors("") == []
    assert bridge.normalize_authors(None) == []


def test_normalize_authors_fixture():
    authors = bridge.normalize_authors(FIXTURE[0]["authors"])
    assert authors[0] == "Minchan Kwon"
    assert authors[-1] == "Junmo Kim"
    assert all(a == a.strip() and a for a in authors)


# --- build_body (D-T2: importance excluded) ----------------------------------

def test_build_body_includes_problem_solution_not_importance():
    paper = FIXTURE[0]
    body = bridge.build_body(paper)
    assert paper["abstract"] in body
    assert paper["summary"] in body
    assert "Problem: " in body and paper["problem"] in body
    assert "Solution: " in body and paper["solution"] in body
    # D-T2: the curator's importance framing is the grade seed — never in the body.
    assert paper["importance"] not in body
    assert "trustworthy AI systems" not in body


def test_build_body_real_papers_clear_richness_bar():
    for paper in FIXTURE:
        assert len(bridge.build_body(paper)) >= bridge.MIN_BODY_CHARS


def test_build_body_handles_missing_optional_parts():
    thin = {"abstract": "a", "summary": "b", "problem": "", "solution": ""}
    assert bridge.build_body(thin) == "a\n\nb"


# --- validate_paper ----------------------------------------------------------

def test_validate_flags_thin_body():
    thin = dict(FIXTURE[0], abstract="x", summary="y", problem="", solution="")
    errs = bridge.validate_paper(thin)
    assert any(e.startswith("body-too-thin") for e in errs)


def test_validate_flags_missing_field():
    missing = dict(FIXTURE[0])
    missing["arxiv"] = ""
    errs = bridge.validate_paper(missing)
    assert "missing/empty:arxiv" in errs


def test_validate_flags_noncanonical_arxiv():
    bad = dict(FIXTURE[0], arxiv="https://example.com/whatever")
    assert "arxiv:no-canonical-id" in bridge.validate_paper(bad)


def test_validate_flags_bad_date():
    bad = dict(FIXTURE[0], date="May 4 2026")
    assert "date:bad-format" in bridge.validate_paper(bad)


# --- sidecar_entry -----------------------------------------------------------

def test_sidecar_entry_shape():
    entry = bridge.sidecar_entry(FIXTURE[0])
    assert entry["arxiv_id"] == "2605.00553"
    assert entry["arxiv_url"] == "https://arxiv.org/abs/2605.00553"
    assert entry["importance"] == FIXTURE[0]["importance"].strip()
    assert isinstance(entry["authors"], list) and entry["authors"][0] == "Minchan Kwon"
    assert entry["aisearch_url"].endswith(FIXTURE[0]["_id"])
    assert entry["date"] == "2026-05-04"


# --- date_to_rfc822 ----------------------------------------------------------

def test_date_to_rfc822():
    out = bridge.date_to_rfc822("2026-05-04")
    assert "04 May 2026 00:00:00 +0000" in out
    # round-trips back to the same calendar day
    parsed = datetime.strptime(out, "%a, %d %b %Y %H:%M:%S %z")
    assert (parsed.year, parsed.month, parsed.day) == (2026, 5, 4)


# --- _cdata ------------------------------------------------------------------

def test_cdata_splits_terminator():
    # The real invariant: a body containing ']]>' must survive CDATA-wrapping and
    # round-trip back to the exact original through an XML parser (no early close).
    raw = "danger ]]> here"
    root = ET.fromstring("<r>" + bridge._cdata(raw) + "</r>")
    assert root.text == raw


# --- build_rss ---------------------------------------------------------------

def make_sidecar(papers):
    return {p["_id"]: bridge.sidecar_entry(p) for p in papers}


def test_build_rss_parses_and_maps_fields():
    sidecar = make_sidecar(FIXTURE)
    rss = bridge.build_rss(sidecar, max_items=100, build_date_rfc822=bridge.date_to_rfc822("2026-05-04"))
    root = ET.fromstring(rss)  # must be well-formed
    items = root.findall("./channel/item")
    assert len(items) == 2
    guids = {it.find("guid").text for it in items}
    assert guids == set(sidecar.keys())
    for it in items:
        _id = it.find("guid").text
        assert it.find("link").text == sidecar[_id]["arxiv_url"]
        assert it.find("guid").get("isPermaLink") == "false"
        assert "00:00:00 +0000" in it.find("pubDate").text
        assert it.find("description").text  # body present


def test_build_rss_excludes_importance_text():
    sidecar = make_sidecar(FIXTURE)
    rss = bridge.build_rss(sidecar, max_items=100, build_date_rfc822="x")
    # D-T2: no entry's importance prose may appear anywhere in the feed.
    for entry in sidecar.values():
        assert entry["importance"] not in rss


def test_build_rss_caps_items_newest_first():
    older = dict(FIXTURE[0], _id="older-paper", date="2025-01-01")
    sidecar = make_sidecar([FIXTURE[0], FIXTURE[1], older])
    rss = bridge.build_rss(sidecar, max_items=2, build_date_rfc822="x")
    root = ET.fromstring(rss)
    items = root.findall("./channel/item")
    assert len(items) == 2  # cap honored
    guids = {it.find("guid").text for it in items}
    assert "older-paper" not in guids  # oldest dropped first


# --- walk_pages (D-A2 forward-only watermark) --------------------------------

def ids(papers):
    return [p["_id"] for p in papers]


def walk(fetch, seen, max_walk_pages=12, max_new=40, pending=False):
    """Thin wrapper: returns (collected, pages, stop_reason, next_pending)."""
    return bridge.walk_pages(fetch, seen, max_walk_pages, max_new, pending)


def test_walk_single_page_end():
    fetch = fake_fetcher([page(FIXTURE, has_more=False)])
    collected, pages, reason, pend = walk(fetch, set())
    assert ids(collected) == ids(FIXTURE)
    assert pages == 1 and reason == "end" and pend is False


def test_walk_all_seen_stops_immediately():
    seen = {p["_id"] for p in FIXTURE}
    fetch = fake_fetcher([page(FIXTURE, has_more=True)])
    collected, pages, reason, pend = walk(fetch, seen)
    assert collected == [] and pages == 1 and reason == "caught_up"


def test_walk_collects_new_after_seen_within_page():
    # day-granular: a fresh paper can sit AFTER a seen one in the same page — we
    # must not stop at the first seen id, only after a full all-seen page.
    seen = {FIXTURE[1]["_id"]}
    mixed = [FIXTURE[0], FIXTURE[1]]  # new, then seen
    fetch = fake_fetcher([page(mixed, has_more=False)])
    collected, _, _, _ = walk(fetch, seen)
    assert ids(collected) == [FIXTURE[0]["_id"]]


def test_walk_stops_at_first_all_seen_page():
    p1 = [dict(FIXTURE[0], _id="new-a"), dict(FIXTURE[0], _id="new-b")]
    p2 = [dict(FIXTURE[0], _id="seen-x")]
    fetch = fake_fetcher([page(p1, has_more=True), page(p2, has_more=True, current=2)])
    collected, pages, reason, _ = walk(fetch, {"seen-x"})
    assert ids(collected) == ["new-a", "new-b"]
    assert pages == 2 and reason == "caught_up"


def test_walk_respects_max_new():
    p1 = [dict(FIXTURE[0], _id=f"id-{i}") for i in range(10)]
    fetch = fake_fetcher([page(p1, has_more=True)])
    collected, _, reason, pend = walk(fetch, set(), max_new=4)
    assert len(collected) == 4 and reason == "item_cap" and pend is True


def test_walk_respects_page_safety_cap():
    pages_in = [page([dict(FIXTURE[0], _id=f"p{n}-{i}") for i in range(2)],
                     has_more=True, current=n) for n in range(1, 6)]
    fetch = fake_fetcher(pages_in)
    collected, pages, reason, _ = walk(fetch, set(), max_walk_pages=2, max_new=999)
    assert pages == 2 and len(collected) == 4 and reason == "walk_cap"


def test_walk_page1_empty_raises_shape_error():
    fetch = fake_fetcher([page([], has_more=False)])
    with pytest.raises(bridge.BridgeShapeError):
        walk(fetch, set())


def test_walk_stops_on_currentpage_clamp():
    p1 = [dict(FIXTURE[0], _id="new-a")]
    # site clamps ?p=2 back to page 1 (currentPage=1 != requested 2) → end of corpus
    clamped = page([dict(FIXTURE[0], _id="new-a")], has_more=True, current=1)
    fetch = fake_fetcher([page(p1, has_more=True, current=1), clamped])
    collected, pages, reason, _ = walk(fetch, set())
    assert pages == 2 and ids(collected) == ["new-a"] and reason == "end_clamp"


def _burst_corpus():
    # newest-first, 3/page: 7 new + 2 old; a real fetcher returns the same corpus
    # each run (the site is fixed) — the watermark advances between runs.
    pages_in = [
        page([dict(FIXTURE[0], _id=i) for i in ["N1", "N2", "N3"]], has_more=True, current=1),
        page([dict(FIXTURE[0], _id=i) for i in ["N4", "N5", "N6"]], has_more=True, current=2),
        page([dict(FIXTURE[0], _id=i) for i in ["N7", "O1", "O2"]], has_more=False, current=3),
    ]
    return lambda: fake_fetcher(pages_in)


def test_walk_burst_drains_overflow_across_runs():
    # THE blocker regression: a burst bigger than one run's item cap must drain over
    # runs, not be orphaned. seen = caught up to the 2 old papers.
    mk = _burst_corpus()
    seen = {"O1", "O2"}
    c1, _, r1, pend1 = walk(mk(), seen, max_new=4)
    assert ids(c1) == ["N1", "N2", "N3", "N4"] and r1 == "item_cap" and pend1 is True
    seen |= set(ids(c1))
    # run 2: pending suppresses the leading all-seen stop and drains the overflow
    c2, _, r2, pend2 = walk(mk(), seen, max_new=4, pending=True)
    assert ids(c2) == ["N5", "N6", "N7"] and r2 == "end" and pend2 is False


def test_walk_without_pending_reproduces_the_orphaning_bug():
    # Control: same state as run 2 above but WITHOUT the pending flag → the old
    # behavior wrongly stops at the all-seen page 1, orphaning N5..N7. This is the
    # bug the pending_overflow flag gates.
    mk = _burst_corpus()
    seen = {"O1", "O2", "N1", "N2", "N3", "N4"}
    c, _, r, _ = walk(mk(), seen, max_new=4, pending=False)
    assert c == [] and r == "caught_up"


def test_walk_pending_clears_when_no_overflow():
    # pending set but actually caught up (all-seen LAST page) → stop + clear pending.
    seen = {p["_id"] for p in FIXTURE}
    fetch = fake_fetcher([page(FIXTURE, has_more=False)])
    c, _, r, pend = walk(fetch, seen, pending=True)
    assert c == [] and r == "caught_up" and pend is False


# --- heartbeat ---------------------------------------------------------------

def test_heartbeat_ok_has_success():
    hb = bridge.build_heartbeat("ok", 10, 3, 2, None, "2026-06-15T00:00:00Z")
    assert hb["status"] == "ok" and hb["last_success"] == "2026-06-15T00:00:00Z"


def test_heartbeat_error_has_no_success():
    hb = bridge.build_heartbeat("error", 10, 0, 0, "boom", "2026-06-15T00:00:00Z")
    assert hb["status"] == "error" and hb["last_success"] is None and hb["error"] == "boom"


def test_heartbeat_carries_stop_reason_and_pending():
    hb = bridge.build_heartbeat("ok", 5, 2, 3, None, "t",
                                stop_reason="item_cap", pending_overflow=True)
    assert hb["stop_reason"] == "item_cap" and hb["pending_overflow"] is True


# --- hardening (review fixes) ------------------------------------------------

def test_sidecar_entry_strips_control_chars():
    dirty = dict(FIXTURE[0], abstract="x\x0c\x07" + FIXTURE[0]["abstract"], title="A\x0bB")
    entry = bridge.sidecar_entry(dirty)
    assert "\x0c" not in entry["abstract"] and "\x07" not in entry["abstract"]
    assert entry["title"] == "AB"


def test_build_rss_wellformed_despite_control_chars():
    # XML-illegal control chars in scraped prose must not make the feed un-parseable.
    dirty = dict(FIXTURE[0], _id="ctrl-id", abstract="x\x07" + FIXTURE[0]["abstract"],
                 title="T\x0cT")
    sidecar = {"ctrl-id": bridge.sidecar_entry(dirty)}
    rss = bridge.build_rss(sidecar, max_items=10, build_date_rfc822="x")
    ET.fromstring(rss)  # must not raise


def test_validate_rejects_impossible_date():
    assert "date:bad-format" in bridge.validate_paper(dict(FIXTURE[0], date="2026-02-31"))
    assert "date:bad-format" in bridge.validate_paper(dict(FIXTURE[0], date="2026-13-01"))


def test_load_json_rejects_non_dict(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert bridge._load_json(p) is None


def test_err_trims_to_first_line():
    assert bridge._err(Exception("first line\nbanner1\nbanner2")) == "first line"
