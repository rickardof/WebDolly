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


def user_hints_for(url: str) -> str:
    """Return the block of past user corrections for this domain, if any.

    These are instructions the user had to give the agent mid-run last time
    (e.g. "click the send arrow in the small chat window to reveal the answer
    fields"). Surfaced on every step on the domain -- including Prolific study
    pages -- so a correction given once is not forgotten, within a run (it
    survives context trimming because it is re-injected fresh each step) or
    across runs on a similar study.
    """
    domain = domain_of(url)
    if not domain:
        return ""
    with _lock:
        data = _fresh(_read())
    entry = data.get(domain)
    if not entry:
        return ""
    hints = entry.get("user_hints") or []
    if not hints:
        return ""
    bullets = "\n".join(f"  - {h.get('text', '')}" for h in hints[-MAX_USER_HINTS_PER_DOMAIN:])
    block = (f'[Things the user told you before on {domain} (apply when relevant, '
             f'do not repeat past mistakes):\n{bullets}]\n')
    # Imperative escalation: Fara tends to call run_js once and then revert
    # to visual clicking. If the user has had to repeat the JS request, the
    # passive bullet list isn't enough — prepend a direct order.
    js_kw = ("js", "javascript", "script", "run_js", "veloce", "velocement",
             "fast", "faster", "batch", "loop")
    js_hits = sum(1 for h in hints
                  if any(k in h.get("text", "").lower() for k in js_kw))
    if js_hits >= 2:
        block = (f'[DIRECTIVE on {domain}: user has asked {js_hits} times to '
                 f'use run_js batch loops here. On any repetitive '
                 f'select+submit survey, write ONE run_js that iterates '
                 f'(while/for) over trials: find next select button → click '
                 f'→ await humanDelay() → click submit → await humanDelay() '
                 f'→ wait for next trial to load → repeat until no more '
                 f'trials. Do NOT revert to visual clicking after a single '
                 f'successful run_js call.]\n' + block)
    return block


def entries() -> dict:
    with _lock:
        return _fresh(_read())
