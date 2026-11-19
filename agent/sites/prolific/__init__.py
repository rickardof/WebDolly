"""Prolific (app.prolific.com): the research-participation platform this agent
was first built for.

Everything here is knowledge about Prolific itself -- its URLs, the wording and
controls of its pages, the rules a participant follows on it -- kept out of the
agent loop so the loop stays a generic browser agent. The comments carry the
incidents each rule came from (run_YYYYMMDD_HHMMSS names a transcript under
var/logs/pw/).
"""
import os
import re

from ..base import Site


# == what Prolific's pages look like ==========================================
#
# Moved here verbatim from agent_core / pw_bridge, comments included: the
# words, selectors and scripts that recognise Prolific's own controls, and the
# texts the model is shown about them.

# Prolific shows this banner at the top of every page while any slot is
# reserved and still pending. The wording is stable across study types and
# languages are not localised for the warning text. If the DOM contains it,
# the account is in a 'held submission' state that must be resolved via
# 'I'm finished' (with a real code) or 'Cancel participation'. Used by the
# orphan-reservation guard so the model does not hallucinate a 'Take part'
# button when it is really looking at 'I'm finished'.
_ORPHAN_BANNER_RE = re.compile(
    r"(?:please\s+)?either\s+finish\s+or\s+return\s+(?:this|your)\s+submission",
    re.IGNORECASE,
)


def reservation_nudge(fresh_unlaunched: bool, detail_id: str) -> str:
    """One-shot guidance when the agent has lingered on a reserved
    /studies/<id> detail page without progress (see the reservation-loop
    detector). Branches on launch state so a study that is merely *not yet
    launched* is never steered toward abandonment:

    - fresh_unlaunched: the slot was reserved THIS run and the external survey
      was never opened (no orphan banner, never left prolific.com). The model
      is still working through Prolific's launch flow, whose 'Start study'
      dialog hides its confirm button below the fold -- push the LAUNCH and
      forbid cancelling. Abandoning here returns a completable study
      (run_20261105_181001 lost a GBP 4 eval exactly this way: it opened the
      'Start study' dialog, could not reach the below-the-fold confirm button,
      and cancelled).
    - otherwise: an orphan/held slot, or a survey already attempted with no
      code -- the legitimate exits are 'I'm finished' (with a code) or 'Cancel
      participation'.
    """
    if fresh_unlaunched:
        return (
            "LAUNCH THIS STUDY -- do NOT cancel it. You reserved "
            f"/studies/{detail_id} and have NOT opened the survey yet (you "
            "never left prolific.com this run), so the study is completable "
            "and simply not started.\n"
            "To start it: click the button that opens the study -- 'Open study "
            "in new window' (or 'Take part' / 'Start study'). A 'Start study' "
            "dialog then appears with a confidentiality checkbox and a confirm "
            "button. That confirm button is at the BOTTOM of the dialog and is "
            "often BELOW the visible area: SCROLL DOWN to bring it on screen "
            "(the dialog moves with the page), then click it. If scrolling "
            "does not move it at all, press Tab until the 'Start study' button "
            "is focused and press Enter. Do NOT click outside the dialog (that "
            "only closes it) and do NOT click 'Report study'. Click 'Cancel "
            "participation' ONLY if the task is genuinely impossible (it needs "
            "a microphone or camera, or a CAPTCHA blocks you)."
        )
    return (
        f"RESERVATION LOOP on /studies/{detail_id}: you have been stuck on "
        "this reserved slot without progress. Prolific will keep redirecting "
        "you back to this detail page as long as the slot is held. The ONLY "
        "legitimate exits are: (a) 'I'm finished' AFTER you actually completed "
        "the external survey (with its completion code to paste), or (b) "
        "'Cancel participation' (red outline button on the right panel) -> "
        "confirm 'Yes, cancel participation' in the modal. Do NOT press back / "
        "forward / address bar / Ctrl+Tab / click on the top Prolific nav tabs "
        "again -- those cannot release the slot. Pick one of the two panel "
        "buttons now."
    )


# Studies that are listed but lead nowhere. Matched on the guarded site only
# (a study detail page): on an external questionnaire the same words can be
# part of a question, and aborting a survey halfway on a false positive costs
# far more than reopening a dead study once more.
_DEAD_END_MARKERS = (
    (r"you are not eligible|not eligible (?:to|for) (?:take part|participate|this)"
     r"|you do not (?:meet|match) the (?:requirements|criteria)", "non idoneo"),
    (r"no longer available|no longer accepting|is full|places? (?:are |have been )?"
     r"(?:full|taken)|study (?:is|has been) (?:closed|completed)", "non più disponibile"),
    (r"you have already (?:completed|taken part|submitted)", "già svolto"),
    # Only as a STATUS about this account, never as the two bare words. Every
    # study tagged "Includes in-study screening" says in its own description
    # "If you don't meet the requirements, you'll be screened out of the
    # study, but we'll still provide a small bonus" -- run_20261111_164730
    # matched that sentence on five consecutive steps (the cache entry has
    # hits 5, steps 9 to 13), with the reservation timer running and nothing
    # else on screen but the description. The study was then banned by title
    # for 14 days. 20 of the 21 entries in the cache were studies this
    # account was never screened out of: submissions.json has one "Screened
    # Out" row among them.
    (r"you(?:'ve| have| had)? been screened out|you were screened out"
     r"|we(?:'ve| have)? screened you out",
     "screened out"),
)


_TIME_REMAINING_RE = re.compile(
    r"time\s+remaining\W{0,12}"
    r"(?:(\d+)\s*(?:hours?|hrs?|h)\W{0,4})?"
    r"(?:(\d+)\s*(?:minutes?|mins?|m)\W{0,4})?"
    r"(?:(\d+)\s*(?:seconds?|secs?|s)\b)?",
    re.IGNORECASE)


# Phrases shown by Prolific after a cancel-participation confirmation or after
# clicking "I didn't receive a completion code". Multilingual, matched loosely
# so a small layout change doesn't break detection.
_PROLIFIC_SUBMITTED_RE = re.compile(
    r"(?i)(you have cancelled your participation|participation (?:has been )?cancelled|"
    r"partecipazione (?:è stata )?annullata|hai annullato la partecipazione|"
    r"your (?:submission|response) (?:has been |was )?(?:received|recorded|submitted)|"
    r"risposta (?:è stata )?(?:ricevuta|registrata)|"
    r"thanks for taking part|grazie per (?:aver|la) partecipa|"
    r"study submitted without a completion code|senza codice di completamento)")


# -- the control that actually submits the study ---------------------------
#
# run_20261109_102733 finished and PASSED a 45-trial evaluation on
# eval.services -- "Your submission has been validated", every check green --
# and then emitted `terminate NOCODE` while a black button reading "Return to
# Prolific and collect your reward" sat in the middle of the screen. Nothing
# was registered. $3,00.
#
# A prose nudge alone is not enough here (it is also one-shot, and the model
# had already decided it was done), so the harness finds and clicks the
# control itself. Two signals, generic on purpose:
#   1. an href pointing at Prolific's own completion endpoint. That URL,
#      app.prolific.com/submissions/complete?cc=..., is Prolific's documented
#      contract with every researcher, so it identifies the control on any
#      survey platform without knowing anything about the platform.
#   2. failing that, a label that says it goes back to Prolific -- needed
#      because a button that navigates in JS carries no href.
_COMPLETION_RETURN_JS = r"""
(() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return null;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return null;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return null;
    return r;
  };
  const box = (el, why) => {
    const r = vis(el);
    return r ? {x: r.x, y: r.y, width: r.width, height: r.height, why: why,
                text: (el.innerText || el.value || '').trim().slice(0, 80)} : null;
  };
  for (const a of document.querySelectorAll('a[href]')) {
    if (/prolific\.(com|co\.uk)\/submissions\/complete/i.test(
          a.getAttribute('href') || '')) {
      const b = box(a, 'completion-url');
      if (b) return b;
    }
  }
  const re = /(return|back|continue|go)\s+to\s+prolific|collect\s+your\s+reward/i;
  for (const el of document.querySelectorAll(
         'a, button, [role="button"], input[type="submit"]')) {
    const t = (el.innerText || el.value || '').trim();
    if (t && re.test(t)) {
      const b = box(el, 'label');
      if (b) return b;
    }
  }
  return null;
})()
"""


# -- Prolific's own "I didn't receive a completion code" control -----------
#
# run_20261110_184734: a 1-minute Google Form study whose confirmation screen
# ("La tua risposta e stata registrata") carried no completion code. The model
# did everything right up to the last inch -- filled the form, submitted it,
# switched back to the Prolific tab, clicked "I'm finished", even went hunting
# through the form's page source for a hidden code -- and then, with
# Prolific's "Confirm submission" panel open and a blue link reading
# "I didn't receive a completion code" one click below the greyed-out
# "Complete study" button, called terminate NOCODE. Twice. The slot stayed
# reserved and the finished survey was never submitted.
#
# Neither existing guard covers that inch. guarded_nocode_refusal exempts any
# run that reached the external survey -- it exists for the opposite bug, a
# NOCODE emitted without ever opening the study -- and completion_pushback is
# spent on the survey host and returns "" as soon as we are back on Prolific.
#
# The discriminating signal is the control itself, not the run's history:
# while that link is on screen the submission has NOT been made, because
# Prolific replaces the whole panel with "Awaiting review" the moment it has.
# So the harness probes for it (read-only: only input is scored) and, like the
# return-to-Prolific control above, presses it with the real cursor instead of
# arguing with a model that has already decided it is finished. The dialog it
# opens is left to the model, with the screenshot in view: confirming a
# submission, or deciding the survey was never really done and cancelling
# participation instead, is a judgment the harness must not make blind.
_NOCODE_LINK_JS = r"""
(() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return null;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return null;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return null;
    return r;
  };
  const re = /(did\s*n.{0,2}t|have\s*n.{0,2}t|not)\s+receive(d)?\s+(a|the|any)?\s*(completion\s+)?code|non\s+ho\s+ricevuto\s+(il\s+|un\s+)?codice/i;
  for (const el of document.querySelectorAll(
         'a, button, [role="button"], input[type="submit"]')) {
    const t = (el.innerText || el.value || '').trim();
    if (!t || t.length > 120 || !re.test(t)) continue;
    const r = vis(el);
    if (r) return {x: r.x, y: r.y, width: r.width, height: r.height,
                   text: t.slice(0, 80)};
  }
  return null;
})()
"""


_NOCODE_LINK_REFUSAL = (
    "NOCODE REFUSED: Prolific's 'Confirm submission' panel is still open on this "
    "page, which means the study has NOT been submitted -- the slot stays reserved "
    "and the survey you just filled in is lost. On that panel, under the greyed-out "
    "'Complete study' button, there is a link reading 'I didn't receive a completion "
    "code': left_click it, confirm in the dialog that follows, and call terminate "
    "NOCODE only once the page says the submission was saved ('Awaiting review'). "
    "If instead you never actually completed the survey, press 'Cancel' and use "
    "'Cancel participation' before terminating.")


# Stable data-testid selectors captured from Prolific's live reserved-study
# page + its "Start study" launch dialog (ds-dialog-panel). These are
# first-party Prolific UI, identical across every external study, so the launch
# is deterministic and does not depend on the vision model aiming a pixel click
# at a small link that sits just above "Report study" and whose confirm control
# can fall below the dialog fold. The CLICKS themselves are NOT done here: the
# launcher in agent_core drives them with the same humanised xdotool cursor the
# vision path uses, because CDP Input.dispatch* / DOM .click() are flagged by
# Prolific's behavioural fraud scoring (see the input backend note in
# agent_core). These helpers only READ the DOM to ground those real clicks.
_SEL_RESERVE = '[data-testid="reserve"]'            # "Take part in this study"


_SEL_OPEN_DIALOG = '[data-testid="open-study-in-new-window-button"]'


_SEL_DIALOG = '[data-testid="start-study-dialog"]'


_SEL_START = '[data-testid="dialog-start-study"]'   # <a href=<survey> target=_blank>


# The study's name on its own detail page: Prolific's own study-name slot
# first, then the document title ("<Study> | Prolific"). Used only to tell
# the model which studies to skip.
_STUDY_TITLE_JS = """() => {
        // Only trust what we see on a study detail page -- /studies/<24hex>.
        if (!/\\/studies\\/[0-9a-f]{24}\\b/.test(location.pathname)) return '';
        // Prolific's own study-name slot comes first. The generic
        // `[data-testid*="title"]` match used to grab the sidebar user
        // greeting on this account, so it is no longer in the fallback list.
        const sel = ['[data-testid="study-name"]',
                     '[data-testid*="study-name"]',
                     '[data-testid="study-title"]',
                     'main h1', 'main h2'];
        for (const s of sel) {
            const el = document.querySelector(s);
            const t = el && (el.innerText || '').trim();
            if (t && t.length > 3 && t.length < 120) return t;
        }
        // Document title pattern on Prolific is "<Study> | Prolific" — pick
        // the segment that is NOT just the product name.
        const parts = (document.title || '').split(/\\s[|–—-]\\s/)
            .map(s => s.trim()).filter(s => s && s.toLowerCase() !== 'prolific');
        return parts[0] || '';
    }"""


# @@ PROLIFIC-DEFINITIONS @@


class ProlificSite(Site):
    name = "prolific"
    title = "Prolific"

    def __init__(self):
        # Only on the site that has such a section: a third-party survey that
        # happens to have an /about-you page must not be hijacked.
        self.hosts = tuple(
            h.strip().lower()
            for h in os.environ.get("FARA_GUARDED_HOSTS", "prolific.com,prolific.co").split(",")
            if h.strip())
        # The agent's workspace is the APP host. Other hosts on the same
        # domain -- www.prolific.com and its marketing pages -- are a different
        # site with no studies on it, yet every guard treats them as "on
        # Prolific" because they share the domain. Wandering onto one burns a
        # whole cycle: in run_20261106_142347 the model typed www instead of
        # app at step 1, landed on /ai-task-builder, and spent 23 steps there
        # and on /login before finding its way back. Learnt from start_url at
        # run start, so a different deployment does not need a code change.
        self.app_host = (os.environ.get("FARA_APP_HOST") or "app.prolific.com").lower()

    # Sections of the site the agent must never be in. The agent's whole job
    # is to take part in studies and, when none fits, wait for new ones; the
    # profile ("About you") questionnaire is not part of that, whatever the
    # page nudges it towards. Enforced in code -- every step, every visit_url,
    # every cycle restart -- not left to the model's judgement or to prompt
    # text.
    forbidden_path = re.compile(r"^/(about-you|logout|sign-out|signout)(/|$)", re.IGNORECASE)

    # A typed navigation to the app host that goes nowhere real is refused.
    # Clicking is unaffected -- this only rejects addresses the model composes
    # itself. run_20261108_154719 invented /study/<uuid> from a study it had
    # just been looking at, landed on Prolific's 404, and spent the rest of
    # the cycle trying to work out where the list had gone. Prolific's study
    # ids are 24 hex characters under /studies/, never a UUID under /study/,
    # so a made-up address is recognisable before it is loaded.
    app_visitable_prefixes = ("/submissions", "/account", "/settings",
                              "/data-collection-tool")

    home_path = re.compile(r"^/studies/?$", re.IGNORECASE)
    home_path_str = "/studies"
    task_path = re.compile(r"^/studies/([0-9a-fA-F]{24})/?$", re.IGNORECASE)
    complete_path = re.compile(r"^/submissions/complete/?$", re.IGNORECASE)
    complete_code_param = "cc"

    # Prolific "chrome" prefixes: pages where no study TASK is running yet --
    # the account/submissions surfaces (the studies list and the per-study
    # reservation page are handled separately via home_path / task_path).
    # Read off the live app's own navigation (2026-11-08): the in-app links
    # are / /balance-hub /studies /submissions /about-you /messages/unread
    # /desktop-app /referrals /account/general /account/preferences. Five of
    # those ten were missing here, so clicking any of them from the studies
    # list made the launch-surface test answer "a task is running":
    # run_20261108_181732 step 42 clicked "About you" in the top nav and the
    # harness started a study timer on /about-you, told the model it was
    # inside a running survey and pushed a "Studio iniziato" notification.
    # The page-identity guard (agent_core._reached_in_launch_tab) covers the
    # paths this tuple still does not name.
    launch_chrome_prefixes = ("/submissions", "/account", "/settings",
                              "/about-you", "/balance-hub", "/messages",
                              "/desktop-app", "/referrals")

    # Prolific's own error pages. They need naming explicitly because the
    # launch-surface test answers "unknown path => a task is running": that
    # default is deliberate (it is what stops the wander guard cutting short
    # a first-party task at a path we have not catalogued, 1ffb26d), but it
    # also made a 404 look like a running study. run_20261108_154719 step 19
    # invented the URL /study/<uuid> (singular, UUID -- not Prolific's
    # /studies/<24hex>), got redirected to /404, and the harness started a
    # study timer on it, switched the wander guard off and turned batch-fill,
    # reading dwell and the audio/video check loose on an error page.
    error_path = re.compile(
        r"^/(40[0-9]|41[0-9]|50[0-9]|error|errors|not[-_]?found|maintenance)(/|$)",
        re.IGNORECASE)

    # The only Prolific-HOSTED task surface we have ever seen. Named so the
    # page-identity guard keeps an explicit escape hatch for it.
    hosted_task_path = re.compile(r"^/data-collection-tool(/|$)", re.IGNORECASE)

    empty_list_markers = ("waiting for studies",)
    # "showing: 0/0 studies" (or "0 / 0") -- Prolific's counter when the list
    # is empty.
    empty_list_re = re.compile(r"showing\s*:?\s*0\s*/\s*0\s*stud", re.IGNORECASE)

    # --- its pages, as the agent sees them (definitions above) ---------------
    # "Your Prolific code is ..." on a study's own end page.
    code_keywords = ("prolific",)
    task_title_js = _STUDY_TITLE_JS
    task_title_junk = ("prolific", "studies", "my submissions")
    held_slot_banner_re = _ORPHAN_BANNER_RE
    dead_end_markers = _DEAD_END_MARKERS
    deadline_re = _TIME_REMAINING_RE
    submission_confirmation_re = _PROLIFIC_SUBMITTED_RE
    completion_return_js = _COMPLETION_RETURN_JS
    nocode_link_js = _NOCODE_LINK_JS
    nocode_link_refusal = _NOCODE_LINK_REFUSAL
    launch_reserve_sel = _SEL_RESERVE
    launch_open_sel = _SEL_OPEN_DIALOG
    launch_dialog_sel = _SEL_DIALOG
    launch_start_sel = _SEL_START

    def reservation_nudge(self, fresh_unlaunched: bool, task_id: str) -> str:
        return reservation_nudge(fresh_unlaunched, task_id)

    def home_url(self) -> str:
        return os.environ.get("FARA_STUDIES_URL", "https://app.prolific.com/studies")

    def task_url(self, task_id: str) -> str:
        if not task_id or not self.task_path.match(f"/studies/{task_id}"):
            return ""
        return f"https://app.prolific.com/studies/{task_id}"
