"""Prolific's submission history (app.prolific.com/submissions), as text.

The GUI server's crawler (gui_server._submissions_refresh) reads the history a
page at a time between cycles and keeps it in a local store; what a page of
that history looks like -- its rows, its status badges, its SUMMARY block, its
pager -- is Prolific's own, and lives here. Moved verbatim from gui_server.
"""
import re as _re

# The list is rendered client-side, so the crawler waits for it to be there
# instead of for a fixed five seconds: at several pages a refresh the fixed
# wait IS the cost of the crawl.
_SUBMISSIONS_READY_JS = ("() => /SUBMISSION HISTORY/i.test(document.body.innerText) && "
                         "/(APPROVED|AWAITING REVIEW|RETURNED|REJECTED|TIMED OUT|ACTIVE|SCREENED OUT)/"
                         ".test(document.body.innerText)")

_STATUS_RE   = _re.compile(
    r'^(AWAITING REVIEW|APPROVED|RETURNED|REJECTED|TIMED OUT|ACTIVE|SCREENED OUT)$',
    _re.IGNORECASE)


_MONEY_RE    = _re.compile(r'^(screened out )?reward', _re.IGNORECASE)


# The label of an extra payment on a submission, whose amount is on the next
# line with no prefix of its own.
_EXTRA_PAY_RE = _re.compile(r'^(bonus|adjustment|tip)s?\s*:?\s*$', _re.IGNORECASE)


# Chromium runs with --lang=it-IT, so Prolific renders rewards in Italian
# locale: "Reward: £0,06" / "Reward: £1,20". The old regex only accepted
# "." and silently truncated the fractional part, showing £1,20 as £1.00
# and £0,06 as £0.00. Accept both separators and normalise below.
_REWARD_AMT  = _re.compile(r'([£$€])\s*(\d+(?:[.,]\d+)?)')


_BY_RE       = _re.compile(r'^by\s+.+', _re.IGNORECASE)


_DATELINE_RE = _re.compile(r'^\d{1,2}\s+\w{3}\s+\d{4}', _re.IGNORECASE)


_SKIP_LINE   = _re.compile(
    r'^(approved by|read more|submissions?|summary|total|filters?|sort|search'
    r'|balance|pending|about you|messages|studies|refer|projects?'
    r'|date|status|reward|actions?|study name|\d+\s*submissions?'
    r'|skip to main|time taken).*$', _re.IGNORECASE)


def _parse_submission_rows(raw_rows: list[str]) -> list[dict]:
    """Parse the Prolific /submissions page innerText (as a list of stripped
    non-empty lines) into one dict per row.

    Pulled out of _fetch_submissions so it can be unit-tested without a browser:
    the regex layer has had subtle bugs twice (comma-decimal truncation, title
    stealing the reward line), and locale/currency changes will keep coming.
    """
    parsed, i = [], 0
    while i < len(raw_rows):
        line = raw_rows[i]
        if _STATUS_RE.match(line):
            status = line.title()
            title, date_full, currency, paid = "", "", "£", 0.0
            # A row reads, in page order: title, "By <researcher>", date,
            # ["Approved by <date>"], ["Read more"], "Reward: <amount>",
            # optionally "Bonus:"/"Adjustment:" and a bare amount, then the
            # status. So walk back from the status until the title.
            for j in range(i - 1, max(i - 12, -1), -1):
                prev = raw_rows[j]
                if _DATELINE_RE.match(prev):
                    date_full = prev.strip()
                elif _MONEY_RE.match(prev):
                    m = _REWARD_AMT.search(prev)
                    if m:
                        currency = m.group(1)
                        paid += float(m.group(2).replace(",", "."))
                elif _AMOUNT_RE.match(prev):
                    # A bare amount: the value of the "Bonus:"/"Adjustment:"
                    # line just above it. It belongs to this submission, so it
                    # is added to what the study paid. This line used to fall
                    # through to the title branch, which took "£0,51" for the
                    # study name and stopped -- inventing a dateless,
                    # rewardless phantom submission and losing the real one
                    # (9 of the first 399 rows read).
                    m = _money(prev)
                    if m:
                        currency = m[0]
                        paid += m[1]
                elif (_EXTRA_PAY_RE.match(prev) or _BY_RE.match(prev)
                        or _SKIP_LINE.match(prev)):
                    continue
                elif not title and len(prev) > 4:
                    title = prev[:80]
                    break
            if title:
                parsed.append({
                    "title": title, "status": status,
                    "date": date_full.split(",")[0].strip(),
                    "date_full": date_full,
                    "reward": f"{paid:.2f}" if paid else "", "currency": currency,
                })
        i += 1
    return parsed


# Prolific's own SUMMARY block, at the top of every /submissions page. These
# are the account's authoritative lifetime figures: summing the rows we
# managed to read instead reported GBP 0,70 + USD 27,39 over 8 studies against
# the GBP 590,94 + USD 939,95 over 559 the page itself was showing, because
# only the first of 41 pages was ever read.
_AMOUNT_RE = _re.compile(r'^([£$€])\s*([\d.,]+)$')


_INT_RE = _re.compile(r'^\d[\d.,]*$')


def _money(line: str) -> tuple[str, float] | None:
    """("£", 590.94) from a bare amount line, or None.

    Chromium runs with --lang=it-IT, so Prolific renders "£590,94": the comma
    is the decimal separator and the dot groups thousands ("£1.234,56").
    """
    m = _AMOUNT_RE.match(line.strip())
    if not m:
        return None
    raw = m.group(2)
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return m.group(1), float(raw)
    except ValueError:
        return None


def _parse_submission_header(rows: list[str]) -> dict:
    """The SUMMARY block and the wallet lines, or {} if the page did not show
    them. Earnings are per currency: Prolific pays some studies in GBP and
    others in USD and never adds them up itself."""
    def _after(label: str, within: int = 3):
        for i, r in enumerate(rows):
            if r.strip().lower() == label:
                return rows[i + 1:i + 1 + within]
        return []

    out: dict = {}
    for key, label in (("balance", "balance"), ("pending", "pending")):
        for line in _after(label, 1):
            m = _money(line)
            if m:
                out[key] = {m[0]: m[1]}
    for key, label in (("total", "total submissions"), ("approved", "submissions approved")):
        for line in _after(label, 1):
            if _INT_RE.match(line.strip()):
                out[key] = int(line.strip().replace(".", "").replace(",", ""))
    earned: dict = {}
    # "Total earned" is followed by one line per currency, so read until the
    # next line that is not an amount instead of assuming how many there are.
    for line in _after("total earned", 6):
        m = _money(line)
        if not m:
            break
        earned[m[0]] = earned.get(m[0], 0.0) + m[1]
    if earned:
        out["earned"] = earned
    return out


def _parse_last_page(rows: list[str]) -> int:
    """The highest page number in the pager at the foot of the list, or 1.

    The pager reads "Prev 1 2 3 4 5 ... 41 Next", so the last numbered button
    is the page count. Only the tail of the page is considered: a study title
    that happens to be a number must not be mistaken for a page.
    """
    tail = rows[-16:]
    if not any(r.strip().lower() == "next" for r in tail):
        return 1
    nums = [int(r) for r in tail if r.strip().isdigit()]
    return max(nums) if nums else 1


# Prolific lists 20 submissions a page and pages with ?page=N (verified
# against the live account: pages 1-3 return disjoint sets of 20 rows and the
# pager reports 41 pages for 816 submissions).
_SUBMISSIONS_URL = "https://app.prolific.com/submissions"


_SUBS_PER_PAGE = 20


# What Prolific counts in "Submissions approved" and "Total earned": not only
# the approved rows but the screened-out ones too, which pay a screening fee.
# Measured against the live account: 523 approved + 35 screened out = 558
# rows worth $933.95 + £580.14, against a SUMMARY of 559 rows worth
# $939.95 + £590.94 (the gap is the 9 rows still missing from the store).
# Counting only "approved" made the crawl's checksum unsatisfiable and left
# every period short of the account's own headline.
_SUBS_PAID = ("approved", "screened out")


# @@ SUBMISSIONS-DEFINITIONS @@
