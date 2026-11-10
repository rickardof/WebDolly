"""Unit tests for the Prolific /submissions page parser.

Runs standalone (no browser needed). The parser is pure text processing —
it takes the stripped non-empty innerText lines and returns one dict per
submission row.

Two bugs this file guards against, both silent until the display looks wrong:
  * comma-decimal truncation (Italian locale: "£1,20" came through as £1.00)
  * currency sign confusion when the page mixes GBP/USD in one listing
"""

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "agent"))

# gui_server pulls in Playwright (via pw_bridge) and HTTP server threads at
# import time, but the parser itself is pure text processing. Stub the heavy
# modules so the test runs in CI without the full browser stack installed.
for mod_name in ("playwright", "playwright.sync_api"):
    if mod_name not in sys.modules:
        m = types.ModuleType(mod_name)
        if mod_name == "playwright.sync_api":
            m.sync_playwright = lambda: None  # never called by the parser
        sys.modules[mod_name] = m

import gui_server as gs
from gui_server import _parse_submission_rows, _fmt_money_by_cur


_RESULTS: list[tuple[str, str, str]] = []  # (name, status, detail)


def _t(name: str):
    def _run(fn):
        try:
            fn()
            print(f"PASS  {name}")
            _RESULTS.append((name, "PASS", ""))
        except AssertionError as e:
            print(f"FAIL  {name}  ::  {e}")
            _RESULTS.append((name, "FAIL", str(e)))
        except Exception as e:
            print(f"ERR   {name}  ::  {type(e).__name__}: {e}")
            _RESULTS.append((name, "ERR", f"{type(e).__name__}: {e}"))
        return fn
    return _run


# Minimal fragment of what Prolific renders, with Italian locale decimals
# (comma) and both GBP/USD rows, plus the awkward "Approved by X / Read more"
# interstitial that only appears on recently approved rows.
ITALIAN_LOCALE_ROWS = [
    "Submission History",
    "Study name", "Date", "Reward", "Status", "Actions",

    "A Scenario-Based Experiment on the Effect of Performance",
    "By ckgsb.edu.cn",
    "22 Oct 2026, 09:30",
    "Approved by 30 Oct",
    "Read more",
    "Reward: £1,20",
    "APPROVED",

    "Daily Diary — Day 9 Evening Survey (Main)",
    "By Nabila Khan",
    "22 Oct 2026, 19:12",
    "Reward: £0,06",
    "AWAITING REVIEW",

    "AI Workforce Decisions — Employees in AI-Affected Roles",
    "By Tim Bono",
    "21 Oct 2026, 08:05",
    "Reward: $2,50",
    "APPROVED",

    "Le tue storie di viaggio potrebbero dare forma alla prossima esperienza",
    "By travel-research",
    "22 Oct 2026, 11:00",
    "Screened out reward: £0,10",
    "SCREENED OUT",

    "Legacy dot-decimal study",
    "By legacy-researcher",
    "20 Oct 2026, 10:00",
    "Reward: £1.50",
    "APPROVED",
]


@_t("parse:comma_decimal_pence")
def _():
    parsed = _parse_submission_rows(ITALIAN_LOCALE_ROWS)
    diary = [p for p in parsed if "Daily Diary" in p["title"]][0]
    assert diary["currency"] == "£", f"currency={diary['currency']!r}"
    assert diary["reward"] == "0.06", f"reward={diary['reward']!r} (expected 0.06, old regex gave 0 or '')"


@_t("parse:comma_decimal_pound")
def _():
    parsed = _parse_submission_rows(ITALIAN_LOCALE_ROWS)
    scen = [p for p in parsed if "Scenario-Based" in p["title"]][0]
    assert scen["currency"] == "£"
    assert scen["reward"] == "1.20", f"reward={scen['reward']!r} (expected 1.20, old regex gave 1)"


@_t("parse:comma_decimal_dollar")
def _():
    parsed = _parse_submission_rows(ITALIAN_LOCALE_ROWS)
    ai = [p for p in parsed if "AI Workforce" in p["title"]][0]
    assert ai["currency"] == "$"
    assert ai["reward"] == "2.50", f"reward={ai['reward']!r}"


@_t("parse:screened_out_reward_still_captured")
def _():
    parsed = _parse_submission_rows(ITALIAN_LOCALE_ROWS)
    so = [p for p in parsed if "storie di viaggio" in p["title"]][0]
    assert so["status"].lower() == "screened out"
    assert so["currency"] == "£"
    assert so["reward"] == "0.10", f"reward={so['reward']!r}"


@_t("parse:dot_decimal_still_works")
def _():
    parsed = _parse_submission_rows(ITALIAN_LOCALE_ROWS)
    legacy = [p for p in parsed if "Legacy dot-decimal" in p["title"]][0]
    assert legacy["reward"] == "1.50", f"reward={legacy['reward']!r}"


@_t("parse:date_captured")
def _():
    parsed = _parse_submission_rows(ITALIAN_LOCALE_ROWS)
    scen = [p for p in parsed if "Scenario-Based" in p["title"]][0]
    assert scen["date"] == "22 Oct 2026", f"date={scen['date']!r}"
    assert scen["date_full"] == "22 Oct 2026, 09:30", f"date_full={scen['date_full']!r}"


@_t("parse:all_five_rows_found")
def _():
    parsed = _parse_submission_rows(ITALIAN_LOCALE_ROWS)
    titles = {p["title"] for p in parsed}
    assert len(parsed) == 5, f"expected 5 rows, got {len(parsed)}: {titles}"


@_t("parse:title_is_not_the_amount_line")
def _():
    # Regression: when the amount regex fails, the parser used to fall through
    # and treat the "£0,06" fragment as the title. All titles must look like
    # titles, not currency strings.
    parsed = _parse_submission_rows(ITALIAN_LOCALE_ROWS)
    for p in parsed:
        assert not p["title"].startswith(("£", "$", "€")), \
            f"title stole the amount line: {p['title']!r}"


@_t("parse:integer_amount_no_decimal")
def _():
    rows = [
        "Flat Rate Study", "By someone", "15 Oct 2026, 10:00",
        "Reward: £5", "APPROVED",
    ]
    parsed = _parse_submission_rows(rows)
    assert len(parsed) == 1
    assert parsed[0]["reward"] == "5"
    assert parsed[0]["currency"] == "£"


@_t("fmt:single_currency")
def _():
    assert _fmt_money_by_cur({"£": 1.50}) == "£1.50"


@_t("fmt:mixed_currencies_joined")
def _():
    # The whole point of this fix: never pretend $ + £ equals one scalar.
    out = _fmt_money_by_cur({"£": 1.50, "$": 2.30})
    assert "£1.50" in out and "$2.30" in out and " + " in out, out


@_t("fmt:zero_fallback")
def _():
    assert _fmt_money_by_cur({}) == "£0.00"
    assert _fmt_money_by_cur({"£": 0.0}) == "£0.00"


@_t("fmt:zero_currencies_skipped")
def _():
    # A currency bucket at 0 should not clutter the string with "£0.00 + $5.00".
    out = _fmt_money_by_cur({"£": 0.0, "$": 5.00})
    assert out == "$5.00", f"got {out!r}"


# ===================================================== the SUMMARY block and
# the pager. Reading only the first of 41 pages made /earnings report
# £0.70 + $27.39 over 8 studies while this block said £590.94 + $939.95 over
# 559. The shape below is the live page's, with invented studies and figures.
PAGE = """Skip to main content
Studies
Submissions
About you
Messages
Desktop
Refer a Friend
Balance
£86,06
Pending
£7,74
RF
Submissions
SUMMARY
Total submissions
816
Submissions approved
559
Total earned
£590,94
$939,95
SUBMISSION HISTORY
Date
Status
Date: Newest to oldest
Study name
Date
Reward
Status
Actions
A study about nothing
By Some Researcher
9 Nov 2026, 17:59
Approved by 10 Nov
Read more
Reward: $3,80
APPROVED
Another study
By Another Researcher
9 Nov 2026, 17:44
Reward: £1,20
AWAITING REVIEW
A third study
By A Third Researcher
8 Nov 2026, 09:05
Screened out reward: £0,10
SCREENED OUT
Prev
1
2
3
4
5
…
41
Next""".split("\n")


@_t("header:summary_block")
def _():
    h = gs._parse_submission_header(PAGE)
    assert h["total"] == 816, h
    assert h["approved"] == 559, h
    assert h["earned"] == {"£": 590.94, "$": 939.95}, h


@_t("header:wallet_lines")
def _():
    h = gs._parse_submission_header(PAGE)
    assert h["balance"] == {"£": 86.06}, h
    assert h["pending"] == {"£": 7.74}, h


@_t("header:absent_is_empty_not_wrong")
def _():
    # A page that did not render (or a layout change) must report nothing, so
    # callers keep the figures they already had.
    assert gs._parse_submission_header(["Skip to main content", "Submissions"]) == {}


@_t("header:italian_locale_amounts")
def _():
    # Chromium runs --lang=it-IT: comma decimals, dot thousands.
    assert gs._money("£590,94") == ("£", 590.94)
    assert gs._money("£1.234,56") == ("£", 1234.56)
    assert gs._money("$9.80") == ("$", 9.80)
    assert gs._money("£0,06") == ("£", 0.06)
    assert gs._money("Reward: £3,00") is None     # only bare amount lines
    assert gs._money("816") is None


@_t("pager:last_page")
def _():
    assert gs._parse_last_page(PAGE) == 41


@_t("pager:no_pager_means_one_page")
def _():
    rows = [r for r in PAGE if r not in ("Prev", "Next") and not r.isdigit()]
    assert gs._parse_last_page(rows) == 1


@_t("pager:a_numeric_study_title_is_not_a_page")
def _():
    # "2026" as a study name sits far from the pager; only the tail counts.
    rows = ["2026"] + PAGE[:-9] + ["Prev", "1", "2", "3", "Next"]
    assert gs._parse_last_page(rows) == 3


@_t("page:rows_and_header_from_one_read")
def _():
    rows = _parse_submission_rows(PAGE)
    assert len(rows) == 3, rows
    assert [r["status"] for r in rows] == ["Approved", "Awaiting Review", "Screened Out"]
    assert rows[0]["reward"] == "3.80" and rows[0]["currency"] == "$"


# ============================================================== the local store
def _row(title, date_full, status="Approved", reward="1.00", cur="£"):
    return {"title": title, "status": status, "date": date_full.split(",")[0],
            "date_full": date_full, "reward": reward, "currency": cur}


@_t("store:merge_adds_new_rows")
def _():
    st = {"rows": {}, "header": {}}
    assert gs._subs_merge(st, [_row("A", "9 Nov 2026, 10:00"),
                               _row("B", "8 Nov 2026, 10:00")]) == 2
    assert len(st["rows"]) == 2


@_t("store:a_row_read_twice_is_not_two_studies")
def _():
    # Pages shift as new submissions arrive, so the same row is re-read often.
    st = {"rows": {}, "header": {}}
    gs._subs_merge(st, [_row("A", "9 Nov 2026, 10:00")])
    assert gs._subs_merge(st, [_row("A", "9 Nov 2026, 10:00")]) == 0
    assert len(st["rows"]) == 1


@_t("store:status_flip_updates_in_place")
def _():
    # Awaiting review becomes approved weeks later: the row must change, not
    # be counted a second time.
    st = {"rows": {}, "header": {}}
    gs._subs_merge(st, [_row("A", "9 Nov 2026, 10:00", status="Awaiting Review")])
    assert gs._subs_merge(st, [_row("A", "9 Nov 2026, 10:00", status="Approved")]) == 1
    assert len(st["rows"]) == 1
    assert next(iter(st["rows"].values()))["status"] == "Approved"


@_t("store:same_title_different_day_is_a_second_study")
def _():
    st = {"rows": {}, "header": {}}
    gs._subs_merge(st, [_row("A", "9 Nov 2026, 10:00"), _row("A", "8 Nov 2026, 10:00")])
    assert len(st["rows"]) == 2


@_t("store:newest_first")
def _():
    st = {"rows": {}, "header": {}}
    gs._subs_merge(st, [_row("old", "1 Nov 2026, 10:00"), _row("new", "9 Nov 2026, 10:00"),
                        _row("mid", "5 Nov 2026, 10:00")])
    assert [r["title"] for r in gs._subs_rows(st)] == ["new", "mid", "old"]


@_t("store:prolific_summary_is_the_checksum")
def _():
    st = {"rows": {}, "header": {"total": 3, "approved": 2}}
    gs._subs_merge(st, [_row("A", "9 Nov 2026, 10:00"),
                        _row("B", "8 Nov 2026, 10:00", status="Returned")])
    assert not gs._subs_complete(st), "2 of 3 rows is not the whole history"
    gs._subs_merge(st, [_row("C", "7 Nov 2026, 10:00")])
    assert gs._subs_complete(st), "3 rows, 2 approved: matches the SUMMARY"


@_t("store:no_header_means_never_complete")
def _():
    # Without Prolific's own figures there is nothing to check against, so the
    # crawl must not conclude it is done.
    st = {"rows": {"k": _row("A", "9 Nov 2026, 10:00")}, "header": {}}
    assert not gs._subs_complete(st)


@_t("store:approved_shortfall_keeps_the_crawl_going")
def _():
    # Right row count, but a status flipped deep in the history.
    st = {"rows": {}, "header": {"total": 2, "approved": 2}}
    gs._subs_merge(st, [_row("A", "9 Nov 2026, 10:00"),
                        _row("B", "8 Nov 2026, 10:00", status="Awaiting Review")])
    assert not gs._subs_complete(st)


@_t("store:survives_a_round_trip_to_disk")
def _():
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        orig = gs.SUBS_STORE
        try:
            gs.SUBS_STORE = pathlib.Path(d) / "submissions.json"
            st = gs._subs_store_load()
            assert st["rows"] == {} and st["cursor"] == 2
            gs._subs_merge(st, [_row("A", "9 Nov 2026, 10:00")])
            st["header"] = {"total": 1, "approved": 1}
            gs._subs_store_save(st)
            back = gs._subs_store_load()
            assert len(back["rows"]) == 1, back
            assert back["header"]["total"] == 1
            assert gs._subs_complete(back)
        finally:
            gs.SUBS_STORE = orig


if __name__ == "__main__":
    passed = sum(1 for _, s, _ in _RESULTS if s == "PASS")
    failed = sum(1 for _, s, _ in _RESULTS if s != "PASS")
    print(f"\n== SUMMARY ==  {passed}/{len(_RESULTS)} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
