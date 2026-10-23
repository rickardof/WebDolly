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


if __name__ == "__main__":
    passed = sum(1 for _, s, _ in _RESULTS if s == "PASS")
    failed = sum(1 for _, s, _ in _RESULTS if s != "PASS")
    print(f"\n== SUMMARY ==  {passed}/{len(_RESULTS)} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
