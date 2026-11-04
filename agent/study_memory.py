"""Cross-cycle study memory: remembers what worked on previous surveys.

When the model uses `run_js` to batch-complete repetitive trials on a survey
domain (e.g. boltai.com, qualtrics.com), this module remembers it.  On future
cycles, when the model enters a survey on a known domain, a hint is injected
so it can apply the efficient strategy immediately instead of discovering it
the hard way (50+ slow steps before someone notices).

Data lives in a small JSON file alongside study_cache.json, keyed by domain.
"""
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
MEMORY_FILE = Path(os.environ.get("FARA_STUDY_MEMORY",
                                   HERE / "study_memory.json"))
TTL_SECONDS = float(os.environ.get("FARA_STUDY_MEMORY_DAYS", "60")) * 86400
MAX_ENTRIES = 200
MAX_USER_HINTS_PER_DOMAIN = 5
MIN_USER_HINT_LEN = 20  # Filters out "yes"/"no"/"ok" answers.
# User hints age out faster than study entries overall: situational guidance
# ("cancel this one and go back") becomes stale and starts to be mis-applied
# to unrelated studies on the same domain. Keep them fresh.
USER_HINT_TTL_SECONDS = float(os.environ.get("FARA_USER_HINT_DAYS", "14")) * 86400

_lock = threading.Lock()


def _read() -> dict:
    try:
        data = json.loads(MEMORY_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write(data: dict) -> None:
    try:
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = MEMORY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, MEMORY_FILE)
    except Exception:
        pass


def _fresh(data: dict) -> dict:
    cutoff = time.time() - TTL_SECONDS
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and v.get("ts", 0) >= cutoff}


def domain_of(url: str) -> str:
    """Extract the survey domain from a URL, stripping common prefixes."""
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def record(url: str, *, run_js_used: bool = False, steps: int = 0,
           outcome: str = "") -> None:
    """Record what happened on a survey domain."""
    domain = domain_of(url)
    if not domain:
        return
    with _lock:
        data = _fresh(_read())
        prev = data.get(domain, {})
        entry = {
            "ts": time.time(),
            "seen": int(prev.get("seen", 0)) + 1,
            "outcome": outcome or prev.get("outcome", ""),
        }
        if run_js_used:
            entry["run_js_used"] = True
            entry["run_js_count"] = int(prev.get("run_js_count", 0)) + 1
        elif prev.get("run_js_used"):
            entry["run_js_used"] = True
            entry["run_js_count"] = int(prev.get("run_js_count", 0))
        if steps:
            entry["last_steps"] = steps
        # Preserve user hints across record() calls -- they live on the
        # domain, not on a single run.
        if prev.get("user_hints"):
            entry["user_hints"] = prev["user_hints"]
        if len(data) > MAX_ENTRIES:
            oldest = sorted(data.items(), key=lambda kv: kv[1].get("ts", 0))
            for k, _ in oldest[:len(data) - MAX_ENTRIES]:
                data.pop(k, None)
        data[domain] = entry
        _write(data)


def record_user_hint(url: str, text: str) -> None:
    """Store an instruction the user gave the agent mid-run on this domain.

    These are things like "the survey is in another tab, use Ctrl+Shift+Tab"
    or "scroll down to find the submit button" -- guidance the user had to
    inject because the model got stuck. On future runs on the same domain,
    hint_for() will surface them so the model remembers what the user asked
    for last time instead of needing a fresh correction.

    Trivial answers ("yes", "ok", "si") are filtered by MIN_USER_HINT_LEN
    so the hint stream stays signal, not noise.
    """
    text = (text or "").strip()
    if len(text) < MIN_USER_HINT_LEN:
        return
    domain = domain_of(url)
    if not domain:
        return
    with _lock:
        data = _fresh(_read())
        prev = data.get(domain, {"ts": time.time(), "seen": 0})
        hints = list(prev.get("user_hints") or [])
        # Dedupe: skip if we already stored a near-identical hint recently.
        if any(h.get("text", "").strip().lower() == text.lower() for h in hints):
            return
        hints.append({"ts": time.time(), "text": text[:400]})
        hints = hints[-MAX_USER_HINTS_PER_DOMAIN:]
        prev["user_hints"] = hints
        prev["ts"] = max(prev.get("ts", 0), time.time())
        data[domain] = prev
        _write(data)


def hint_for(url: str) -> str:
    """Return the run_js batch-completion hint for this domain, if any.

    Survey-specific: only meaningful while actually inside a survey, so the
    caller gates this on study_timer being active. User corrections are
    surfaced separately via user_hints_for(), which applies more broadly.
    """
    domain = domain_of(url)
    if not domain:
        return ""
    with _lock:
        data = _fresh(_read())
    entry = data.get(domain)
    if not entry or not entry.get("run_js_used"):
        return ""
    n = entry.get("run_js_count", 1)
    return (f'[Surveys on {domain} previously had many identical repeated trials '
            f'({n}x confirmed) — use run_js to batch-complete them when you see '
            f'the same select-and-submit pattern repeating.]\n')


def _fresh_hints(hints: list) -> list:
    """Drop hints older than USER_HINT_TTL_SECONDS from the list."""
    cutoff = time.time() - USER_HINT_TTL_SECONDS
    return [h for h in hints if float(h.get("ts", 0)) >= cutoff]


def user_hints_for(url: str) -> str:
    """Return the block of past user corrections for this domain, if any.

    These are instructions the user had to give the agent mid-run last time,
    scoped to a SPECIFIC past situation (e.g. "the survey is in another tab,
    use Ctrl+Shift+Tab" or "scroll down to find the submit button"). Framed
    carefully so the model treats them as situational guidance, not standing
    directives: an old hint like "cancel the study and go back" was given to
    resolve one stuck run, and if re-read as a blanket rule it makes the model
    cancel every future study on the domain (seen in run_20261103_194847,
    where the model hallucinated every Prolific study as 'ruled-out').

    Stale hints (> USER_HINT_TTL_SECONDS old) are dropped so situational
    guidance doesn't linger into contexts where it no longer applies.
    """
    domain = domain_of(url)
    if not domain:
        return ""
    with _lock:
        data = _fresh(_read())
    entry = data.get(domain)
    if not entry:
        return ""
    hints = _fresh_hints(entry.get("user_hints") or [])
    if not hints:
        return ""
    now = time.time()
    def _age(h):
        secs = max(0.0, now - float(h.get("ts", now)))
        days = int(secs // 86400)
        hours = int((secs % 86400) // 3600)
        if days: return f"{days}d ago"
        if hours: return f"{hours}h ago"
        return "recent"
    bullets = "\n".join(f"  - [{_age(h)}] {h.get('text', '')}"
                        for h in hints[-MAX_USER_HINTS_PER_DOMAIN:])
    block = (f'[Past situational guidance from the user on {domain} — each note '
             f'was given to resolve a SPECIFIC past problem and is NOT a standing '
             f'directive. Apply ONLY if the current page clearly matches that same '
             f'situation; otherwise ignore. Never use these as a reason to skip, '
             f'cancel, or label a study as "ruled out" unless the current screen '
             f'itself shows the problem the note was about:\n{bullets}]\n')
    # Imperative escalations: if the user has repeatedly asked for the same
    # pattern on this domain, prepend a stronger directive so the model actually
    # follows. Keyword sets are deliberately narrow (two hints with the same
    # intent must concur) to avoid firing on one-off remarks.
    low = [h.get("text", "").lower() for h in hints]
    enter_hits = sum(1 for t in low if any(k in t for k in _ENTER_KW))
    speed_hits = sum(1 for t in low if any(k in t for k in _SPEED_KW))
    # Order: later prepends sit on TOP. We put the speed DIRECTIVE on the
    # bottom (prepended first) and the more specific Enter DIRECTIVE on top
    # (prepended last) -- when both fire the Enter one must dominate.
    # When Enter is active, the speed DIRECTIVE drops its 'scroll to submit →
    # click submit' rhythm (which contradicts Enter) and adopts the Enter
    # rhythm instead, so the two no longer tell the model opposite things.
    if speed_hits >= 2:
        rhythm = ("click an option → press Enter → wait for next trial → repeat"
                  if enter_hits >= 1 else
                  "select → scroll to submit → click submit → wait for next "
                  "trial → repeat")
        block = (f'[DIRECTIVE on {domain}: user has asked {speed_hits} times '
                 f'to work faster/more efficiently here. On repetitive '
                 f'select+submit surveys, develop a fast clicking rhythm: '
                 f'{rhythm}. Do NOT hesitate between trials. Vary '
                 f'your selections naturally — never straight-line.]\n' + block)
    if enter_hits >= 1:
        times = ("once" if enter_hits == 1
                 else "twice" if enter_hits == 2
                 else f"{enter_hits} times")
        block = (f'[DIRECTIVE on {domain}: the user has told you {times} '
                 f'that after selecting an option on this survey you '
                 f'MUST press Enter to proceed to the next trial. '
                 f'Do NOT scroll looking for a submit button. Do NOT click '
                 f'submit. IMPORTANT: the harness ALREADY presses Enter for '
                 f'you right after each left_click on this domain, you must '
                 f'NOT emit a key action with Return/Tab/Enter yourself '
                 f'(pressing Enter twice advances two trials and submits the '
                 f'next one with no selection). After left_click, your very '
                 f'next action is to take the next screenshot (wait or the '
                 f'next click on the new trial), never a key action.]\n' + block)
    return block


# Keyword sets exposed at module level so other modules (e.g. agent_core,
# which auto-chains Enter after a click when the Enter DIRECTIVE is active)
# and tests can reuse the exact same list that gates the DIRECTIVE text.
_ENTER_KW = ("press enter", "press the enter", "just press", "just enter",
             "hit enter", "use enter", "enter key", "enter to",
             "click enter", "enter on the keyboard", "enter on keyboard",
             "keyboard to submit", "with enter", "submit with enter",
             "no submit", "no scroll", "don't scroll", "dont scroll",
             "don't click submit", "dont click submit", "no need to scroll",
             "no need to click", "reach submit", "clicking submit")
_SPEED_KW = ("veloce", "velocement", "fast", "faster", "quickly",
             "speed", "efficien", "ritmo", "rhythm")


def enter_hint_count(url: str) -> int:
    """How many Enter-rhythm user hints are stored for this URL's domain.

    Agent-core consults this to decide whether to auto-chain an Enter press
    after a left_click: on domains where the user has repeatedly asked for
    the click-then-Enter pattern, we skip the extra model round-trip and
    press Enter ourselves. 0 when the domain or its hint list is empty.
    Stale hints are dropped (same TTL as user_hints_for) so a one-time
    Enter instruction from months ago doesn't keep auto-pressing today.
    """
    domain = domain_of(url)
    if not domain:
        return 0
    with _lock:
        data = _fresh(_read())
    entry = data.get(domain) or {}
    hints = _fresh_hints(entry.get("user_hints") or [])
    low = [h.get("text", "").lower() for h in hints]
    return sum(1 for t in low if any(k in t for k in _ENTER_KW))


def entries() -> dict:
    with _lock:
        return _fresh(_read())
