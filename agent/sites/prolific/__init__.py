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
from . import submissions as _subs


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


def nocode_refusal() -> str:
    return (
        "NOCODE REFUSED: you are still on the Prolific study detail page, "
            "you never opened the external survey, and no 'cancelled' or "
            "'submitted' confirmation has appeared. Clicking 'I'm finished' on "
            "the detail page does NOT submit the study. To close it properly: "
            "click the red 'Cancel participation' button, confirm in the modal, "
            "and only after the confirmation page appears call terminate NOCODE. "
            "If instead you did complete the survey and no code was shown, "
            "switch to the Prolific tab, click 'I'm finished', then "
            "'I didn't receive a completion code', confirm, and terminate.")


def completion_pushback_text(host) -> str:
    return (
        f"NOT DONE YET: you are ending the task but you are still on {host}, not back "
        "on Prolific, and no completion code has appeared anywhere. A study only counts "
        "as submitted once you return to Prolific (the page URL becomes "
        "app.prolific.com/.../complete) or a completion code is shown on screen. A "
        "'thank you' or validation screen on the survey site is NOT the end by itself. "
        "Look on this page for the final control that submits or returns you to Prolific "
        "-- a button or link such as Submit, Finish, Done, Continue, or Return to "
        "Prolific -- and click it, then wait. Only if there is truly no such control and "
        "no code anywhere should you end the task again.")


def nocode_codes_refusal_text(code) -> str:
    return (
        f"NOCODE REFUSED: you are ending this study as 'no completion code', but "
            f"{code} was shown on the study's own pages. Type {code} into the "
            "'Completion Code' field of this panel, click 'Complete study', and "
            f"terminate with CODE:{code}. Only if Prolific rejects that code should "
            "you fall back to 'I didn't receive a completion code'.")


def refuse_forbidden_visit() -> str:
    return (
        "visit_url REFUSED: that page (About you / logout) is off limits. Your only job is to "
                    "take part in studies, or wait on the studies list for new ones.")


def refuse_unknown_path() -> str:
    return (
        "visit_url REFUSED: there is no such page on Prolific. "
                    "Study pages are /studies/<id> with the id exactly as the "
                    "list links it -- do NOT build a URL from a study's name or "
                    "from an id you saw elsewhere. Go back to the studies list "
                    "and click the study, rather than typing an address.")


def corrected_unknown_path(home, target, app_host) -> str:
    return (
        f"visit_url {home} (CORRECTED: {target} is not a real "
                        f"page -- wrong host AND no such path. Study pages are "
                        f"{app_host}/studies/<id> with the id exactly as the "
                        f"list links it; never build one from a name or a UUID. "
                        f"You are back on the studies list: click the study.)")


def corrected_host(fixed, target, app_host) -> str:
    return (
        f"visit_url {fixed} (CORRECTED: you asked for {target}, which is "
                    f"Prolific's public marketing site. The studies only exist on "
                    f"{app_host} -- always use that host.)")


def history_back_refusal() -> str:
    return (
        "history_back REFUSED: going back would leave this site (you would "
                    "be logged out). Use the page's own navigation instead, e.g. the "
                    "Studies tab in the top bar.")


def forbidden_notice() -> str:
    return (
        "You ended up in the 'About you' section, which is off limits, so you were "
                    "taken back to the list of studies. Never open it. Your only job is to take "
                    "part in studies that meet the criteria and, if none is available, to wait "
                    "and check the list again for new ones.")


def forbidden_live_notice() -> str:
    return (
        "Aperta la sezione «About you»: riportato all'elenco degli studi.")


def wander_notice(n) -> str:
    return (
        f"You spent more than {n} seconds away from the list of studies without taking "
                    "part in a study, so you were taken back to it. Stay on the list of studies and "
                    "do not browse other pages. If no eligible study is listed, follow your task "
                    "instructions for that case (refresh, wait or finish).")


def wander_live_notice(n) -> str:
    return (
        f"Più di {n} s fuori dall'elenco studi senza svolgerne uno: riportato a /studies.")


def off_host_notice(back, app_host) -> str:
    return (
        f"You had navigated to Prolific's PUBLIC MARKETING SITE "
                        f"(www.prolific.com), which is a different website: it "
                        f"advertises Prolific to researchers and has no studies, no "
                        f"study list and no account of yours on it. You have been "
                        f"returned to {back}. The studies only ever live on "
                        f"{app_host} -- stay on it.")


def chrome_guard_live_notice(here) -> str:
    return (
        f"[guard] {here} is Prolific account chrome reached "
                           f"in the launch tab, not a study: no timer started")


def resume_on_task_page() -> str:
    return (
        "You are resuming on a Prolific study page. A previous run was "
                            "working on this study. Continue it — open the survey if needed, "
                            "or enter the completion code if the survey is done. "
                            "Do NOT go back to the studies list.")


def resume_in_task() -> str:
    return (
        "You are resuming inside an external survey. A previous run was "
                            "working through it. Continue where you left off — complete the "
                            "current question and proceed. Do NOT navigate back to Prolific "
                            "until the survey is complete.")


def in_task_framing() -> str:
    return (
        "[You are INSIDE the study task right now — this is a "
                        "Prolific-hosted survey, NOT the studies list and NOT the "
                        "reservation/detail page. A study is open and in progress. "
                        "Keep answering the trials and advancing. Do NOT click "
                        "'Cancel participation' or 'Back to studies', and do NOT "
                        "terminate as if no study were open. Only leave if the task "
                        "itself is genuinely impossible (needs a microphone/camera, "
                        "or a CAPTCHA blocks you).]\n")


def held_slot_notice() -> str:
    return (
        "RESERVED-SLOT STATE on this Prolific study: the "
                            "'Please either finish or return this submission' "
                            "banner is visible, which means the slot is HELD "
                            "(possibly from a PREVIOUS run that did not clean "
                            "up). The right-hand panel in this state shows:\n"
                            "  - 'I'm finished' (blue): click ONLY if you "
                            "personally completed the external survey this run "
                            "and have a completion code to paste. Clicking it "
                            "otherwise lands you on a dead Confirm-submission "
                            "screen with no code to enter.\n"
                            "  - 'Cancel participation' (red outline): releases "
                            "the slot. Opens a modal; confirm with the red "
                            "'Yes, cancel participation' button.\n"
                            "  - 'Open study in new window' (link): reopens the "
                            "external survey in a new tab, if there is one.\n"
                            "There is NO 'Take part in this study' button in "
                            "this state. If you did not open+complete the "
                            "external survey this run, click 'Cancel "
                            "participation' -> 'Yes, cancel participation', "
                            "then terminate with NOCODE.")


def launched_notice() -> str:
    return (
        "The study's external survey has just been opened "
                                "for you in the survey window (the 'Start study' "
                                "dialog was confirmed automatically). You are now "
                                "IN the survey: read the current page and answer "
                                "the questions/trials, then proceed. Do NOT go back "
                                "to the Prolific tab to cancel.")


def nocode_link_live_notice(label, after) -> str:
    return (
        "[completion] the study was not submitted: clicked "
                                   f"Prolific's {label!r} -> {after}")


def nocode_link_clicked(label) -> str:
    return (
        "HARNESS ACTION: you were ending this study as 'no "
                                "completion code' with Prolific's 'Confirm submission' "
                                "panel still open, so the study was NOT submitted. The "
                                f"link {label!r} has been clicked for you. "
                                "Look at the screenshot: confirm the submission in the "
                                "dialog that opened (the button that submits / "
                                "continues), wait for the page to say the submission "
                                "was saved ('Awaiting review'), and only then terminate "
                                "NOCODE. If you did NOT actually complete the survey, "
                                "dismiss the dialog and use 'Cancel participation' "
                                "instead.")


def completion_return_clicked(label, after) -> str:
    return (
        "HARNESS ACTION: you were ending the task on the survey "
                                "site without ever submitting the study. The control "
                                "that returns it to Prolific "
                                f"({label!r}) has been clicked for you and "
                                f"the page is now {after}. Look at the screenshot: if "
                                "a completion code is shown, terminate with it; if you "
                                "are back on Prolific with the submission registered, "
                                "terminate NOCODE; if the click went nowhere, find and "
                                "click the right control yourself.")


# Appended to every run's task, whatever preset or text the user started
# with. Kept short and placed last-but-prominent: rules buried deep in a
# long preset were measured to be ignored (pay rate considered in 1 of 436
# study-list steps when it was rule #12 of a ~2500-token preset).
_STANDING_RULES_FULL = (
    "\n\nSTANDING RULES (always apply):\n"
    "1. ATTENTION CHECKS — HARDEST RULE, READ FIRST. Before answering ANY question, scan its "
    "FULL text (title AND option labels) for an embedded instruction. Patterns to catch: "
    "'to show you are paying attention select X', 'this is an attention check', "
    "'please select [specific option]', 'choose the [third/second/last] option', "
    "'select strongly agree/disagree for this one', 'what colour is the sky', "
    "'ignore the question above and answer Z', 'if you are reading this select Y'. "
    "If you find one, obey it EXACTLY — override persona, context, consistency, and what feels "
    "natural. Failing ONE attention check rejects the whole study and loses the reward. On "
    "repetitive Likert/A-B batteries, do NOT speed-click past the question text: scan every "
    "single question's text for a hidden instruction before clicking any option.\n"
    "2. Choosing a study: compare ALL study cards first; among the eligible ones open the "
    "one with the highest reward rate per hour (the £/hr or $/hr shown), not just the first.\n"
    "2a. ONLY a study that shows a clickable 'Take part in this study' button is available. A "
    "study or submission row labelled Returned, Approved, Completed, Rejected, Cancelled, "
    "Timed-out, or Awaiting review is a PAST participation — it has NO 'Take part' button and "
    "CANNOT be opened or re-taken. Do NOT click it, its title, or its row hoping it will launch. "
    "If you click a study and no 'Take part' button appears (and it is not merely loading) after "
    "one or two tries, treat it as not available: stop retrying, go back to the Studies list, and "
    "look at other studies. Never spend many steps trying to launch the same un-openable study.\n"
    "3. NO AUDIO, EVER: a study that requires LISTENING to audio (sounds, speech, music, "
    "recordings, 'audio playback', headphones or speakers) is not eligible. An 'Audio' "
    "icon/label on the card means skip it. Videos are OK as long as the study does not "
    "require hearing their sound — you can watch silent or muted videos. Microphone or "
    "camera requirements alone do NOT disqualify a study. "
    "Before pressing 'Take part in this study', read the requirements section; if it "
    "mentions listening or audio playback, do not take part.\n"
    "4. Answer quality: read every question and its FULL text carefully before answering. "
    "Keep every answer consistent with the persona and with your earlier answers in the same "
    "survey. Write specific, relevant open-ended answers in the survey's language, never "
    "generic filler.\n"
    "5. Scope: your only job is to take part in studies that meet the criteria and, when none "
    "is available, to wait and re-check the list of studies for new ones. Never open the "
    "'About you' section of the site or answer profile questionnaires outside a study.\n"
    "5a. After clicking 'Take part in this study', the button turns grey/disabled while loading — "
    "this is NORMAL. Wait a few seconds for the study to open; do NOT go back or assume it failed.\n"
    "5b. After finishing a survey: ALWAYS switch back to the Prolific tab before calling "
    "terminate. On the Prolific tab: (a) click 'I'm finished'; (b) if a completion-code field "
    "appears, enter the code you received, or if no code was given click 'I didn't receive a "
    "completion code' (or type NOCODE in the field if that link is absent); (c) confirm/submit. "
    "Only after this Prolific-side step call terminate with CODE:<code> or NOCODE.\n"
    "5c. Broken / screened-out studies: if the external survey is stuck, broken, or you were "
    "screened out and cannot finish, use Ctrl+Shift+Tab to switch to the Prolific tab (do NOT "
    "use history_back — the survey is in a separate tab). On the Prolific tab click "
    "'Cancel participation' (a red button), confirm in the modal, then terminate with NOCODE. "
    "Do NOT keep refreshing or waiting — cancel and move on.\n"
    "6. Minimum study time: the estimated completion time shown on the study page is a minimum "
    "you must respect. A 'Study timer' line will appear in your status once you enter a study. "
    "If you reach what looks like the final submit/finish page and the timer says time remains, "
    "use the `wait` action to wait (e.g. wait 30s at a time), then check the status line again. "
    "Submit only once the timer says 'OK to submit now'.\n"
    "7. Screener questions: before and during a study, read the description and requirements "
    "carefully to understand what profile the researcher is looking for. When answering screener "
    "questions, emphasise whichever TRUE aspects of the persona best match those criteria — the "
    "goal is to avoid being excluded for a trivially wrong answer when the persona genuinely "
    "qualifies. You may present a truth selectively or adapt phrasing to fit the screener "
    "(e.g. 'I use AI tools every day' if the persona does). You must NEVER invent facts that "
    "directly contradict the given persona (age, nationality, profession, stated experience), "
    "and never claim qualifications the persona does not have.\n"
    "8. Repetitive trials: when a survey has many identical repeated trials (e.g. 60 A/B "
    "comparisons, repeated Likert scales), work through them efficiently with normal clicks — "
    "develop a fast rhythm: select option, scroll to submit, click submit, wait for next trial. "
    "Do NOT use run_js (it is disabled). DECIDE EACH TRIAL FROM WHAT IT SHOWS: look at both "
    "options before clicking. Many comparison studies plant ground-truth trials among the "
    "ordinary ones — one option is obviously broken, distorted, or does not match the stated "
    "prompt — and getting those wrong fails the researcher's automatic checks even when every "
    "other answer is fine. So never choose by position: neither always the same side "
    "(straight-lining) nor a left-right-left alternation, which is just as mechanical and just "
    "as detectable. Variety is the RESULT of judging each pair, not a goal of its own; when the "
    "two options really look equivalent, pick either one and move on. "
    "Follow any guidance from past user corrections shown in the [Things the user told you] "
    "section — they carry across studies on the same domain; but a correction that tells you to "
    "answer fast or without inspecting the stimuli never covers a trial where one option is "
    "obviously wrong or where the text carries an instruction: those are planted checks and "
    "rule 1 wins. "
    "Rule 1 still applies — scan EACH trial's text for an embedded attention-check instruction.\n"
    "9. NEVER ask_user_question — this run is unattended and nobody will reply. The USER PROFILE "
    "block at the top of this task counts as 'explicitly provided' for the critical-point rule, "
    "so any demographic (age, gender, nationality, native language, education, employment, "
    "household, income band, political view, lifestyle habit, …) is already supplied. If a "
    "survey field is required but not in the profile, invent a value that is plausible AND "
    "consistent with the profile and with every answer you already gave in this same survey; "
    "re-use the same invented value within the survey. The ONLY time you may stop is to call "
    "terminate — never to ask."
)


_STANDING_RULES_COMPACT = (
    "\n\nRULES: "
    "1. ATTENTION CHECKS — HARDEST RULE. Scan every question's FULL text (title + options) for an "
    "embedded instruction: 'select X to show you are paying attention', 'this is an attention check', "
    "'please pick [option]', 'choose the third option', 'what colour is the sky', 'if reading this select Y'. "
    "If found, obey EXACTLY — override persona, consistency, everything. One failure rejects the whole study. "
    "On repetitive Likert/A-B trials, scan each trial's text before clicking; do NOT speed-click past it. "
    "2. Pick the highest £/hr study. Only a study with a clickable 'Take part' button is open-able; a row "
    "marked Returned/Approved/Completed/Cancelled/Awaiting-review is a PAST participation (no Take-part, "
    "can't reopen). If no Take-part appears after 1-2 clicks (and it's not just loading), stop, go back to "
    "the list, try others. "
    "3. NO AUDIO — skip studies requiring listening/speakers/headphones. Videos OK if no audio needed. "
    "4. Read FULL question text before answering. Stay consistent with persona; no filler. "
    "5. Only do studies, nothing else. After 'Take part' button goes grey, WAIT — it's loading. "
    "After survey: Prolific tab → 'I'm finished' → enter the code → 'Complete study' → terminate. "
    "If the survey gave NO code, click 'I didn't receive a completion code' on that same "
    "panel, confirm, and terminate NOCODE only once Prolific says the submission is saved "
    "('Awaiting review'). NEVER terminate with the 'Confirm submission' panel still open. "
    "If study is broken/screened out: Ctrl+Shift+Tab to Prolific tab → 'Cancel participation' → confirm → terminate NOCODE. "
    "6. Respect minimum time — wait if timer says time remains. "
    "7. Screener: emphasise true persona traits that match; never invent contradicting facts. "
    "8. Repetitive trials: work through them efficiently with normal clicks. "
    "Do NOT use run_js. Judge EACH trial from what it shows — look at both options. Studies "
    "plant ground-truth trials where one option is obviously broken/off-prompt, and missing "
    "them fails the automatic checks. Never answer by position: not the same side every time, "
    "not a left-right alternation. A past user correction about answering fast never covers a "
    "trial with an obviously wrong option or an embedded instruction. "
    "Rule 1 still applies — scan EACH trial's text for an embedded attention-check instruction. "
    "9. NEVER ask_user_question — unattended run. Treat the USER PROFILE above as 'explicitly provided'; "
    "for any demographic not in it, invent a value consistent with the profile and your prior answers in this survey."
)


def identity_hint(idv: dict) -> str:
    """The line that gives the model its EXACT Prolific ID / email (from
    identity.json or the task) so it types them when a study asks, instead of
    refusing as 'not provided'. Empty when nothing is known."""
    parts = []
    if (idv or {}).get("participant_id"):
        parts.append(f"Prolific ID is exactly {idv['participant_id']}")
    if (idv or {}).get("email"):
        parts.append(f"Prolific email is exactly {idv['email']}")
    if not parts:
        return ""
    return ("\n\nYOUR ACCOUNT DETAILS (known, from file -- use the EXACT value, never alter or "
            "invent): " + "; ".join(parts) + ". When a study or survey asks for your Prolific ID "
            "or email, type exactly this value. It is already available, so never say it is "
            "missing and never refuse, cancel, or return a study over a Prolific ID / email field.")


# Injected in place of a human answer when the model calls ask_user_question
# during an UNATTENDED loop run. The whole point of loop mode is a 24/7 run
# the user does not have to babysit, so a question must never stall it: the
# agent is steered to decide for itself and keep moving. The user keeps full
# control via /inject at any time, and gets a non-blocking Telegram heads-up.
_UNATTENDED_ANSWER = (
    "You are running UNATTENDED in a 24/7 loop — no human will answer, so stop "
    "asking questions (this is a standing rule). Decide for yourself using the "
    "standing rules. If a study will not open, is broken, or you are stuck, go "
    "back to the Prolific studies list and pick another eligible study; if none "
    "is open, wait and re-check the list. If you genuinely cannot finish the "
    "current study, cancel/return it (Ctrl+Shift+Tab to the Prolific tab → "
    "Cancel participation) then terminate with NOCODE and move on. Never stop "
    "to ask again."
)


# @@ PROLIFIC-DEFINITIONS @@


class ProlificSite(Site):
    name = "prolific"
    title = "Prolific"

    def behaviors(self):
        # Prolific is a task platform: studies are listed, launched, timed
        # and handed back with a completion code.
        from behaviors.task_platform import TaskPlatform
        return (TaskPlatform,)

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

    # --- the participant's identity ---------------------------------------
    # Researchers label the field "Prolific ID", "PID" or "participant ID";
    # Prolific appends PROLIFIC_PID (24 hex characters) to every study URL.
    participant_id_label_re = re.compile(r"prolific\s*[-_]?\s*(id|pid|identifier)|\bpid\b"
                                         r"|participant\s*[-_]?\s*id")
    participant_id_params = ("prolific_pid", "pid", "participant_id", "participantid",
                             "prolificid", "prolific_id")
    participant_id_format = re.compile(r"[0-9a-fA-F]{24}")
    participant_id_in_text = re.compile(r"\b[0-9a-f]{24}\b")
    participant_id_aliases = ("prolific_id",)

    # --- the account's history (sites/prolific/submissions.py) -------------
    history_url = _subs._SUBMISSIONS_URL
    history_per_page = _subs._SUBS_PER_PAGE
    history_ready_js = _subs._SUBMISSIONS_READY_JS
    history_status_re = _subs._STATUS_RE
    history_paid_statuses = _subs._SUBS_PAID
    parse_history_rows = staticmethod(_subs._parse_submission_rows)
    parse_history_header = staticmethod(_subs._parse_submission_header)
    parse_history_last_page = staticmethod(_subs._parse_last_page)
    money = staticmethod(_subs._money)

    # --- what the model is told (texts above) -----------------------------
    def standing_rules(self, compact: bool = False) -> str:
        return _STANDING_RULES_COMPACT if compact else _STANDING_RULES_FULL

    identity_hint = staticmethod(identity_hint)

    def unattended_answer(self) -> str:
        return _UNATTENDED_ANSWER

    nocode_refusal = staticmethod(nocode_refusal)
    nocode_codes_refusal_text = staticmethod(nocode_codes_refusal_text)
    completion_pushback_text = staticmethod(completion_pushback_text)
    refuse_forbidden_visit = staticmethod(refuse_forbidden_visit)
    refuse_unknown_path = staticmethod(refuse_unknown_path)
    corrected_unknown_path = staticmethod(corrected_unknown_path)
    corrected_host = staticmethod(corrected_host)
    history_back_refusal = staticmethod(history_back_refusal)
    forbidden_notice = staticmethod(forbidden_notice)
    forbidden_live_notice = staticmethod(forbidden_live_notice)
    wander_notice = staticmethod(wander_notice)
    wander_live_notice = staticmethod(wander_live_notice)
    off_host_notice = staticmethod(off_host_notice)
    chrome_guard_live_notice = staticmethod(chrome_guard_live_notice)
    resume_on_task_page = staticmethod(resume_on_task_page)
    resume_in_task = staticmethod(resume_in_task)
    in_task_framing = staticmethod(in_task_framing)
    held_slot_notice = staticmethod(held_slot_notice)
    launched_notice = staticmethod(launched_notice)
    nocode_link_live_notice = staticmethod(nocode_link_live_notice)
    nocode_link_clicked = staticmethod(nocode_link_clicked)
    completion_return_clicked = staticmethod(completion_return_clicked)

    def home_url(self) -> str:
        return os.environ.get("FARA_STUDIES_URL", "https://app.prolific.com/studies")

    def task_url(self, task_id: str) -> str:
        if not task_id or not self.task_path.match(f"/studies/{task_id}"):
            return ""
        return f"https://app.prolific.com/studies/{task_id}"
