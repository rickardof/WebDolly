"""TaskPlatform: the loop's rules for a site that hands out timed tasks.

A task platform lists units of work, launches one (often on another host),
times it, and takes it back with a completion code. Everything the agent
loop does because of that lives here; the site profile supplies the URLs,
page patterns and texts (`sites.current`), this class supplies what to do
with them:

  * the launch surface (list, task page, account pages) and the moment a
    task starts, which starts the task timer;
  * the guards: forbidden sections, lingering off the list, the marketing
    site, a completion code in the URL;
  * tasks that lead nowhere (and, opt-in, audio/video ones) are skipped and
    remembered;
  * completion codes seen on the task's pages, the platform's submit/cancel
    confirmations, the researcher's quality-check verdict;
  * the minimum time: a submit is held until it is up, the model is told
    how long is left, the speed is raised when the task runs long;
  * the reserved-slot guard and the deterministic launcher;
  * the terminate guards: no NOCODE before the task was even opened, the
    "I didn't receive a code" control pressed instead of argued about, the
    survey's own return control clicked, a free-text "done" pushed back once;
  * the end of a task: the code check, notifications, study_memory.

Run options it reads (keyword arguments of run_agent):

  wander_seconds  how long the agent may linger on the platform's pages off
                  the task list before it is taken back to it (0 = never);
  skip_av         abandon a study whose page needs audio, video, a webcam or
                  a microphone, instead of relying on the task text to tell
                  the model to avoid them. Either way, a study ruled out is
                  remembered in study_cache so later cycles do not open it.

The module has two parts: first the page probes and decisions the rules use
(the timer, dead-end and audio/video detection, completion codes, the
terminate guards, the reserved-slot launcher...), then the TaskPlatform
class that calls them from the loop's hooks. What is generic -- browser
input, page text, the transcript, notifications, speed modes -- stays in
agent_core and is reached as `core.X`, looked up at call time.
"""

import random
import re
import time
from urllib.parse import parse_qs, urlparse

import agent_core as core
import cancel
import sites
import study_cache

from . import Behavior

# ===================================================================
# What the rules below read off the page and decide. Browser input, page
# text and the loop's own state come from agent_core (`core.X`).
# ===================================================================


def _learn_app_host(url: str) -> None:
    """Adopt the start URL's host as the platform's app host, so a different
    deployment of the same platform needs no code change."""
    host = (urlparse(url or "").hostname or "").lower()
    if host and core._on_guarded_site(url):
        sites.current.app_host = host


def _reached_in_launch_tab(url, cur_page_id, launch_page_id) -> bool:
    """True when `url` is app-host chrome we have NOT catalogued, reached by
    plain navigation in the very tab that was showing the launch surface.

    _on_launch_surface answers "unknown path => a task is running" on
    purpose (it stops the wander guard cutting a platform-hosted task short
    at a path nobody catalogued). That default keeps producing false
    positives -- an error page, an account section -- so pair it with the
    invariant that actually separates the two cases: a platform-hosted task
    is opened in its own window, so it never shares a page object with the
    list it was launched from. Same-tab navigation off the launch surface
    into an unknown app path is account chrome, not a task. A task that did
    navigate in place is still caught by the profile's hosted_task_path."""
    if not core._on_guarded_site(url):
        return False
    if sites.current.hosted_task_path.match(urlparse(url).path or "/"):
        return False
    return (launch_page_id is not None and cur_page_id is not None
            and cur_page_id == launch_page_id)


def _on_launch_surface(url: str) -> bool:
    """True when `url` is a platform page where no task is running yet: the
    task list, a task's detail/reservation page, platform chrome (account,
    history, settings) or one of its error pages.

    False for third-party hosts AND for platform-hosted task tools -- those
    ARE a running task. "Inside a task" is defined as the negation of this
    (off the launch surface), so a task the platform hosts itself gets the
    same timer, abandonment accounting and in-task framing as one on another
    host. Keyed on the surface, not on "left the platform's domain", so the
    model does not mistake a running first-party task for the detail page
    and cancel a task it is actually completing."""
    if not core._on_guarded_site(url):
        return False
    site = sites.current
    if site.is_signin(url):
        return True   # where the session ends up when it expires, not a task
    path = urlparse(url).path or "/"
    if site.home_path.match(path) or site.task_path.match(path):
        return True
    norm = path.rstrip("/") or "/"
    if norm == "/":
        return True
    if site.error_path.match(norm):
        return True
    return any(norm == p or norm.startswith(p + "/")
               for p in site.launch_chrome_prefixes)


_PAGE_CODE_CACHE: dict = {}


def _page_code_re():
    """The regex that finds a completion code printed on a page ("Your
    completion code is: X"), with the active profile's own words added to the
    keywords ("Your <platform> code is: X")."""
    extra = tuple(sites.current.code_keywords)
    rx = _PAGE_CODE_CACHE.get(extra)
    if rx is None:
        alt = "".join(re.escape(w) + "|" for w in extra)
        filler = (r"(?:is|below|here|in|on|into|to|" + alt
                  + r"please|enter|paste|copy|the|your|following|this)")
        kw = r"(?:completion|confirmation|survey|" + alt + r"study)"
        rx = re.compile(
            kw + r"\s*(?:code|id)\b"
            r"(?:[\s:,.\-]+" + filler + r"\b)*[\s:,.\-]*"
            # the code itself can't be the next keyword ("<platform> code /
            # completion code: X" must yield X, not swallow "completion")
            r"(?!" + kw + r"\b)"
            r"([A-Za-z0-9][A-Za-z0-9_\-]{3,40})", re.IGNORECASE)
        _PAGE_CODE_CACHE[extra] = rx
    return rx


_NOT_A_CODE = {"will", "and", "when", "after", "must", "should", "has", "have"}


def codes_on_page(text: str) -> list:
    """Completion codes printed on a custom end page ("Your completion code
    is: C1A2B3"). Many tasks end on their own page instead of redirecting
    back to the platform, so the code lives only on screen. Used to
    double-check what the model read, never to replace it."""
    out = []
    for m in _page_code_re().finditer(text or ""):
        c = m.group(1)
        if c.lower() in _NOT_A_CODE or (c.isalpha() and c.islower()) or c in out:
            continue          # an ordinary word, or already seen
        out.append(c)
    return out


_CODE_PARAM = re.compile(r"(^|[_\-])(cc|code|completion[_\-]?code|completioncode|pcode)$", re.IGNORECASE)


def codes_in_url(url: str) -> list:
    """Codes carried in an external page's URL (?code=PLKXLQ). Seen on 13 Oct:
    a survey's thank-you page had the code only in its own address, and the
    model typed it into the platform from there."""
    try:
        out = []
        for k, vals in parse_qs(urlparse(url or "").query).items():
            if _CODE_PARAM.search(k):
                for v in vals:
                    v = v.strip()
                    if re.fullmatch(r"[A-Za-z0-9_\-]{3,40}", v) and v not in out:
                        out.append(v)
        return out
    except Exception:
        return []


def check_code(answer: str, seen: list) -> str:
    """Compare the model's final answer with the codes seen during the study.

    "match"     the code it reported was seen on a page
    "mismatch"  codes were seen, the one it reported is not among them
    "missed"    it reported NOCODE but a code was on screen: likely a lost payment
    "not_found" nothing recognisable was seen -- no opinion
    """
    if not seen:
        return "not_found"
    if answer == "NOCODE":
        return "missed"
    code = answer.split(":", 1)[1].strip() if answer.startswith("CODE:") else ""
    return "match" if code in seen else "mismatch"


def _orphan_reservation_visible() -> bool:
    try:
        return bool(sites.current.held_slot_banner_re.search(core._page_text(4000) or ""))
    except Exception:
        return False


def _reservation_nudge(fresh_unlaunched: bool, detail_id: str) -> str:
    """One-shot guidance for a model stuck on a held task's detail page,
    branched on whether the task was ever launched (see the profile)."""
    return sites.current.reservation_nudge(fresh_unlaunched, detail_id)


def _launch_selectors() -> tuple:
    """The profile's launch controls, most-advanced state first: an open
    start dialog, the control that opens it, the control that reserves."""
    s = sites.current
    return tuple(x for x in (s.launch_dialog_sel, s.launch_open_sel, s.launch_reserve_sel) if x)


def _launch_control_visible() -> bool:
    """Is a way INTO the task still on the page? See
    pw_bridge.launch_control_present. Safe-false so a bridge hiccup never
    turns into a cancellation."""
    sels = _launch_selectors()
    if not sels:
        return False
    try:
        return bool(core.pw_bridge.launch_control_present(sels))
    except Exception:
        return False


def _humanized_launch_reserved_study(include_take_part: bool = True) -> dict:
    """Launch the task the model has opened on the platform, with humanised
    xdotool clicks grounded on the profile's launch selectors: (optionally)
    the reserve control -> the control that opens the launch dialog -> tick
    the dialog checkbox(es) -> its start control, then follow the task tab it
    opens. The model chose the task; this only performs the mechanical launch
    it keeps fumbling (the confirm link sits below the dialog fold).

    Returns {'status': ...}. Never raises (except on cancellation). status:
      launched           -- task tab is open and now the active/front page
      unsupported        -- the active profile has no launch flow
      no-reserve-control -- un-reserved but no reserve control found
      no-launch-control  -- reserved but no control that opens the dialog
      no-dialog          -- that control clicked but the dialog never appeared
      no-start-control   -- dialog open but no start control
      clicked-no-tab     -- start clicked but no task tab appeared
      error              -- unexpected failure (detail included)

    Every control is wheel-scrolled into view before it is clicked
    (_box_into_view); the dialog is taller than the kiosk viewport, so the
    confirm link is usually below the fold. When even scrolling cannot reveal
    it (the page behind is already at its scroll end, as in
    run_20261106_124620), the link is focused and activated with a real Enter.
    """
    site = sites.current
    if not site.launch_start_sel:
        return {"status": "unsupported"}
    try:
        core.pw_bridge.front()   # pin the study tab to the foreground for xdotool
        # 1. Reserve, if still on the un-reserved detail page.
        if (include_take_part
                and not core.pw_bridge.has_selector(site.launch_open_sel)
                and not core.pw_bridge.has_selector(site.launch_dialog_sel)):
            rbox = core._box_into_view(site.launch_reserve_sel)
            if rbox is None or not core._humanized_click_box(rbox):
                return {"status": "no-reserve-control"}
            core.pw_bridge.wait_selector(site.launch_open_sel, 9000)
        # 2. Open the 'Start study' dialog.
        if not core.pw_bridge.has_selector(site.launch_dialog_sel):
            obox = core._box_into_view(site.launch_open_sel)
            if obox is None or not core._humanized_click_box(obox):
                return {"status": "no-launch-control"}
            if not core.pw_bridge.wait_selector(site.launch_dialog_sel, 6000):
                return {"status": "no-dialog"}
        # 3. Tick every unchecked checkbox (device + any confidentiality one);
        #    the 'Start study' link stays disabled until they are ticked. Taken
        #    one at a time and re-queried, because ticking one can reflow the
        #    dialog and invalidate the boxes measured for the others.
        def _first_unchecked():
            boxes = core.pw_bridge.unchecked_checkbox_boxes(
                site.launch_dialog_sel, scroll=False)
            return boxes[0] if boxes else None

        for _ in range(6):
            if _first_unchecked() is None:
                break
            cbox = core._scroll_box_into_view(_first_unchecked)
            if cbox is None or not core._humanized_click_box(cbox, pre=(0.2, 0.5)):
                break
            cancel.sleep(random.uniform(0.15, 0.4))
        # 4. Activate 'Start study' and follow the survey tab it opens.
        before = core.pw_bridge.page_ids()
        sbox = core._box_into_view(site.launch_start_sel)
        clicked = bool(sbox) and core._humanized_click_box(sbox)
        if not clicked:
            # Nothing scrolls and the confirm link stays off-screen. Focus it
            # in the DOM and press Enter with a REAL xdotool keystroke: the
            # activation event is genuine, only the focus is programmatic.
            if not core.pw_bridge.focus_element(site.launch_start_sel):
                return {"status": "no-start-control"}
            cancel.sleep(random.uniform(0.2, 0.5))
            core.docker_exec("xdotool", "key", "--clearmodifiers", "Return")
        surl = core.pw_bridge.follow_new_tab(before, 9000)
        if surl:
            return {"status": "launched", "survey_url": surl}
        return {"status": "clicked-no-tab"}
    except cancel.Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "detail": repr(e)}


# A researcher's own words when their automatic checks reject the work that was
# just submitted. These pages are the only place the verdict ever appears: the
# study still returns a completion code, the platform still shows "awaiting review",
# so without this the run looks like a clean success on every surface and the
# failure is found weeks later in a rejection -- or never.
#
# Every alternative demands a word of failure ("failed", "did not pass",
# "flagged"), never the bare phrase: a study that WARNS about attention checks
# in its instructions, or announces "this is an attention check", must not be
# read as one that failed them.
_QC_FAIL_RE = re.compile(
    r"submission has been flagged"
    r"|failed to correctly choose"
    r"|(?:failed|did not pass|didn't pass|missed)[^.!?]{0,60}?"
    r"(?:attention|quality|validation|comprehension|screening)[^.!?]{0,20}?check"
    r"|(?:attention|quality|comprehension|validation)[^.!?]{0,20}?check[^.!?]{0,60}?"
    r"(?:failed|was not passed|not passed|unsuccessful)"
    r"|you (?:have )?failed [^.!?]{0,40}?check",
    re.I)


_QC_GENERIC_RE = re.compile(r"submission has been flagged", re.I)


def qc_failure_in(text: str) -> str:
    """The sentence a page uses to say the submission failed the researcher's
    automatic checks, or "" when it says no such thing.

    Returned with its surroundings (a window, not the match alone) so the
    summary carries the researcher's wording: "ground_truth: Failed to
    correctly choose obvious test cases" says what to fix, "flagged" does not.
    """
    hits = list(_QC_FAIL_RE.finditer(text or ""))
    if not hits:
        return ""
    # "Your submission has been flagged" comes first on the page and says the
    # least; the line that names the check that failed usually comes after it,
    # under the results. Prefer the specific one, whatever its position.
    m = next((h for h in hits if not _QC_GENERIC_RE.fullmatch(h.group(0))), hits[0])
    text = text or ""
    start = max(0, m.start() - 70)
    before = text[start:m.start()]
    cut = max(before.rfind(". "), before.rfind("! "), before.rfind("? "))
    if cut >= 0:                       # start the snippet at a sentence
        start += cut + 2
    elif start > 0 and " " in before:  # ...or at least not mid-word
        start += before.index(" ") + 1
    return core._WS_RE.sub(" ", text[start:m.end() + 110]).strip()[:220]


def _study_title() -> str:
    """Name of the study on the page being looked at, or "".

    Only used to tell the model which studies to skip: the list page shows
    names, never ids, so a cached entry without a title is useless to it.
    Returns "" on any non-study page so junk headings (the sidebar account
    name was ending up cached as the study title) can't poison the cache.
    The script that reads it is the profile's task_title_js.
    """
    js = sites.current.task_title_js
    if not js:
        return ""
    try:
        title = (core.pw_bridge.run(lambda b: b.active_page().evaluate(js)) or "").strip()[:100]
    except Exception:
        return ""
    # Belt-and-braces: common non-title strings that have slipped into the
    # cache before. Add anything else you see here as a false positive.
    low = title.lower()
    if not title or low in sites.current.task_title_junk:
        return ""
    return title


# A dead-end marker only counts in a sentence that states something that has
# already happened. The same words in the researcher's description are a
# hypothesis about what might happen later ("if you do not meet the
# requirements...", "you may be screened out"), and the description is part
# of the same body text as any banner, so tense is the only thing that tells
# them apart.
_HYPOTHETICAL_RE = re.compile(
    r"\b(?:if|unless|in case|in the event|provided that|should you|whether"
    r"|may|might|could|would|will|shall|going to|about to|once you|when you"
    r"|qualora|potresti|potrebbe)\b|'ll\b|\bse\b",
    re.IGNORECASE,
)


# Studies that need headphones, a camera or a mic: opt-in, because skipping
# them is a choice about what work to accept, not a safety rule.
_AV_TEXT_MARKERS = (
    (r"\bheadphones?\b|\bearphones?\b|\bcuffie\b|\bauricolari\b", "cuffie richieste"),
    (r"listen to (?:the |this |an? )?(?:audio|recording|clip|sound|speech)"
     r"|ascolt(?:a|erai) (?:l'|un |il )?(?:audio|registrazione)", "audio da ascoltare"),
    (r"audio (?:clip|recording|stimuli|task)", "stimoli audio"),
)


# The sentence the last _detect_dead_end match came from. Logged with the
# skip: for 17 of the 21 entries the old cache held, the page text that
# condemned the study was gone by the time anyone asked.
_last_dead_end_sentence = ""


def _detect_dead_end(text: str) -> str:
    """The reason this study leads nowhere, read off the page, or "".

    Sentence by sentence, so a marker is judged together with the words
    around it: a conditional in the same sentence means the page is
    describing what the study does, not reporting what happened to this
    account.
    """
    global _last_dead_end_sentence
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        for pattern, reason in sites.current.dead_end_markers:
            if re.search(pattern, sentence, re.IGNORECASE):
                if _HYPOTHETICAL_RE.search(sentence):
                    break      # the description, not a status
                _last_dead_end_sentence = sentence.strip()[:200]
                return reason
    _last_dead_end_sentence = ""
    return ""


def _detect_av(text: str = "", check_text: bool = True) -> str:
    """Audio/video requirement on the current page, as a short reason, or "".

    Two signals. A real media element or an embedded player is checked on
    any page, because it cannot be mistaken for prose. The wording ("you
    will watch a video", "headphones required") is only read on the study
    description, where `check_text` is passed True by the caller.
    """
    _JS = r"""() => {
        const out = [];
        const audio = [...document.querySelectorAll('audio')]
            .filter(el => el.offsetParent !== null || el.autoplay || el.src || el.currentSrc);
        if (audio.length) out.push('tag audio');
        const players = /soundcloud/i;
        if ([...document.querySelectorAll('iframe')].some(f => players.test(f.src || '')))
            out.push('player audio incorporato');
        return out.join(', ');
    }"""
    try:
        found = core.pw_bridge.run(lambda b: b.active_page().evaluate(_JS)) or ""
    except Exception:
        found = ""
    if found:
        return found
    if check_text and text:
        for pattern, reason in _AV_TEXT_MARKERS:
            if re.search(pattern, text, re.IGNORECASE):
                return reason
    return ""


class WanderGuard:
    """Brings the agent back to the list of studies if it lingers elsewhere.

    The clock runs only while the active tab LINGERS on the platform launch
    surface without a task running: a study detail page, account pages,
    results... It resets whenever the agent is on the list, or anywhere a
    study is actually in progress -- that must never be cut short. "In
    progress" is two cases, and both reset the clock:
      * an external survey host (off the platform's domain), and
      * a platform-HOSTED task such as /data-collection-tool/..., which is
        still on the platform's domain and is NOT the list, so the old
        `_on_guarded_site` test let the clock keep running straight through
        it. Measured in run_20261106_084555: the clock started on the detail
        page, never reset while the agent worked the data-collection-tool
        trials, fired mid-task at wander_seconds=98 and yanked it to
        /studies; the model then read the reserved slot as a finished study
        and went for 'Cancel participation'. Same root cause as the
        launch-surface timer fix, so it reuses that predicate.
    After `seconds` lingering, check() returns True once and the caller sends
    it back to /studies."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self.seconds, self.clock, self.since = float(seconds or 0), clock, None

    def check(self, url: str) -> bool:
        if self.seconds <= 0:
            return False
        if not _on_launch_surface(url) or sites.current.home_path.match(urlparse(url).path or ""):
            self.since = None
            return False
        now = self.clock()
        if self.since is None:
            self.since = now
            return False
        if now - self.since >= self.seconds:
            self.since = None
            return True
        return False


def _extract_study_duration(x: float | None = None, y: float | None = None) -> float | None:
    """Return the estimated completion time in minutes from the current page.

    When x/y viewport coordinates are provided, walks up the DOM from the
    element at that point so we read only the study card being clicked —
    not durations of other study cards visible on the same page.
    Falls back to the full page text when the walk-up finds nothing (e.g.
    clicks on buttons/modals far from the duration text in the DOM).
    """
    try:
        text = ""
        if x is not None and y is not None:
            text = core.pw_bridge.evaluate(f"""
                (function() {{
                    var el = document.elementFromPoint({x}, {y});
                    for (var i = 0; i < 20 && el && el !== document.body; i++) {{
                        var t = el.innerText || '';
                        if (/\\d+\\s*(?:minute|min)/i.test(t)) return t;
                        el = el.parentElement;
                    }}
                    return '';
                }})()
            """) or ""
        if not re.search(r"\d+\s*(?:minute|min)", text, re.IGNORECASE):
            text = core.pw_bridge.evaluate("document.body.innerText") or ""
        m = re.search(r"(\d+)\s*(?:minute|min)s?", text, re.IGNORECASE)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def _extract_time_remaining() -> float | None:
    """Seconds left on the platform's own countdown for the reserved slot, read
    from the study detail page ("Time remaining  13 Mins 49 Secs"), or None.

    This is the deadline that decides whether the work is paid: overrun it
    and the platform auto-returns the submission. Nothing used to read it -- the
    harness tracked only the MINIMUM duration -- so a study answered
    correctly but slowly was lost with no warning (run_20261106_154221,
    22m40s against a ~15m window; the page text is kept in
    a captured fixture page).
    """
    try:
        m = sites.current.deadline_re.search(core._page_text(6000) or "")
        if not m or not any(m.groups()):
            return None
        h, mi, se = (float(g or 0) for g in m.groups())
        total = h * 3600 + mi * 60 + se
        return total if total > 0 else None
    except Exception:
        return None


class StudyTimer:
    """Tracks minimum completion time for the current study.

    Started when the agent leaves the platform study detail page toward an
    external survey. The duration is read from the platform page before the
    click. If `min_seconds` has not elapsed when the agent tries to submit,
    the status_line injected into the agent's turn tells it to wait.
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.start_time: float | None = None
        self.min_seconds: float = 0
        # the platform's OWN countdown for the reserved slot ("Time remaining:
        # 13 Mins 49 Secs" on the study detail page). Overrun it and the
        # submission is auto-returned however well the study was answered:
        # run_20261106_154221 answered a 3-minute questionnaire correctly in
        # 22m40s against a ~15m window and lost GBP 0,45. 0 = unknown.
        self.deadline_seconds: float = 0

    def start(self, min_seconds: float, deadline_seconds: float = 0):
        self.start_time = self.clock()
        self.min_seconds = float(min_seconds)
        self.deadline_seconds = float(deadline_seconds or 0)

    def reset(self):
        self.start_time = None
        self.min_seconds = 0
        self.deadline_seconds = 0

    def deadline_remaining(self) -> float | None:
        """Seconds before the platform takes the slot back, or None if unknown."""
        if self.start_time is None or self.deadline_seconds <= 0:
            return None
        return self.deadline_seconds - self.elapsed()

    def deadline_pressure(self) -> float | None:
        """0.0 at the start of the window, 1.0 at the deadline, >1 past it.
        None when the platform showed no countdown."""
        rem = self.deadline_remaining()
        if rem is None:
            return None
        return 1.0 - (rem / self.deadline_seconds)

    def elapsed(self) -> float:
        if self.start_time is None:
            return 0.0
        return self.clock() - self.start_time

    def remaining(self) -> float:
        if self.start_time is None or self.min_seconds <= 0:
            return 0.0
        return max(0.0, self.min_seconds - self.elapsed())

    def is_active(self) -> bool:
        return self.start_time is not None

    def status_line(self) -> str | None:
        """One-liner injected into every agent turn while a study is active."""
        if not self.is_active():
            return None
        rem = self.remaining()
        if rem <= 0:
            line = (f"Study timer: {self.elapsed():.0f}s elapsed / "
                    f"{self.min_seconds:.0f}s minimum — OK to submit now.")
        else:
            line = (f"Study timer: {self.elapsed():.0f}s elapsed, "
                    f"{rem:.0f}s still needed (minimum {self.min_seconds:.0f}s). "
                    "Do NOT submit yet — use the `wait` action on the last page.")
        return line + (self.deadline_line() or "")

    def deadline_line(self) -> str | None:
        """The hard half of the clock: how long before the platform takes the slot
        back. Appended to the status line so the model sees both bounds, not
        only the minimum it must not undercut."""
        left = self.deadline_remaining()
        if left is None:
            return None
        if left <= 0:
            return (f" DEADLINE PASSED: {sites.current.title}'s countdown for this slot ran "
                    f"out {abs(left) / 60:.0f} min ago. Submit whatever you can "
                    f"right now; do not start new sections.")
        mins = left / 60
        if mins <= 3:
            return (f" DEADLINE {mins:.1f} min away: {sites.current.title} returns this "
                    f"submission automatically when its countdown ends, and the "
                    f"work is lost. Finish and submit NOW -- answer the quickest "
                    f"acceptable way, skip anything optional.")
        if mins <= 8:
            return (f" Deadline in {mins:.0f} min ({sites.current.title} auto-returns the "
                    f"submission then). Keep moving and do not re-read pages.")
        return f" Deadline in {mins:.0f} min."


def terminate_refusal(timer: "StudyTimer") -> str:
    """Why `terminate` must not go through yet, or "" if it may."""
    if timer.is_active() and timer.remaining() > core.TERMINATE_GRACE_SECONDS:
        return (f"TERMINATE REFUSED: study timer has {int(timer.remaining())}s remaining "
                f"(minimum {timer.min_seconds:.0f}s). "
                "Use the `wait` action to wait (e.g. wait 30), then try again.")
    return ""


def saw_submission_confirmation(text: str) -> bool:
    """True if the platform page shows a post-submit/post-cancel confirmation.

    Used by the NOCODE guard: a legit NOCODE either comes AFTER having visited
    the external survey, or AFTER the platform-side 'I didn't receive a code' /
    'Cancel participation' flow landed on a confirmation screen. The guard
    refuses NOCODE only when BOTH signals are absent -- i.e. the model is
    still on the study-detail page, having done neither.
    """
    return bool(text) and bool(sites.current.submission_confirmation_re.search(text))


def guarded_nocode_refusal(answer: str, url: str, saw_external: bool,
                           saw_cancel_confirm: bool, attempts: int) -> str:
    """Refuse a NOCODE emitted on the platform without having done the study.

    Narrow conjunctive guard that reproduces exactly the bug in
    run_20261029_184717: the agent is still on a platform study-detail page,
    the external survey was never opened, and no platform-side submission /
    cancel confirmation ever appeared -- yet the model calls terminate NOCODE.
    That marks the study DONE on our side while the platform still has it in
    'awaiting submission', and the next cycle's _wait_for_studies sits forever
    on an empty list.

    Also catches "CODE: NOCODE" (the model treating the word NOCODE as a
    completion code): same root cause, same guard.

    Returns the refusal string, or "" to let the terminate through. A safety
    valve lets the 3rd attempt pass regardless so a stale platform label can
    never genuinely wedge the loop.
    """
    if not core._is_nocode_answer(answer):
        return ""
    if not core._on_guarded_site(url):
        return ""
    if saw_external or saw_cancel_confirm:
        return ""
    if attempts >= 2:
        return ""                      # safety valve: 3rd try passes
    return sites.current.nocode_refusal()


_NEXT_RE = re.compile(
    r"(?i)(^>>$|^>$|^→$|\bnext\b|\bcontinue\b|\bavanti\b|\bcontinua\b|\bweiter\b"
    r"|\bsuivant\b|\bproceed\b|\bforward\b)")


def _is_nav_button(label: str) -> bool:
    """True if a button label is a page-navigation control (Next/Continue/>>)."""
    return bool(label and _NEXT_RE.search(label.strip()) and not core._SUBMIT_RE.search(label))


def _click_hits_nav(x: float, y: float) -> str:
    """Return the label if the click target is a Next/Continue/>> button, else ""."""
    _JS = r"""([x, y]) => {
        const el = document.elementFromPoint(x, y);
        if (!el) return "";
        let node = el;
        for (let i = 0; i < 10 && node && node !== document.body; i++) {
            const tag = node.tagName;
            if (tag === 'BUTTON' || tag === 'INPUT' || tag === 'A'
                || node.getAttribute('role') === 'button') {
                const t = (node.tagName === 'INPUT' ? node.value : node.textContent) || '';
                const aria = node.getAttribute('aria-label') || '';
                return (t.trim() || aria.trim()).replace(/\s+/g, ' ');
            }
            node = node.parentElement;
        }
        return "";
    }"""
    try:
        label = core.pw_bridge.run(lambda b: b.active_page().evaluate(_JS, [x, y]))
    except Exception:
        return ""
    return label if _is_nav_button(label) else ""


def _reading_dwell(text: str) -> float:
    """Seconds a human would spend reading `text` before clicking Next.

    Uses ~180-280 WPM (randomized) which covers casual-to-careful survey
    reading. Clamped to [1.5, 25] so very short pages still get a brief
    pause and very long pages don't stall forever.
    """
    words = len(text.split())
    if words < 8:
        return 0.0
    wpm = random.uniform(180, 280)
    base = (words / wpm) * 60.0
    jitter = random.uniform(0.8, 1.3)
    return max(1.5, min(25.0, base * jitter))


def _completion_return_box():
    """Viewport box of the control that returns this task to the platform
    (the profile's completion_return_js finds it), or None. Read-only:
    finding it never clicks anything."""
    js = sites.current.completion_return_js
    if not js:
        return None
    try:
        return core.pw_bridge.evaluate(js) or None
    except Exception:
        return None


# How far outside the control a click may land and still count as aimed at it.
# run_20261109_120846 missed by 8px on a 36px-tall button; 40 leaves room for a
# worse estimate without reaching a neighbouring control (and the inert-point
# test below is what actually makes the snap safe, not this number).
_RETURN_NEAR_MISS_PX = 40


_NEAR_MISS_JS_TAIL = r""";
  if (!box) return null;
  const x0 = box.x, y0 = box.y, x1 = box.x + box.width, y1 = box.y + box.height;
  if (x >= x0 && x <= x1 && y >= y0 && y <= y1) return null;   // already on it
  const dx = x < x0 ? x0 - x : (x > x1 ? x - x1 : 0);
  const dy = y < y0 ? y0 - y : (y > y1 ? y - y1 : 0);
  if (Math.hypot(dx, dy) > margin) return null;
  // Never hijack a click that really did land on some other control: only a
  // point on inert background is a miss. Walk up from the hit element because
  // the label/icon inside a button is what elementFromPoint returns.
  const ROLES = {button: 1, link: 1, checkbox: 1, radio: 1, tab: 1,
                 menuitem: 1, option: 1, switch: 1};
  let n = document.elementFromPoint(x, y);
  for (let i = 0; n && i < 10 && n !== document.body; i++, n = n.parentElement) {
    const tag = n.tagName;
    if (tag === 'A' || tag === 'BUTTON' || tag === 'INPUT' || tag === 'SELECT'
        || tag === 'TEXTAREA' || tag === 'LABEL' || tag === 'SUMMARY'
        || ROLES[n.getAttribute('role')] || n.hasAttribute('onclick')
        || n.tabIndex >= 0) return null;
  }
  box.miss = Math.round(Math.hypot(dx, dy));
  return box;
}"""


def _completion_near_miss_js() -> str:
    """The near-miss probe: the profile's return-control finder, wrapped in a
    test of how far the click landed from it. "" when the profile has no
    such control."""
    finder = sites.current.completion_return_js
    if not finder:
        return ""
    return r"""([x, y, margin]) => {
  const box = """ + finder.strip() + _NEAR_MISS_JS_TAIL


def _completion_return_near_miss(x: float, y: float,
                                 margin: int = _RETURN_NEAR_MISS_PX):
    """Box of the return-to-the-platform control when the click at (x, y)
    was plainly aimed at it and missed, else None.

    run_20261109_120846 step 50: a 36px-tall "return and collect your reward"
    button (y 566..601), clicked at y=609 -- eight pixels under its bottom
    edge. The click hit the page background, nothing happened, and the model
    concluded the button was broken: it waited, switched tab back to the
    platform, pressed its finish control with no code in hand, read the
    resulting spinner as a disabled button, and sat one click away from the
    "no completion code" control before coming back and hitting the same
    button correctly (same screenshot, y=584). Six wasted steps at the one
    moment of a run where a wrong click costs the whole task.

    Same remedy as the checkbox retry in dispatch_action: trust the DOM box
    over the pixel estimate and re-aim. The snap fires only when the point is
    on inert background, so a click that did land on some other control is
    never hijacked -- and because it only rewrites the coordinate, the
    minimum-time hold still sees (and can still refuse) the corrected click.
    """
    js = _completion_near_miss_js()
    if not js:
        return None
    try:
        return core.pw_bridge.run(lambda b: b.active_page().evaluate(
            js, [x, y, margin])) or None
    except Exception:
        return None


def _nocode_link_box():
    """Viewport box of the platform's "I didn't receive a completion code"
    control (the profile's nocode_link_js), or None. Read-only: finding it
    never clicks anything."""
    js = sites.current.nocode_link_js
    if not js:
        return None
    try:
        return core.pw_bridge.evaluate(js) or None
    except Exception:
        return None


def nocode_link_decision(answer: str, url: str, link_found: bool,
                         saw_cancel_confirm: bool, seen_codes: list,
                         page_codes: list, attempts: int,
                         already_clicked: bool) -> str:
    """What to do with a no-code `terminate` while that link is on screen.

    "codes"  a completion code WAS seen on the study's pages: refuse and name
             it rather than forfeiting it by submitting without one.
    "click"  press the link for the model (first time only).
    "retry"  the link is still there after we pressed it: say so in words.
    ""       let the terminate through -- no panel open (so the submission is
             already made, or there is nothing left to press), the study was
             closed on the platform, the answer is not a no-code one, or the
             safety valve has been reached.
    """
    if not core._is_nocode_answer(answer):
        return ""
    if not core._on_guarded_site(url):
        return ""
    if saw_cancel_confirm:
        return ""                      # already submitted/cancelled on the platform
    if not link_found:
        return ""
    if attempts >= 2:
        return ""                      # safety valve: 3rd try passes
    if seen_codes or page_codes:
        return "codes"
    return "retry" if already_clicked else "click"


def nocode_codes_refusal(seen: list) -> str:
    code = next((c for c in seen if c), "")
    return sites.current.nocode_codes_refusal_text(code)


def completion_pushback(answer: str, url: str, seen_codes: list,
                        page_codes: list, already_pushed: bool) -> str:
    """One-shot nudge when a free-text `terminate` has not actually reached
    the platform, or "" if the termination may stand.

    A platform study is only really finished once you are back on the platform
    with a completion code (that case is caught earlier, from the URL) or a
    code was shown on the study's own pages. A free-text "I completed the
    study" while still on the survey host, with no code seen anywhere, almost
    always means the model stopped on the host's own "thank you"/validation
    screen and never clicked the final control that submits and returns to
    the platform -- so nothing was registered. Generic on purpose: it keys off the
    invariant (back on the platform OR a code), not on any one platform, trial
    count or button label. Fires at most once per study; if the model still
    terminates after it, that judgment is obeyed.
    """
    if already_pushed:
        return ""
    if answer.startswith("CODE:"):
        return ""                      # a code was found: check_code handles it
    # NOCODE used to be exempt here ("a code outcome: check_code handles it"),
    # but check_code only compares codes -- it has nothing to say about a
    # study that was never submitted. NOCODE on the survey host with no code
    # anywhere is exactly the miss this guard exists for, and it is how
    # run_20261109_102733 threw away a validated submission. Still one-shot:
    # a model that genuinely faces a dead end terminates again and is obeyed.
    if core._on_guarded_site(url):
        return ""                      # already back on the platform
    if seen_codes or page_codes:
        return ""                      # a code did appear somewhere
    host = urlparse(url or "").hostname or "this site"
    return sites.current.completion_pushback_text(host)


def _click_hits_submit(x: float, y: float) -> str:
    """Return the label of the submit/finish control at (x, y), else "".

    Walks up from the clicked point to the nearest button/link/role=button and
    reports its label only when that label reads as a whole-survey submit (so a
    'Next'/'Continue' click returns "" and is never held). Top-document only,
    like _check_disabled_near: an iframe survey's own submit is caught by the
    batch-fill peek instead, which searches every frame.
    """
    _JS = r"""([x, y]) => {
        const el = document.elementFromPoint(x, y);
        if (!el) return "";
        let node = el;
        for (let i = 0; i < 10 && node && node !== document.body; i++) {
            const tag = node.tagName;
            if (tag === 'BUTTON' || tag === 'INPUT' || tag === 'A'
                || node.getAttribute('role') === 'button') {
                const t = (node.tagName === 'INPUT' ? node.value : node.textContent) || '';
                const aria = node.getAttribute('aria-label') || '';
                return (t.trim() || aria.trim()).replace(/\s+/g, ' ');
            }
            node = node.parentElement;
        }
        return "";
    }"""
    try:
        label = core.pw_bridge.run(lambda b: b.active_page().evaluate(_JS, [x, y]))
    except Exception:
        return ""
    return label if core._is_terminal_submit(label) else ""



class TaskPlatform(Behavior):
    name = "task_platform"

    # --------------------------------------------------------------- state
    def start(self, run, start_url):
        if start_url:
            _learn_app_host(start_url)
        self.wander = WanderGuard(run.options.get("wander_seconds", 0))
        self.study_timer = StudyTimer()
        run.timer = self.study_timer
        self.skip_av = bool(run.options.get("skip_av"))
        self._last_guarded_duration = None  # persists across clicks within a study entry
        # the platform's remaining-time countdown, read off the detail page
        # before the launch click. See _extract_time_remaining.
        self._last_guarded_deadline = None
        self._prev_was_guarded = False  # tracks previous step's site so ANY action triggers timer
        # Prev step was on the platform's launch surface (list/detail/chrome).
        # Leaving it is what starts the task timer.
        self._prev_on_launch = bool(sites.current.hosts)
        # Page object the launch surface was last seen in, and the chrome
        # URLs we have already explained once (see _reached_in_launch_tab).
        self._launch_page_id = None
        self._cur_page_id = None
        self._chrome_guard_noticed = set()
        self._current_study_id = None  # id of the study being looked at / taken
        self._current_study_title = ""  # its name, read on the platform while we can still see it
        self._seen_codes = []  # completion codes spotted on the study's own pages
        self._survey_url = ""  # URL of the external survey (for study_memory)
        # Completion guard: nudge a free-text "I'm done" that never got back to
        # the platform / a code, at most once per study so a re-terminate is obeyed.
        self._completion_pushed_back = False
        # One shot per study at clicking the survey's own return-to-the-platform
        # control ourselves (see the profile's completion_return_js).
        self._completion_return_tried = False
        # NOCODE guard: refuse a terminate NOCODE emitted while still on the
        # the platform detail page without having visited the external survey or
        # seen a platform-side cancel/submit confirmation. See
        # guarded_nocode_refusal for the full rationale.
        self._saw_external_this_study = False
        self._saw_cancel_confirm = False
        self._nocode_guard_attempts = 0
        # Sibling guard, platform side: a no-code terminate while the
        # 'Confirm submission' panel is still open. See _nocode_link_box.
        self._nocode_link_attempts = 0
        self._nocode_link_tried = False
        # Reservation-loop guard: once a slot is reserved on the platform the URL
        # becomes /studies/{id} and stays there — the only legitimate exits
        # are 'Start study' or 'Cancel participation'. The model sometimes
        # tries to escape with history_back / visit_url instead (seen in
        # run_20261103_194847 after an older user_hint was misread as a
        # standing rule to cancel). Count those abort attempts on the same
        # detail URL; after RESERVED_LOOP_THRESHOLD we inject a nudge that
        # names the only two valid exits.
        self._reserved_loop_url = ""
        self._reserved_loop_count = 0
        # Any step that ends still on /studies/{id} with no URL change from
        # the previous step counts as 'stuck'. The model in run_20261104_073003
        # wasted 13 steps tab-switching and pressing Win/Ctrl+L while the
        # URL never moved -- none counted as abort attempts, so the original
        # history_back-only guard never fired. Fallback threshold is lower
        # than the abort-attempt one because 'stuck' already means no useful
        # browser transition is happening.
        self._reserved_stuck_count = 0
        # One-shot per reservation. Gates BOTH the deterministic launcher (when
        # the agent is stuck on a fresh, un-launched reservation the harness
        # opens the external survey itself via pw_bridge.launch_reserved_study,
        # using stable data-testid selectors, instead of leaving a fragile
        # pixel-click + below-the-fold confirm to the vision model) AND the
        # text nudge that is its fallback.
        self._reserved_loop_nudged = False
        # Orphan-reservation guard: see _orphan_reservation_visible. On the
        # reserved detail page the model has been seen to hallucinate a
        # 'Take part in this study' button that does not exist (the panel
        # actually shows 'I'm finished' + 'Cancel participation'), then
        # click into the Confirm-submission flow with no code to enter.
        # Nudged once per visit to the detail page, reset on return to
        # /studies so re-entry re-fires it after context compaction.
        self._orphan_nudged = False
        # First "your submission failed our checks" wording seen this run,
        # read off the fingerprint the change signal already computes, so
        # noticing it costs no extra page read. Kept per run: it goes into
        # the step's diag line, and from there into summary.json.
        self._qc_flag = ""
        # Per step.
        self.here_guarded = False
        self.here_on_launch = False
        self.on_list = False
        self.m_study = None
        self.skip_hint = ""
        self.qc_hit = ""
        self._timer_started_this_step = False
        self.pre_click_duration = None

    # ------------------------------------------------------------ features
    def allows(self, run, feature):
        if feature == "batch_fill":
            # Gate on the LAUNCH SURFACE, not on the host. Batch-fill must
            # never touch the platform's own chrome (the studies list, a detail
            # page, submissions/account) -- but a platform-HOSTED task
            # (/data-collection-tool/...) is a running questionnaire like any
            # other, and `here_guarded` locked it out of batch-fill purely
            # because it shares the domain. run_20261106_154221 answered a
            # 3-minute dropdown questionnaire with 139 separate vision clicks
            # at 8.3s each, took 22m40s against the platform's ~15m window, and
            # the submission was auto-returned. Same confusion the wander
            # guard had in 1ffb26d.
            return not self.here_on_launch
        if feature == "page_hints":
            # Nothing to hint on the studies list.
            return not self.on_list
        if feature == "reading_dwell":
            # Gated on the launch surface, not the host. Skipping the dwell
            # on a platform-HOSTED questionnaire made the agent answer real
            # questions with no reading time at all -- the exact behavioural
            # signature the humanised input path exists to avoid -- purely
            # because the task shares the platform's domain. `on_list` stays:
            # there is nothing to read on the studies list.
            # ...but never at the cost of the slot: reading dwell is the
            # first thing to drop once the platform's countdown is inside its
            # last quarter. A page read like a human but submitted after the
            # deadline is auto-returned and pays nothing.
            _dl_pressure = self.study_timer.deadline_pressure()
            _dwell_ok = _dl_pressure is None or _dl_pressure < 0.75
            return not self.on_list and not self.here_on_launch and _dwell_ok
        if feature == "remember_page":
            # Only pages with no fillable form reach this far (batch-fill
            # returns before it), which is exactly the scenario / instruction
            # / stimulus set a later question can refer back to. Never the
            # platform's own pages.
            return not self.here_guarded
        return True

    def wants(self, run, feature):
        if feature == "prescroll":
            # Pre-screenshot viewport nudge: in speed modes, inside a study,
            # on non-guarded pages (the platform dashboard is excluded).
            return self.study_timer.is_active() and not self.here_guarded
        if feature == "auto_enter":
            # Only while a study is running.
            return self.study_timer.is_active()
        return False

    # -------------------------------------------------------------- arrive
    # How long a sign-in page has to stay before it counts as a session that
    # ended: signing in and token refreshes pass through one for a moment.
    SIGNIN_RECHECK_SECONDS = 4.0
    #: The answer a run ends with when it finds the account signed out; the
    #: loop that drives the runs stops on it (see gui_server).
    SIGNIN_ANSWER = "SIGNIN_REQUIRED"

    def arrive(self, run):
        here = run.here
        study_timer = self.study_timer
        if sites.current.is_signin(here):
            # The agent never signs in for the user, and nothing else makes
            # sense on this page: not the task timer, not the batch fill, not
            # a bounce back to the app (which only leads here again). Say so
            # and stop; with the run stopped the panel's manual control is
            # free for the user to sign in.
            try:
                core.cancel.sleep(self.SIGNIN_RECHECK_SECONDS)
                again = core.get_current_url() or here
            except core.cancel.Cancelled:
                raise
            except Exception:
                again = here
            if sites.current.is_signin(again):
                text = ("Sessione scaduta: il browser è sulla pagina di accesso. "
                        "Accedi dal pannello (Controllo manuale) e riavvia la run.")
                run.write({"step": run.step, "type": "notice", "text": text, "url": again})
                core._notify_telegram("🔐 WebDolly: " + text)
                yield {"type": "notice", "text": text, "messages": run.messages}
                yield {"type": "done", "answer": self.SIGNIN_ANSWER, "messages": run.messages}
                return True
            run.here = here = again
        if core.is_forbidden_url(here):
            back = core.safe_page_for(here)
            core.pw_bridge.goto(back)
            core.pw_bridge.wait_for_content()
            run.pending = sites.current.forbidden_notice()
            yield {"type": "notice",
                   "text": sites.current.forbidden_live_notice(),
                   "messages": run.messages}
        elif self.wander.check(here):
            core.pw_bridge.goto(core.safe_page_for(here))
            core.pw_bridge.wait_for_content()
            n = int(self.wander.seconds)
            run.pending = sites.current.wander_notice(n)
            yield {"type": "notice",
                   "text": sites.current.wander_live_notice(n),
                   "messages": run.messages}

        # Back on the platform with the completion code in the URL: the
        # submission is already made, so finish here instead of asking the
        # model to read the code off the screen.
        cc = core.completion_code(here)
        if cc:
            core.capture_page("complete")
            answer = f"CODE: {cc}"
            # Backstop: the submit guards above hold the final submit until
            # the timer clears, so reaching the completion URL early should
            # be impossible. If it happens anyway (e.g. a survey whose only
            # end control is an unlabeled arrow that the label classifier
            # cannot recognise as a submit), the study is already returned
            # and cannot be undone -- but flag it loudly instead of logging
            # a silent success, so the slipping survey can be handled.
            early = (study_timer.is_active()
                     and study_timer.remaining() > core.TERMINATE_GRACE_SECONDS)
            run.write({
                "step": run.step, "action_type": "terminate", "args": {"answer": answer},
                "code_source": "url", "url": here,
                "early_return": bool(early),
                "timer_remaining": round(study_timer.remaining(), 1) if early else None,
            })
            core._restore_user_speed()
            core._close_extra_tabs()
            if early:
                rem = int(study_timer.remaining())
                core._live({"type": "notice",
                            "text": f"⚠️ Studio restituito {rem}s SOTTO il tempo minimo "
                                    f"(min {study_timer.min_seconds:.0f}s) via URL: submit sfuggito alle gate."})
                core._notify_telegram(f"⚠️ WebDolly: studio restituito TROPPO PRESTO\n{answer}\n"
                                      f"{rem}s sotto il minimo ({study_timer.min_seconds:.0f}s)\n{here}")
            else:
                core._notify_telegram(f"WebDolly: studio completato ✅\n{answer} (dall'URL)\n"
                                      f"Ciclo: {run.run_dir.name if run.run_dir else '?'}")
            yield {"type": "notice", "text": f"Codice di completamento letto dall'URL: {cc}",
                   "messages": run.messages}
            yield {"type": "done", "answer": answer, "messages": run.messages}
            return True

        # Stateful transition check: if previous step was on the platform and now we're
        # external, start the timer regardless of HOW the navigation happened
        # (left_click, key press, Enter in a modal, JS redirect, etc.).
        # Done BEFORE the batch-fill block so the timer is already active when
        # batch-fill decides whether to hold a final submit on the very first
        # external page (a one-page fill-and-submit survey would otherwise be
        # submitted before the timer ever started).
        here_guarded = core._on_guarded_site(here)
        # Stranded on the marketing site (a click on its logo/nav, or a
        # redirect, not just a mistyped visit_url): walk straight back to
        # the app. Nothing on the platform's marketing site can advance a study, and
        # the model does not recognise it as the wrong site -- it treated
        # /ai-task-builder as a broken studies list for 23 steps
        # (run_20261106_142347). Never fires inside a study: an external
        # survey is not on the guarded domain at all.
        if core._off_app_host(here):
            back = sites.current.app_home_url()
            core._live({"type": "notice",
                        "text": f"[guard] off the app host ({urlparse(here).hostname}) "
                                f"-> back to {back}"})
            try:
                core.pw_bridge.goto(back)
                core.pw_bridge.wait_for_content()
                here = core.pw_bridge.current_url() or back
            except Exception:
                here = back
            here_guarded = core._on_guarded_site(here)
            if not run.pending:
                run.pending = sites.current.off_host_notice(back, sites.current.app_host)
        run.here = here
        self.here_guarded = here_guarded
        self._timer_started_this_step = False
        # Proactive, cache-aware compaction at natural checkpoints: when
        # we cross the platform/external boundary (either direction),
        # most of the history for the OTHER side (list browsing, old
        # studies, prior survey pages) is no longer useful. Collapse it
        # once BEFORE this step grows the context further, so the model
        # starts the new page with a small stable prefix and llama's
        # prompt cache can warm cheaply for the next 10+ steps.
        if self._prev_was_guarded != here_guarded and len(run.messages) > 6:
            core._hard_compact_context(run.messages, keep_recent=2)
        here_on_launch = _on_launch_surface(here)
        try:
            self._cur_page_id = core.pw_bridge.active_page_id()
        except Exception:
            self._cur_page_id = None
        if not here_on_launch and _reached_in_launch_tab(
                here, self._cur_page_id, self._launch_page_id):
            here_on_launch = True
            if here not in self._chrome_guard_noticed:
                self._chrome_guard_noticed.add(here)
                core._live({"type": "notice", "text":
                            sites.current.chrome_guard_live_notice(here)})
        self.here_on_launch = here_on_launch
        # "Inside a task" = off the launch surface: an external survey host
        # OR a platform-hosted task tool (e.g. /data-collection-tool/...).
        # The study timer and in-task framing key on THIS transition, not on
        # "left the platform's domain", so first-party platform surveys are treated as
        # the running task they are instead of looking like the reservation
        # page (which made the model cancel completable studies).
        entered_task = self._prev_on_launch and not here_on_launch
        if self._prev_was_guarded and not here_guarded:
            # NOCODE-guard signal: strictly "reached an EXTERNAL survey host"
            # (unchanged on purpose -- see guarded_nocode_refusal).
            self._saw_external_this_study = True
        if entered_task and not study_timer.is_active():
            effective_duration = self._last_guarded_duration
            if effective_duration is not None:
                study_timer.start(effective_duration * 60,
                                  deadline_seconds=self._last_guarded_deadline or 0)
                core._live({"type": "timer_start", "min_seconds": study_timer.min_seconds,
                            "min_minutes": effective_duration})
            else:
                study_timer.start(0, deadline_seconds=self._last_guarded_deadline or 0)
                core._live({"type": "timer_start", "min_seconds": 0, "min_minutes": 0})
            self._timer_started_this_step = True
            self._survey_url = here
            dest = "external" if not here_guarded else f"{sites.current.name}-hosted task"
            core._live({"type": "notice", "text": f"[timer] transition detected: launch→{dest} | duration={effective_duration}min"})
            dur_str = f" (~{effective_duration:.0f} min)" if effective_duration else ""
            core._notify_telegram(f"📝 Studio iniziato{dur_str}\n{here}")
        self._prev_was_guarded = here_guarded
        self._prev_on_launch = here_on_launch
        if here_on_launch and self._cur_page_id is not None:
            self._launch_page_id = self._cur_page_id
        return False

    # -------------------------------------------------------------- settle
    def settle(self, run):
        here = run.here
        here_guarded = self.here_guarded
        study_timer = self.study_timer
        # Reset timer when back at the studies list (timer re-arms for next study).
        on_list = here_guarded and bool(sites.current.home_path.match(urlparse(here).path or ""))
        self.on_list = on_list
        if on_list:
            study_timer.reset()
            core._restore_user_speed()
            self._last_guarded_duration = None
            self._last_guarded_deadline = None
            self._prev_was_guarded = False
            self._current_study_id = None
            self._current_study_title = ""
            self._seen_codes = []
            self._completion_pushed_back = False
            self._completion_return_tried = False
            self._saw_external_this_study = False
            self._saw_cancel_confirm = False
            self._nocode_guard_attempts = 0
            self._nocode_link_attempts = 0
            self._nocode_link_tried = False
            run.study_pages = []
            self._reset_reserved()
            # Back on the studies list: everything from the previous
            # study is history that will never be relevant again --
            # collapse it in one shot so the next cycle starts with a
            # tiny cache-stable context.
            if len(run.messages) > 6:
                core._hard_compact_context(run.messages, keep_recent=1)

        # --- studies that lead nowhere (and, opt-in, audio/video ones) ---
        # A study that is listed but not doable gets opened again on every
        # cycle: the model has no memory across cycles, so without this it
        # re-discovers the same dead end for as long as it stays listed.
        # The wording is only read on the study page, where it is a
        # description; on an external questionnaire only real media
        # elements count, because aborting a survey halfway on a false
        # positive costs much more than one wasted click.
        m_study = sites.current.task_path.match(urlparse(here).path or "") if here_guarded else None
        self.m_study = m_study
        if m_study:
            self._current_study_id = m_study.group(1)
            core.capture_page("study")
            # Read the name here, on the platform: once the agent is inside the
            # external questionnaire the page title is the survey tool's,
            # which is no use for telling the model what to skip.
            self._current_study_title = _study_title() or self._current_study_title

        skip_reason = ""
        if m_study:
            known = study_cache.entries().get(self._current_study_id)
            if known:
                # Opened again anyway (the title hint is only a hint): bounce
                # out before the model spends steps rediscovering it.
                skip_reason = known.get("reason", "scartato in un ciclo precedente")
            else:
                page_text = core._page_text()
                skip_reason = _detect_dead_end(page_text)
                if not skip_reason and self.skip_av:
                    av = _detect_av(page_text, check_text=True)
                    if av:
                        skip_reason = f"audio/video: {av}"
        # Gated on the launch surface, not the host: a platform-HOSTED
        # task (/data-collection-tool/...) that turns out to need a
        # microphone or webcam has to be abandoned exactly like an
        # external one. `here_guarded` excluded it for sharing the
        # domain. Same confusion as 1ffb26d and 88c15c4.
        elif self.skip_av and not self.here_on_launch and study_timer.is_active():
            av = _detect_av(check_text=False)
            if av:
                skip_reason = f"audio/video: {av}"

        if skip_reason:
            title = self._current_study_title or _study_title()
            key = self._current_study_id or title or here
            study_cache.mark(key, skip_reason, title=title, url=here)
            run.write({"step": run.step, "type": "skip", "reason": skip_reason,
                       "study": key, "title": title, "url": here,
                       "matched": _last_dead_end_sentence})
            abandoned = study_timer.is_active()
            study_timer.reset()
            core._restore_user_speed()
            self._last_guarded_duration = None
            self._last_guarded_deadline = None
            self._current_study_id = None
            self._current_study_title = ""
            try:
                core.pw_bridge.goto(core.studies_url())
                core.pw_bridge.wait_for_content()
                self._prev_was_guarded = True
                self._prev_on_launch = True   # back on the studies list (a launch surface)
            except Exception:
                pass
            run.pending = (
                f"That study was ruled out automatically ({skip_reason}) and you were "
                "taken back to the list of studies. Do not open it again: pick another "
                "study, or follow your task instructions if none is eligible.")
            label = f"«{title}»" if title else "lo studio aperto"
            link = (f"\n{sites.current.task_url(key)}"
                    if sites.current.task_url(key) else "")
            core._notify_telegram(f"⏭ Studio saltato: {label}\n{skip_reason}{link}\n"
                                  "Se ti interessa, puoi farlo a mano.")
            injected = yield {"type": "notice",
                              "text": (f"Studio saltato ({skip_reason}): {label}"
                                       + (", survey abbandonata" if abandoned else "")
                                       + ". Non verrà riaperto nei prossimi cicli."),
                              "messages": run.messages}
            if injected:   # an instruction typed while this was happening
                run.pending += "\n\n" + injected

        # Codes shown on the study's own pages, collected as they appear:
        # by the time the model reports one it is back on the platform, where
        # the original page can no longer be checked.
        if study_timer.is_active() and not here_guarded:
            for c in codes_in_url(here) + codes_on_page(core._end_page_text()):
                if c not in self._seen_codes:
                    self._seen_codes.append(c)

        # Flag platform-side submit/cancel confirmations so the NOCODE
        # guard knows this terminate came after a legitimate close
        # ("I didn't receive a code" confirmation OR Cancel participation
        # confirmation), not from clicking "I'm finished" on the detail
        # page without ever submitting.
        if here_guarded and not self._saw_cancel_confirm:
            try:
                if saw_submission_confirmation(core._end_page_text()):
                    self._saw_cancel_confirm = True
            except Exception:
                pass

        self.skip_hint = study_cache.prompt_line() if on_list else ""

    def _reset_reserved(self):
        self._reserved_loop_url = ""
        self._reserved_loop_count = 0
        self._reserved_stuck_count = 0
        self._reserved_loop_nudged = False
        self._orphan_nudged = False

    # ---------------------------------------------------------- the page
    def after_screenshot(self, run, page_fp):
        # the platform's own pages list past rejections with their reasons; reading
        # one there would stamp this run with someone else's verdict. The
        # researcher's end page is never on a guarded host.
        qc_hit = "" if (self._qc_flag or self.here_guarded) else qc_failure_in(page_fp)
        if qc_hit:
            self._qc_flag = qc_hit
        self.qc_hit = qc_hit

    # ------------------------------------------------------------- prompts
    def first_prompt(self, run):
        resume_hint = ""
        # Off the task list at step 1 means a cycle is resuming work a
        # previous one started -- on a platform.
        if not self.on_list and sites.current.home_url():
            resume_hint = f"\n\nCurrent URL: {run.here}\n"
            if self.here_guarded and self.m_study:
                resume_hint += sites.current.resume_on_task_page()
            else:
                resume_hint += sites.current.resume_in_task()
        return (f"\n\n{self.skip_hint}" if self.skip_hint else "") + resume_hint

    def prompt_hints(self, run):
        return self.skip_hint

    def prompt_suffix(self, run):
        study_timer = self.study_timer
        out = ""
        # platform-hosted task surface (e.g. the data-collection-tool):
        # it lives on the app host, so the model keeps mistaking a
        # trial it is actively doing for the studies list / reservation
        # page and cancels a study it is completing. State plainly where
        # it is. (Guarded + off the launch surface == a first-party task.)
        if self.here_guarded and not self.here_on_launch:
            out += sites.current.in_task_framing()
        timer_line = study_timer.status_line()
        if timer_line:
            out += timer_line + "\n"
            core._live({"type": "timer", "elapsed": study_timer.elapsed(),
                        "remaining": study_timer.remaining(),
                        "min_seconds": study_timer.min_seconds})
            if study_timer.min_seconds > 0:
                over = study_timer.elapsed() - study_timer.min_seconds
                if over > 120:
                    core._auto_escalate("super_fast")
                elif over > 0:
                    core._auto_escalate("hurry")
        return out

    # ------------------------------------------------------------- actions
    def before_action(self, run):
        here = run.here
        here_guarded = self.here_guarded
        study_timer = self.study_timer
        action_type, args = run.action_type, run.args
        # Read study duration from the platform page BEFORE dispatch so we
        # capture the exact study about to be joined (not generic browsing).
        # Pass click coordinates so elementFromPoint targets that specific card.
        self.pre_click_duration = None
        if action_type == "left_click" and core._on_guarded_site(here):
            coord = args.get("coordinate")
            cx, cy = (coord[0], coord[1]) if coord else (None, None)
            self.pre_click_duration = _extract_study_duration(cx, cy)
            if self.pre_click_duration is not None:
                self._last_guarded_duration = self.pre_click_duration
            # the platform's countdown is only on the detail page, so it has
            # to be read before the click that leaves it.
            _pcd = _extract_time_remaining()
            if _pcd is not None:
                self._last_guarded_deadline = _pcd

        # Minimum-time guard, model-driven path: on an external survey, while
        # the study timer is still below its minimum, a left_click that lands
        # on a submit/finish control is held and turned into a wait instead of
        # being dispatched. Once submitted the survey redirects to the platform
        # with the completion code and the study is returned irreversibly, so
        # the hold has to happen here, before dispatch. Next/Continue clicks
        # (and every click off a submit control) are not affected. Setting
        # the pending reply also suppresses the auto-chain-Enter after the
        # action, which would otherwise submit the page the click did not.
        # Near-miss on the control that ends the study: re-aim, don't
        # waste the click. Runs BEFORE the minimum-time guard below so the
        # corrected coordinate is what that guard judges -- a re-aimed
        # click can still be held back if the study minimum is not up.
        if action_type == "left_click" and not here_guarded:
            _coord = args.get("coordinate")
            if _coord:
                _sbox = _completion_return_near_miss(_coord[0], _coord[1])
                if _sbox:
                    _nx = _sbox["x"] + _sbox["width"] * random.uniform(0.3, 0.7)
                    _ny = _sbox["y"] + _sbox["height"] * random.uniform(0.35, 0.65)
                    run.action_note = (f" [re-aimed {_coord[1]:.0f}->{_ny:.0f}y onto "
                                       f"the return control]")
                    args["coordinate"] = [_nx, _ny]
                    core._live({"type": "notice", "text":
                                "[completion] click missed the survey's return control "
                                f"({_sbox.get('text')!r}) by {_sbox.get('miss')}px: "
                                "re-aimed onto it"})

        submit_held = ""
        if (action_type == "left_click"
                and not here_guarded
                and study_timer.is_active()
                and study_timer.remaining() > core.TERMINATE_GRACE_SECONDS):
            coord = args.get("coordinate")
            if coord:
                submit_held = _click_hits_submit(coord[0], coord[1])

        if submit_held:
            rem = int(study_timer.remaining())
            result_desc = (f"SUBMIT HELD (study timer): '{submit_held}' not clicked, "
                           f"~{rem}s below the study minimum")
            core._live({"type": "notice",
                        "text": f"[timer] submit '{submit_held}' trattenuto: {rem}s sotto il minimo"})
            try:
                core.cancel.sleep(min(30.0, max(5.0, study_timer.remaining())))
            except core.cancel.Cancelled:
                raise
            except Exception:
                pass
            run.pending = (study_timer.status_line() or
                           "Do NOT submit yet; wait for the study minimum time.")
            return result_desc

        # Reading dwell: before clicking a Next/Continue/>> button on
        # an external survey, pause proportionally to the visible text
        # so the page isn't traversed at inhuman speed.
        if (action_type == "left_click" and not here_guarded
                and study_timer.is_active()):
            coord = args.get("coordinate")
            if coord:
                nav_label = _click_hits_nav(coord[0], coord[1])
                if nav_label:
                    try:
                        page_text = core._page_text(12000)
                        dwell = _reading_dwell(page_text)
                        if dwell > 0:
                            core._live({"type": "notice",
                                        "text": f"[dwell] reading pause {dwell:.1f}s before '{nav_label}'"})
                            core.cancel.sleep(dwell)
                    except core.cancel.Cancelled:
                        raise
                    except Exception:
                        pass
        return None

    def after_action(self, run):
        here = run.here
        action_type, args = run.action_type, run.args
        # Reservation-loop guard: on a platform /studies/{id} detail page
        # where the orphan banner is visible, the slot is held and the only
        # way out is 'I'm finished' (with a real code) or 'Cancel
        # participation'. Two independent signals feed one nudge:
        #   (A) orphan-detector: on first entry with the banner visible,
        #       inject a one-shot clarification of what the buttons mean
        #       (the model was observed hallucinating a non-existent 'Take
        #       part' button over the actual 'I'm finished' in
        #       run_20261104_073003).
        #   (B) loop-detector: count explicit abort attempts PLUS any step
        #       that ends on the same detail URL without progress; fire the
        #       nudge after 2 abort attempts or 6 stuck steps.
        m_resv = sites.current.task_path.match(urlparse(here).path or "") if self.here_guarded else None
        if not m_resv:
            # Off the detail page: counters reset.
            self._reset_reserved()
            return
        detail_id = m_resv.group(1)
        # A visit to a different detail page resets everything for it.
        if self._reserved_loop_url != detail_id:
            self._reset_reserved()
            self._reserved_loop_url = detail_id

        # Launch state of this reserved slot. A study whose survey was
        # never opened this run (_saw_external_this_study False) and
        # that STILL OFFERS a way in ('Take part', 'Open study in new
        # window', or an open 'Start study' dialog) is mid-launch, not
        # a dead slot: both nudges must push the LAUNCH, never 'Cancel
        # participation', because the platform's dialog hides its confirm
        # link below the fold and abandoning there returns a
        # completable study (run_20261105_181001 lost a GBP 4 eval,
        # run_20261106_124620 a GBP 2,60 one, exactly this way).
        #
        # The orphan BANNER cannot carry that distinction, in either
        # direction. Captured: PRESENT on a never-launched
        # reservation that still offered 'Open study in new window'
        # (run_20261106_124620 screenshot_03), ABSENT on a reserved
        # study with its Start dialog open (fixture study 6aec5848,
        # the captured fixtures). Keying fresh_unlaunched off it made
        # the flag permanently False on the pages it was meant to
        # protect, which silently disabled the auto-launcher and
        # routed every stuck launch to the cancel steer. The DOM
        # launch control is the honest signal; the banner only says
        # whether the slot is already held.
        orphan_visible = _orphan_reservation_visible()
        fresh_unlaunched = (_launch_control_visible()
                            and not self._saw_external_this_study)

        # (A) Held-slot one-shot informational nudge, branched on the
        #     same launch state so a held-but-launchable study is told
        #     to start, not to cancel.
        if not run.pending and not self._orphan_nudged:
            if orphan_visible and fresh_unlaunched:
                self._orphan_nudged = True
                run.pending = _reservation_nudge(True, detail_id)
            elif orphan_visible:
                self._orphan_nudged = True
                run.pending = sites.current.held_slot_notice()

        # (B) Loop detector. Normalise keys to a lowered '+'-joined
        # string so patterns like 'alt+left' match regardless of how
        # the model spelt the modifiers.
        keys_joined = "+".join(
            (k or "").lower() for k in (args.get("keys") or [])
        )
        is_abort_attempt = (
            action_type in ("history_back", "visit_url")
            or (action_type == "key" and keys_joined in (
                "backspace", "alt+left", "alt+right", "ctrl+l",
                "ctrl+tab", "ctrl+shift+tab", "ctrl+next",
                "ctrl+prior", "ctrl+pagedown", "ctrl+pageup",
            ))
        )
        # Clicks on the top the platform nav header (the Studies /
        # Submissions / About you / Messages tabs all sit within the
        # first ~50px) are escape attempts too. The reserved banner is
        # at y<=30 so the primary nav is roughly y 30..100 depending on
        # layout; y<=110 keeps a safety margin without catching the
        # right-hand panel buttons (which start at y>=550).
        if action_type == "left_click":
            coord = args.get("coordinate") or ()
            if len(coord) >= 2 and coord[1] <= 110:
                is_abort_attempt = True

        if is_abort_attempt:
            self._reserved_loop_count += 1
        # Stuck: current URL matches previous URL and both are this
        # detail page. prev_here is "" on the very first iteration,
        # so no false positive at run start.
        if (run.prev_here
                and urlparse(run.prev_here).path == urlparse(here).path):
            self._reserved_stuck_count += 1

        # A fresh un-launched reservation gets a LOWER stuck threshold:
        # the harness steps in to launch the survey before the vision
        # model fumbles the dialog and drifts toward 'Cancel'
        # (run_20261105_181001 clicked Cancel at step 12; stuck hit 4 at
        # step 8). An orphan/held slot keeps the original 6.
        stuck_limit = 4 if fresh_unlaunched else 6
        if (not self._reserved_loop_nudged
                and not run.pending
                and (self._reserved_loop_count >= 2
                     or self._reserved_stuck_count >= stuck_limit)):
            self._reserved_loop_nudged = True
            launched = False
            if fresh_unlaunched:
                # Deterministic humanised launch grounded on stable
                # data-testid selectors: (Take part ->) Open study in
                # new window -> tick checkbox(es) -> Start study, then
                # follow the survey tab. Real xdotool cursor, not the
                # fragile pixel-click + below-the-fold confirm that lost
                # the study.
                res = _humanized_launch_reserved_study(include_take_part=True)
                status = (res or {}).get("status")
                launched = status == "launched"
                core._live({"type": "notice",
                            "text": f"[launch] humanised launch: {status}"})
                if launched:
                    self._survey_url = (res or {}).get("survey_url") or self._survey_url
                    run.pending = sites.current.launched_notice()
                    core._notify_telegram("🚀 Survey aperta automaticamente\n" + str(self._survey_url))
            if not launched:
                # Launch not possible (still un-reserved, control
                # missing, or click blocked): fall back to guidance.
                run.pending = _reservation_nudge(fresh_unlaunched, detail_id)

    def diag(self, run):
        study_timer = self.study_timer
        active = study_timer.is_active()
        d = {
            "guarded": self.here_guarded,
            "prev_guarded": self._prev_was_guarded,  # value BEFORE this step updated it
            "timer_active": active,
            "timer_elapsed": round(study_timer.elapsed(), 1) if active else None,
            "timer_remaining": round(study_timer.remaining(), 1) if active else None,
            "timer_min_sec": study_timer.min_seconds if active else None,
            "last_guarded_duration": self._last_guarded_duration,
        }
        if run.action_type == "left_click" and self.here_guarded:
            d["pre_click_duration"] = self.pre_click_duration
        if self.qc_hit:
            d["qc_flag"] = self.qc_hit
        d["timer_started"] = self._timer_started_this_step
        return d

    # ----------------------------------------------------------- terminate
    def terminate(self, run, answer):
        here = run.here
        refusal = terminate_refusal(self.study_timer)
        if refusal:
            return refusal
        # NOCODE emitted while still on the platform without ever visiting
        # the external survey or seeing a cancel/submit confirmation
        # means the study is still "assigned" on the platform. Letting
        # that NOCODE through wedges the next cycle in
        # _wait_for_studies (empty list, slot still held). Narrow
        # conjunctive guard; see guarded_nocode_refusal.
        nocode_block = guarded_nocode_refusal(
            answer, here, self._saw_external_this_study,
            self._saw_cancel_confirm, self._nocode_guard_attempts)
        if nocode_block:
            self._nocode_guard_attempts += 1
            return nocode_block
        # The other half of the same mistake, one page later: the
        # survey WAS done, we are back on the platform, and the model
        # gives up with the 'Confirm submission' panel still open and
        # 'I didn't receive a completion code' one click away. The
        # link's presence is proof the study is unsubmitted; press it
        # rather than argue. See _nocode_link_box.
        if (core._is_nocode_answer(answer) and core._on_guarded_site(here)
                and not self._saw_cancel_confirm
                and self._nocode_link_attempts < 2):
            _lbox_seen = _nocode_link_box()
            _codes_now = codes_on_page(core._end_page_text())
            _nc = nocode_link_decision(
                answer, here, _lbox_seen is not None, self._saw_cancel_confirm,
                self._seen_codes, _codes_now, self._nocode_link_attempts,
                self._nocode_link_tried)
            if _nc:
                self._nocode_link_attempts += 1
            if _nc == "codes":
                return nocode_codes_refusal(self._seen_codes + _codes_now)
            if _nc == "click":
                _lbox = core._scroll_box_into_view(_nocode_link_box)
                if _lbox and core._humanized_click_box(_lbox):
                    self._nocode_link_tried = True
                    core.cancel.sleep(random.uniform(1.2, 2.2))
                    try:
                        _after = core.pw_bridge.current_url()
                    except Exception:
                        _after = here
                    core._live({"type": "notice", "text":
                                sites.current.nocode_link_live_notice(_lbox.get('text'), _after)})
                    return sites.current.nocode_link_clicked(_lbox.get('text'))
                _nc = "retry"
            if _nc == "retry":
                return sites.current.nocode_link_refusal
        # Ending on the survey host's own thank-you/validation page
        # with the final "submit & return to the platform" control
        # skipped. Before saying anything to the model, try to finish
        # the job: find that control and click it with the real
        # cursor. Prose alone was not enough: run_20261109_102733
        # emitted NOCODE with that control in the middle of the screen.
        _page_codes_now = codes_on_page(core._end_page_text())
        if (not self._completion_return_tried
                and not answer.startswith("CODE:")
                and not core._on_guarded_site(here)
                and not self._seen_codes and not _page_codes_now):
            _rbox = core._scroll_box_into_view(_completion_return_box)
            if _rbox:
                self._completion_return_tried = True
                if core._humanized_click_box(_rbox):
                    core.cancel.sleep(random.uniform(1.2, 2.2))
                    try:
                        _after = core.pw_bridge.current_url()
                    except Exception:
                        _after = here
                    core._live({"type": "notice", "text":
                                "[completion] the study was not submitted: clicked "
                                f"the survey's return control ({_rbox.get('why')}, "
                                f"{_rbox.get('text')!r}) -> {_after}"})
                    return sites.current.completion_return_clicked(_rbox.get('text'), _after)
        # Prose fallback: no such control was found, or clicking it
        # failed. Nudge once.
        nudge = completion_pushback(
            answer, here, self._seen_codes,
            _page_codes_now, self._completion_pushed_back)
        if nudge:
            self._completion_pushed_back = True
            return nudge
        return None

    def finish_record(self, run, answer):
        check = None
        if answer.startswith("CODE:") or answer == "NOCODE":
            # Compare what the model reported with every code seen on
            # the study's pages, plus the one the platform shows after the
            # submission ("Completion code: PLKXLQ"). Never overrides
            # the model: it only flags what needs a human look.
            seen = self._seen_codes + [c for c in codes_on_page(core._end_page_text())
                                       if c not in self._seen_codes]
            check = check_code(answer, seen)
            if check in ("mismatch", "missed"):
                what = ("ha chiuso senza codice" if check == "missed"
                        else f"ha inserito {answer.split(':', 1)[1].strip()}")
                core._notify_telegram(f"⚠️ Codice da verificare: il modello {what}, "
                                      f"ma sulle pagine dello studio c'era "
                                      f"{', '.join(seen[:3])}.\n{run.here}")
        return {"code_source": "model", "code_check": check,
                "codes_seen": self._seen_codes[:5]}

    def finished(self, run, answer):
        if answer.startswith("CODE:") or answer == "NOCODE":
            core._notify_telegram(f"WebDolly: studio completato ✅\n{answer}\n"
                                  f"Ciclo: {run.run_dir.name if run.run_dir else '?'}")
        if self._survey_url:
            core.study_memory.record(self._survey_url,
                                     steps=run.step, outcome=answer[:20])
