"""Fara-1.5 browser-agent loop, as a resumable generator.

Shared by controller.py (CLI) and gui_server.py (interactive GUI). Runs on
the HOST. Talks to:
  - llama-server (Fara1.5-27B) over HTTP at FARA_URL
  - the isolated `fara-browser` container via `docker exec`
    (scrot for screenshots, xdotool for pointer/keyboard, cdp.py for
    navigation actions a kiosk-mode browser has no address bar for)

run_agent() yields event dicts as the loop progresses:
  {"type": "step", "step": n, "thoughts": ..., "action_type": ..., "args": ...,
   "result_desc": ..., "screenshot": "<path>"}
  {"type": "question", "step": n, "thoughts": ..., "question": "..."}
      -- caller must resume with gen.send(answer_text); sending None aborts
         the run (treated as a stop request).
  {"type": "done", "answer": "..."}
  {"type": "error", "message": "..."}

This mirrors the official fara15_agent.py `pending_user_response` pattern:
when the model calls ask_user_question, the *next* round's user-turn text is
the human's answer instead of the generic "next screenshot" prompt.
"""

import ast
import base64
import io
import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse
import urllib.request

from PIL import Image

import fara_prompt as fp
import cancel
import form_fill_pw as form_fill
import os
import re
import pw_bridge

FARA_URL = os.environ.get("FARA_URL", "http://127.0.0.1:8090/v1/chat/completions")

def _notify_telegram(msg: str) -> None:
    """Fire-and-forget Telegram notification. Fails silently."""
    try:
        import telegram_config as _tg
        url = f"https://api.telegram.org/bot{_tg.TELEGRAM_TOKEN}/sendMessage"
        payload = json.dumps({"chat_id": _tg.TELEGRAM_CHAT_ID, "text": msg}).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass
CONTAINER = os.environ.get("FARA_CONTAINER", "fara-browser-pw")
DATA_DIR = Path(os.environ.get("FARA_DATA_DIR", Path.home() / "web-agent" / "data_pw"))
LOG_ROOT = Path(os.environ.get("FARA_LOG_ROOT", Path.home() / "web-agent" / "logs_pw"))

# Optional observer for the GUI: called with small dicts describing what the
# agent is doing right now ({"type": "phase"|"token"|"target", ...}). These
# are ephemeral, not part of the event protocol below -- they only exist so a
# watcher sees the agent think and act instead of a 3-second silence per
# step. A missing or failing hook never affects the run.
live_hook = None


def _live(msg: dict):
    h = live_hook
    if h is not None:
        try:
            h(msg)
        except Exception:
            pass


# Each run writes one screenshot per step under LOG_ROOT/run_*/. For a
# 24/7 polling loop this grows without bound unless pruned -- keep only the
# most recent MAX_RUN_DIRS run directories, oldest deleted first.
MAX_RUN_DIRS = 200


def _prune_old_runs(keep: int = MAX_RUN_DIRS):
    run_dirs = sorted(
        (p for p in LOG_ROOT.glob("run_*") if p.is_dir()),
        key=lambda p: p.name,
    )
    for old in run_dirs[:-keep] if keep > 0 else run_dirs:
        shutil.rmtree(old, ignore_errors=True)

# Fara "keys" names -> xdotool/X11 keysym names.
KEY_MAP = {
    "Enter": "Return", "Alt": "alt", "Shift": "shift", "Tab": "Tab",
    "Control": "ctrl", "Backspace": "BackSpace", "Delete": "Delete",
    "Escape": "Escape", "ArrowUp": "Up", "ArrowDown": "Down",
    "ArrowLeft": "Left", "ArrowRight": "Right", "PageDown": "Next",
    "PageUp": "Prior",
}

# Only these make a key list a simultaneous chord ("ctrl+Tab"); any other
# list is a sequence of independent presses -- see dispatch_action's "key".
MODIFIER_KEYSYMS = {"ctrl", "alt", "shift", "super", "meta"}


def docker_exec(*args):
    # The container's default locale is POSIX/C, under which xdotool can't
    # interpret multi-byte UTF-8 (e.g. any accented character) and aborts
    # entirely with "Invalid multi-byte sequence encountered" -- seen in
    # production typing an Italian answer containing e.g. "perché". Force
    # a UTF-8 locale on every exec; harmless for commands that don't care
    # (scrot, cdp.py).
    return subprocess.run(
        ["docker", "exec", "-e", "LANG=C.utf8", "-e", "LC_ALL=C.utf8", CONTAINER, *args],
        capture_output=True, text=True, check=True,
    )


def take_screenshot(run_dir: Path, step: int) -> Image.Image:
    # _agent_live.png is distinct from the GUI's own _gui_live.png so the
    # two concurrent scrots never clobber the same file.
    docker_exec("scrot", "-o", "/data/_agent_live.png")
    img = Image.open(DATA_DIR / "_agent_live.png").convert("RGB")
    img.save(run_dir / f"screenshot_{step:02d}.png")
    return img


def encode_for_model(img: Image.Image) -> str:
    rh, rw = fp.smart_resize(img.height, img.width)
    buf = io.BytesIO()
    img.resize((rw, rh)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# Whether the chat template opens a <think> block for the model to reason in.
# Measured per model, not assumed: on the 27B it makes no difference (same
# tokens, same action -- Fara's short narrated "thoughts" come from its
# fine-tuning, not from this switch). On the 9B it is decisive: with the
# default (thinking on) it answered 0/6 clicks, always a bogus mouse_move
# ("I will move the cursor to the drag start point") 80-280px off target;
# with thinking off, 5/6 correct clicks. Set FARA_ENABLE_THINKING=1 to
# restore the template default.
ENABLE_THINKING = os.environ.get("FARA_ENABLE_THINKING", "0") == "1"


def call_fara(messages: list, step: int = None) -> str:
    payload = {
        "model": "Fara1.5-27B-Q4_K_M.gguf",
        "messages": messages,
        "temperature": 0,
        "max_tokens": 512,
        "chat_template_kwargs": {"enable_thinking": ENABLE_THINKING},
    }
    result = cancel.post_chat(FARA_URL, payload, timeout=120,
                              on_delta=lambda t: _live({"type": "token", "step": step, "text": t}))
    return result["choices"][0]["message"]["content"]


def parse_thoughts_and_action(message: str):
    """Port of Fara15Agent._parse_thoughts_and_action."""
    tmp = message.split("<tool_call>\n")
    if len(tmp) < 2:
        raise ValueError(f"No <tool_call> block in model output: {message!r}")
    thoughts = tmp[0].strip()
    action_text = tmp[1].split("\n</tool_call>")[0]
    try:
        action = json.loads(action_text)
    except json.JSONDecodeError:
        action = ast.literal_eval(action_text)
    return thoughts, action


# llama-server's prompt cache reuses the KV state of whatever prefix of the
# conversation is byte-identical to a previous call. Mutating an old turn
# (e.g. stripping its image) breaks that match for everything after it, so
# doing it every single step forces a full reprocess of the growing
# history each time -- this is what made steps get progressively slower in
# a long run (measured: prompt-eval tokens climbing step over step).
#
# Instead, screenshots are left untouched (pure append, cache stays valid)
# until MAX_IMAGES_IN_CONTEXT is exceeded, then a whole batch of the oldest
# ones is dropped at once, down to TRIM_TO_IMAGES. This trades a few
# expensive steps (every ~(MAX_IMAGES_IN_CONTEXT - TRIM_TO_IMAGES) steps)
# for every other step being a cheap, mostly-cached prompt.
MAX_IMAGES_IN_CONTEXT = 10
TRIM_TO_IMAGES = 5

# Hard safety net on top of the batch trim above. Measured in production,
# three times, each a variant of the same class of bug: llama-server
# rejects a request outright once it exceeds its 32768-token context
# ("request (34767 tokens) exceeds the available context size"). Two
# heuristic-based attempts to predict this in advance both failed:
#   1. A char-count estimate (chars//4 for text, a flat guess per image)
#      undercounted Fara's own <think> prose and non-English task text --
#      re-measured per-image cost was actually fine (1262 real tokens at
#      this viewport vs. 1500 assumed), so the TEXT side was the problem.
#   2. Tracking the server's own usage.total_tokens as ground truth (no
#      text estimate) fixed the within-cycle case, but still missed a
#      second failure mode: with a short poll_interval, a loop's step 1
#      re-injects the full task/context text (one preset measured at
#      ~2000 real tokens) on EVERY cycle, and only images were ever
#      trimmed -- repeated copies of that text across ~14 quick cycles
#      alone pushed the real total over budget even with zero images left
#      to drop, something no amount of image trimming can fix.
#
# Given estimates and partial fixes have each failed in a new way, this
# replaces them with a direct, authoritative measurement before every
# single request (not just at cycle boundaries): llama-server's own
# /tokenize endpoint for the real cost of all TEXT content (it doesn't
# accept images, so image cost uses the empirically measured per-image
# constant, which IS exact for a fixed viewport -- deterministic patch
# count, content-independent). Calibrated against real usage.prompt_tokens
# on a multi-turn conversation: off by only ~2.5 tokens/message (chat
# template boundaries), comfortably inside PER_MESSAGE_OVERHEAD_MARGIN.
# /tokenize is cheap (~26ms for a 32k-char conversation), so calling it
# every step is not a performance concern. If the real count is still over
# budget after the usual image trim, oldest whole messages (text and all)
# are dropped next -- the only fix that can address bloat coming from
# repeated large text, not just images.
CONTEXT_TOKEN_BUDGET = 28000
MEASURED_IMAGE_TOKENS = 1262
PER_MESSAGE_OVERHEAD_MARGIN = 20
TOKENIZE_URL = FARA_URL.rsplit("/v1/", 1)[0] + "/tokenize"


def _tokenize_count(text: str) -> int:
    if not text:
        return 0
    req = urllib.request.Request(
        TOKENIZE_URL, data=json.dumps({"content": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return len(json.load(r)["tokens"])


def _message_text(m: dict) -> str:
    content = m["content"]
    if isinstance(content, str):
        return content
    return "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")


def _message_has_image(m: dict) -> bool:
    content = m["content"]
    return isinstance(content, list) and any(c.get("type") == "image_url" for c in content)


def _real_token_count(messages: list) -> int:
    """Authoritative (not estimated) token count for the full message list,
    as it would actually be sent to the model right now."""
    combined_text = "\n".join(_message_text(m) for m in messages)
    text_tokens = _tokenize_count(combined_text)
    image_count = sum(1 for m in messages if _message_has_image(m))
    return (text_tokens + image_count * MEASURED_IMAGE_TOKENS
            + len(messages) * PER_MESSAGE_OVERHEAD_MARGIN)


def _image_turn_idxs(messages: list) -> list:
    return [
        i for i, m in enumerate(messages)
        if m["role"] == "user" and isinstance(m["content"], list)
        and any(c.get("type") == "image_url" for c in m["content"])
    ]


def _trim_to_budget(messages: list, budget: int = CONTEXT_TOKEN_BUDGET,
                     max_images: int = MAX_IMAGES_IN_CONTEXT,
                     trim_to: int = TRIM_TO_IMAGES) -> int:
    """Brings `messages` under `budget`, verified against the real
    tokenizer at every stage rather than assumed. Returns the real token
    count after trimming (0 trims performed if already under budget)."""
    image_idxs = _image_turn_idxs(messages)
    if len(image_idxs) > max_images:
        keep_from = len(image_idxs) - trim_to
        for i in image_idxs[:keep_from]:
            messages[i]["content"] = [c for c in messages[i]["content"] if c.get("type") != "image_url"]

    current = _real_token_count(messages)
    if current <= budget:
        return current

    # Safety net: the batch trim above caps image *count*, not token cost.
    # Drop the oldest remaining images one at a time first (cheap, keeps
    # the most recent visual context as long as possible).
    image_idxs = _image_turn_idxs(messages)
    for i in image_idxs:
        if current <= budget:
            break
        messages[i]["content"] = [c for c in messages[i]["content"] if c.get("type") != "image_url"]
        current -= MEASURED_IMAGE_TOKENS

    # Still over budget: the bloat is coming from text, not images (e.g. a
    # large task prompt re-injected on step 1 of every cycle, repeated
    # across many quick cycles). Only removing whole old messages can fix
    # this. Never drop index 0 (system prompt).
    while current > budget and len(messages) > 2:
        del messages[1]
        current = _real_token_count(messages)

    return current


def get_current_url() -> str:
    # Through Playwright: the URL of the tab actually on screen, not of
    # whatever tab cdp.py happened to list first.
    try:
        return pw_bridge.current_url()
    except Exception:
        return ""


# Sections of the site the agent must never be in. The agent's whole job is to
# take part in studies and, when none fits, wait for new ones; the profile
# ("About you") questionnaire is not part of that, whatever the page nudges
# it towards. Enforced here in code -- every step, every visit_url, every
# cycle restart -- not left to the model's judgement or to prompt text.
_FORBIDDEN_PATH = re.compile(r"^/(about-you|logout|sign-out|signout)(/|$)", re.IGNORECASE)


# Only on the site that has such a section: a third-party survey that happens
# to have an /about-you page must not be hijacked.
_GUARDED_HOSTS = tuple(h.strip().lower() for h in
                       os.environ.get("FARA_GUARDED_HOSTS", "prolific.com,prolific.co").split(",") if h.strip())


def _on_guarded_site(url: str) -> bool:
    try:
        host = (urlparse(url or "").hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in _GUARDED_HOSTS)
    except Exception:
        return False


def is_forbidden_url(url: str) -> bool:
    try:
        return _on_guarded_site(url) and bool(_FORBIDDEN_PATH.match(urlparse(url).path or ""))
    except Exception:
        return False


_LIST_PATH = re.compile(r"^/studies/?$", re.IGNORECASE)


class WanderGuard:
    """Brings the agent back to the list of studies if it lingers elsewhere.

    The clock runs while the active tab is on the guarded site (Prolific) but
    NOT on the studies list: study detail pages, account pages, results...
    It resets whenever the agent is on the list, or on any other site -- an
    external survey tab means it is actually doing a study, and that must
    never be cut short. After `seconds` outside the list, check() returns
    True once and the caller sends it back to /studies."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self.seconds, self.clock, self.since = float(seconds or 0), clock, None

    def check(self, url: str) -> bool:
        if self.seconds <= 0:
            return False
        if not _on_guarded_site(url) or _LIST_PATH.match(urlparse(url).path or ""):
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


def safe_page_for(url: str) -> str:
    """The list of studies on the same site as `url`."""
    pr = urlparse(url)
    return f"{pr.scheme}://{pr.netloc}/studies"


def _extract_study_duration(x: float | None = None, y: float | None = None) -> float | None:
    """Return the estimated completion time in minutes from the current page.

    When x/y viewport coordinates are provided, walks up the DOM from the
    element at that point so we read only the study card being clicked —
    not durations of other study cards visible on the same page.
    """
    try:
        if x is not None and y is not None:
            text = pw_bridge.evaluate(f"""
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
        else:
            text = pw_bridge.evaluate("document.body.innerText") or ""
        m = re.search(r"(\d+)\s*(?:minute|min)s?", text, re.IGNORECASE)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


class StudyTimer:
    """Tracks minimum completion time for the current study.

    Started when the agent leaves the Prolific study detail page toward an
    external survey. The duration is read from the Prolific page before the
    click. If `min_seconds` has not elapsed when the agent tries to submit,
    the status_line injected into the agent's turn tells it to wait.
    """

    def __init__(self):
        self.start_time: float | None = None
        self.min_seconds: float = 0

    def start(self, min_seconds: float):
        self.start_time = time.monotonic()
        self.min_seconds = float(min_seconds)

    def reset(self):
        self.start_time = None
        self.min_seconds = 0

    def elapsed(self) -> float:
        if self.start_time is None:
            return 0.0
        return time.monotonic() - self.start_time

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
            return (f"Study timer: {self.elapsed():.0f}s elapsed / "
                    f"{self.min_seconds:.0f}s minimum — OK to submit now.")
        return (f"Study timer: {self.elapsed():.0f}s elapsed, "
                f"{rem:.0f}s still needed (minimum {self.min_seconds:.0f}s). "
                "Do NOT submit yet — use the `wait` action on the last page.")


def dispatch_action(action_type: str, args: dict) -> str:
    if action_type == "left_click":
        x, y = args["coordinate"]
        docker_exec("xdotool", "mousemove", str(round(x)), str(round(y)), "click", "1")
        return f"left_click at ({x:.0f},{y:.0f})"

    if action_type == "double_click":
        x, y = args["coordinate"]
        docker_exec("xdotool", "mousemove", str(round(x)), str(round(y)), "click", "--repeat", "2", "1")
        return f"double_click at ({x:.0f},{y:.0f})"

    if action_type == "right_click":
        x, y = args["coordinate"]
        docker_exec("xdotool", "mousemove", str(round(x)), str(round(y)), "click", "3")
        return f"right_click at ({x:.0f},{y:.0f})"

    if action_type == "triple_click":
        x, y = args["coordinate"]
        docker_exec("xdotool", "mousemove", str(round(x)), str(round(y)), "click", "--repeat", "3", "1")
        return f"triple_click at ({x:.0f},{y:.0f})"

    if action_type == "mouse_move":
        x, y = args["coordinate"]
        docker_exec("xdotool", "mousemove", str(round(x)), str(round(y)))
        return f"mouse_move to ({x:.0f},{y:.0f})"

    if action_type == "left_click_drag":
        x, y = args["coordinate"]
        docker_exec("xdotool", "mousedown", "1")
        docker_exec("xdotool", "mousemove", str(round(x)), str(round(y)))
        docker_exec("xdotool", "mouseup", "1")
        return f"left_click_drag to ({x:.0f},{y:.0f})"

    if action_type == "type":
        text = str(args.get("text", ""))
        docker_exec("xdotool", "type", "--clearmodifiers", text)
        return f"type ({len(text)} chars)"

    if action_type == "key":
        keys = args.get("keys", [])
        mapped = [KEY_MAP.get(k, k) for k in keys]
        # "a+b" means press a and b TOGETHER (a chord, e.g. ctrl+Tab);
        # separate args mean press them in SEQUENCE. Joining everything
        # with "+" broke the moment the model asked for repeated keys --
        # observed in production as 10x ArrowDown then Enter to walk an
        # open dropdown, which became the impossible chord
        # "Down+Down+...+Return" and aborted xdotool (exit 134), killing
        # the step. Only treat it as a chord when a real modifier is
        # present; otherwise send the keys as a sequence.
        # Tab switching goes through Playwright (bring_to_front) so the tab
        # on screen and the tab DOM operations target can never diverge.
        lowered = [m.lower() for m in mapped]
        if lowered in (["ctrl", "tab"], ["ctrl", "next"]):
            return f"switch tab -> {pw_bridge.switch_tab(+1)}"
        if lowered in (["ctrl", "shift", "tab"], ["ctrl", "prior"]):
            return f"switch tab -> {pw_bridge.switch_tab(-1)}"
        if any(m.lower() in MODIFIER_KEYSYMS for m in mapped):
            docker_exec("xdotool", "key", "+".join(mapped))
            return f"key {'+'.join(mapped)}"
        docker_exec("xdotool", "key", *mapped)
        return f"key {' '.join(mapped)}"

    if action_type == "scroll":
        pixels = fp.scale_scroll_pixels(args.get("pixels", 0), fp.VIEWPORT_HEIGHT)
        ticks = max(1, abs(pixels) // 100)
        docker_exec("xdotool", "click", "--repeat", str(ticks), "4" if pixels > 0 else "5")
        return f"scroll {'up' if pixels > 0 else 'down'} ({ticks} ticks)"

    if action_type == "hscroll":
        pixels = fp.scale_scroll_pixels(args.get("pixels", 0), fp.VIEWPORT_WIDTH)
        ticks = max(1, abs(pixels) // 100)
        docker_exec("xdotool", "click", "--repeat", str(ticks), "6" if pixels < 0 else "7")
        return f"hscroll ({ticks} ticks)"

    if action_type == "visit_url":
        url = args.get("url", "")
        if url.startswith(("https://", "http://", "file://", "about:")):
            target = url
        elif " " in url:
            target = f"https://www.bing.com/search?q={quote_plus(url)}&FORM=QBLH"
        else:
            target = "https://" + url
        if is_forbidden_url(target):
            return ("visit_url REFUSED: that page (About you / logout) is off limits. Your only job is to "
                    "take part in studies, or wait on the studies list for new ones.")
        pw_bridge.goto(target)
        return f"visit_url {target}"

    if action_type == "history_back":
        if not pw_bridge.go_back():
            return ("history_back REFUSED: going back would leave this site (you would "
                    "be logged out). Use the page's own navigation instead, e.g. the "
                    "Studies tab in the top bar.")
        return "history_back"

    if action_type == "web_search":
        query = args.get("query", "")
        target = f"https://www.bing.com/search?q={quote_plus(query)}&FORM=QBLH"
        pw_bridge.goto(target)
        return f"web_search {query!r}"

    if action_type == "wait":
        duration = float(args.get("time", 3))
        cancel.sleep(duration)
        return f"wait {duration}s"

    if action_type in ("pause_and_memorize_fact", "read_page_answer_question"):
        return f"{action_type} (not implemented, no-op)"

    if action_type == "terminate":
        return args.get("answer", "")

    raise ValueError(f"Unknown action: {action_type}")


def _maybe_batch_fill(task: str, attempted_signatures: set, memory: list = None):
    """Checks the current page for a batch-fillable form and, if found and
    not already attempted, runs form_fill's extract/decide/apply/verify
    cycle. Returns (step_event, next_turn_text, diag): the first two are
    None if nothing was done (no form found, already attempted, or an
    error occurred -- in which case the caller falls back to a normal Fara
    vision step); `diag` is always a short string explaining what
    happened, written to the transcript (not the GUI feed, to avoid
    clutter) so a silent non-trigger -- e.g. a page with custom-styled
    choice widgets with no native <input> elements to find, or genuinely
    too few fields -- can be diagnosed after the fact instead of guessed.
    """
    try:
        fields = form_fill.extract_fields()
        should_fill, verdict = form_fill.batch_fill_verdict(fields)
    except Exception as e:
        return None, None, f"extraction error: {e}"
    if not should_fill and not (any(form_fill._identity_kind(f) and not f.get("answered")
                                    for f in fields)):
        return None, None, verdict

    url = get_current_url()
    # Question text is part of the signature: Qualtrics keeps one URL for
    # the whole survey and reuses positional ids (fs:0:radio) on every
    # page, so ids+URL alone made every later page look "already tried".
    signature = (url, tuple(sorted((f["id"], f.get("text", "")) for f in fields)))
    if signature in attempted_signatures:
        return None, None, f"already attempted this page/field-set ({len(fields)} fields)"
    attempted_signatures.add(signature)

    _live({"type": "phase", "phase": "form", "fields": len(fields)})
    try:
        result = form_fill.run_batch_fill(persona=task, memory=memory)
    except Exception as e:
        result = {"skipped": True, "reason": f"error: {e}"}
    summary = form_fill.summarize_for_agent(result)

    # Having just filled the page from the DOM, advancing it from the DOM
    # too is both faster and more reliable than handing control back to
    # vision: measured, locating "Next" by sight cost 4-5 screenshot ->
    # model -> scroll round-trips (~39s of a 60s task) because the button
    # sits below the fold. element.click() ignores scroll position.
    # Deliberately only attempted after a CLEAN fill -- if anything didn't
    # apply, the page needs eyes on it, not a blind submit.
    clean = (not result.get("skipped")
             and not result.get("mismatches")
             and not result.get("apply_errors")
             and not result.get("undecided"))
    advance = None
    if clean:
        try:
            advance = form_fill.click_advance_button()
        except Exception as e:
            advance = {"attempted": False, "reason": f"error: {e}"}
        result["advance"] = advance

    if advance and advance.get("advanced"):
        summary += f" Then clicked '{advance['label']}' and the page advanced."
        next_text = (
            f"{summary} Take a screenshot and continue from the new page "
            "state."
        )
    else:
        if advance and advance.get("attempted"):
            appeared = advance.get("new_text") or []
            if appeared:
                seen = "; ".join(f"'{x}'" for x in appeared)
                next_text = (
                    f"{summary} Then clicked '{advance['label']}'. The page did not navigate "
                    f"away, but this new text appeared: {seen}. Read it on the screenshot: "
                    "if it confirms the submission, the task on this page is done; if it is "
                    "an error, fix what it asks for. Do not just click the same button again."
                )
            else:
                next_text = (
                    f"{summary} Then clicked '{advance['label']}', but nothing on the page "
                    "changed. Check the screenshot for an unanswered required question or "
                    "another control to press."
                )
        else:
            why = (f" (no forward button found in the DOM: {advance.get('reason')})"
                   if advance else "")
            next_text = (
                f"{summary}{why} Take a screenshot and continue: scroll down if more "
                "questions remain on this page, otherwise find and click the "
                "button to proceed (e.g. Next/Continue/Submit)."
            )

    step_event = {
        "type": "batch_fill",
        "thoughts": "Detected a multi-question form; filled it via DOM extraction instead of per-field clicking.",
        "result": result,
        "result_desc": summary,
    }
    return step_event, next_text, summary


# Appended to every run's task, whatever preset or text the user started
# with. Kept short and placed last-but-prominent: rules buried deep in a
# long preset were measured to be ignored (pay rate considered in 1 of 436
# study-list steps when it was rule #12 of a ~2500-token preset).
STANDING_RULES = (
    "\n\nSTANDING RULES (always apply):\n"
    "1. Choosing a study: compare ALL study cards first; among the eligible ones open the "
    "one with the highest reward rate per hour (the £/hr or $/hr shown), not just the first.\n"
    "2. NO AUDIO, EVER: a study that involves audio in ANY form is not eligible and must never be "
    "opened or started -- listening to sounds, speech, music or recordings, watching videos with "
    "sound, 'audio playback', headphones or speakers required, or an 'Audio' icon/label on its "
    "card. A study that needs no microphone or camera is STILL excluded if it needs listening. "
    "Before pressing 'Take part in this study', read its requirements section; if it mentions "
    "any of this, do not take part and treat the study as not available. Do not argue that "
    "listening-only is acceptable: it is not.\n"
    "3. Answer quality: read every question and its instructions fully; if it says which "
    "option to pick (attention check), pick exactly that. Keep every answer consistent with "
    "the persona and with your earlier answers in the same survey. Write specific, relevant "
    "open-ended answers in the survey's language, never generic filler.\n"
    "4. Scope: your only job is to take part in studies that meet the criteria and, when none "
    "is available, to wait and re-check the list of studies for new ones. Never open the "
    "'About you' section of the site or answer profile questionnaires outside a study.\n"
    "4b. After finishing a survey: ALWAYS switch back to the Prolific tab before calling "
    "terminate. On the Prolific tab: (a) click 'I'm finished'; (b) if a completion-code field "
    "appears, enter the code you received, or if no code was given click 'I didn't receive a "
    "completion code' (or type NOCODE in the field if that link is absent); (c) confirm/submit. "
    "Only after this Prolific-side step call terminate with CODE:<code> or NOCODE.\n"
    "5. Minimum study time: the estimated completion time shown on the study page is a minimum "
    "you must respect. A 'Study timer' line will appear in your status once you enter a study. "
    "If you reach what looks like the final submit/finish page and the timer says time remains, "
    "use the `wait` action to wait (e.g. wait 30s at a time), then check the status line again. "
    "Submit only once the timer says 'OK to submit now'.\n"
    "6. Screener questions: before and during a study, read the description and requirements "
    "carefully to understand what profile the researcher is looking for. When answering screener "
    "questions, emphasise whichever TRUE aspects of the persona best match those criteria — the "
    "goal is to avoid being excluded for a trivially wrong answer when the persona genuinely "
    "qualifies. You may present a truth selectively or adapt phrasing to fit the screener "
    "(e.g. 'I use AI tools every day' if the persona does). You must NEVER invent facts that "
    "directly contradict the given persona (age, nationality, profession, stated experience), "
    "and never claim qualifications the persona does not have."
)


def run_agent(task: str, start_url: str, max_rounds: int, run_dir: Path = None,
              stop_event=None, messages: list = None, batch_fill: bool = False,
              wander_seconds: float = 0):
    """Resumable agent loop. See module docstring for the event protocol.

    `messages` lets a caller continue the same conversation across several
    calls (e.g. successive loop cycles) instead of starting fresh each time
    -- pass None for a brand-new conversation. Every yielded event includes
    the live `messages` list so the caller can thread it into the next
    call. Before the first request of a continuing cycle and after every
    response, `messages` is measured against the real tokenizer and
    trimmed if needed (see CONTEXT_TOKEN_BUDGET comment) -- a carried-over
    history that's already over budget is caught proactively, not only
    after a request gets rejected.

    `batch_fill`, when true, checks before every normal vision step whether
    the current page looks like a multi-question form (>= form_fill's field
    threshold) and if so fills it in one DOM extract/decide/apply/verify
    pass (see form_fill.py) instead of letting Fara click through each
    field individually. This is a stateless side-call -- it never enters
    Fara's own message history -- only a short text summary of what was
    filled is fed back in as the next turn's observation, the same channel
    used for injected sub-tasks. Each distinct page (URL + field-id set) is
    only attempted once, so a page Fara revisits falls back to normal
    vision-driven interaction.
    """
    _prune_old_runs()
    if run_dir is None:
        run_dir = LOG_ROOT / datetime.now().strftime("run_%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    transcript = (run_dir / "transcript.jsonl").open("w")

    try:
        if start_url:
            pw_bridge.goto(start_url)
            pw_bridge.wait_for_content()

        if messages is None:
            messages = [{"role": "system", "content": fp.SYSTEM_PROMPT}]
        else:
            # Carried over from a prior cycle -- a short poll_interval can
            # mean many quick cycles, each re-injecting a large task/context
            # prompt on its own step 1; verify for real and trim before
            # this cycle's first request, not just after.
            _trim_to_budget(messages)
        pending_user_response = ""
        attempted_form_signatures = set()
        survey_memory = []  # (question, answer) given so far in this run
        wander = WanderGuard(wander_seconds)
        study_timer = StudyTimer()

        for step in range(1, max_rounds + 1):
            if stop_event is not None and stop_event.is_set():
                yield {"type": "done", "answer": "STOPPED", "messages": messages}
                return

            here = get_current_url()
            if is_forbidden_url(here):
                back = safe_page_for(here)
                pw_bridge.goto(back)
                pw_bridge.wait_for_content()
                pending_user_response = (
                    "You ended up in the 'About you' section, which is off limits, so you were "
                    "taken back to the list of studies. Never open it. Your only job is to take "
                    "part in studies that meet the criteria and, if none is available, to wait "
                    "and check the list again for new ones.")
                yield {"type": "notice",
                       "text": "Aperta la sezione «About you»: riportato all'elenco degli studi.",
                       "messages": messages}
            elif wander.check(here):
                pw_bridge.goto(safe_page_for(here))
                pw_bridge.wait_for_content()
                n = int(wander.seconds)
                pending_user_response = (
                    f"You spent more than {n} seconds away from the list of studies without taking "
                    "part in a study, so you were taken back to it. Stay on the list of studies and "
                    "do not browse other pages. If no eligible study is listed, follow your task "
                    "instructions for that case (refresh, wait or finish).")
                yield {"type": "notice",
                       "text": f"Più di {n} s fuori dall'elenco studi senza svolgerne uno: riportato a /studies.",
                       "messages": messages}

            if batch_fill:
                batch_step_event, batch_next_text, batch_diag = _maybe_batch_fill(
                    task, attempted_form_signatures, survey_memory)
                transcript.write(json.dumps(
                    {"step": step, "batch_fill_check": batch_diag}) + "\n")
                transcript.flush()
                if batch_step_event is not None:
                    pending_user_response = batch_next_text
                    batch_step_event["step"] = step
                    batch_step_event["messages"] = messages
                    injected = yield batch_step_event
                    if injected:
                        pending_user_response = injected
                    continue

            # Reset timer when back at the studies list (timer re-arms for next study).
            if _on_guarded_site(here) and _LIST_PATH.match(urlparse(here).path or ""):
                study_timer.reset()

            _live({"type": "phase", "phase": "observe", "step": step})
            img = take_screenshot(run_dir, step)
            data_url = encode_for_model(img)

            if pending_user_response:
                text = pending_user_response
                pending_user_response = ""
            elif step == 1:
                text = task + STANDING_RULES
            else:
                url = get_current_url()
                prefix = f"Current URL: {url}\n" if url else ""
                timer_line = study_timer.status_line()
                if timer_line:
                    prefix += timer_line + "\n"
                    _live({"type": "timer", "elapsed": study_timer.elapsed(),
                           "remaining": study_timer.remaining(),
                           "min_seconds": study_timer.min_seconds})
                text = f"{prefix}{fp.USER_MESSAGE}"

            messages.append({
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": text},
                ],
            })

            _live({"type": "phase", "phase": "think", "step": step})
            content = call_fara(messages, step)
            cancel.check()   # Stop pressed during the call: don't act on its answer
            messages.append({"role": "assistant", "content": content})
            _trim_to_budget(messages)

            thoughts, action = parse_thoughts_and_action(content)
            args = action.get("arguments", {})
            action_type = args.get("action", "unknown")

            if args.get("coordinate"):
                args["coordinate"] = fp.scale_coordinate(args["coordinate"])

            transcript.write(json.dumps({
                "step": step, "raw_response": content, "thoughts": thoughts,
                "action_type": action_type, "args": args,
            }) + "\n")
            transcript.flush()

            screenshot_path = str(run_dir / f"screenshot_{step:02d}.png")

            if action_type == "ask_user_question":
                question = args.get("question", "")
                answer = yield {
                    "type": "question", "step": step, "thoughts": thoughts,
                    "question": question, "screenshot": screenshot_path,
                    "messages": messages,
                }
                if answer is None:
                    yield {"type": "done", "answer": "STOPPED", "messages": messages}
                    return
                pending_user_response = answer
                continue

            target = {"type": "target", "step": step, "action": action_type}
            if args.get("coordinate"):
                target["x"], target["y"] = args["coordinate"][0], args["coordinate"][1]
            if action_type == "type":
                target["chars"] = len(str(args.get("text", "")))   # length only: typed text may be sensitive
            elif action_type == "key":
                target["keys"] = args.get("keys")
            elif action_type in ("scroll", "hscroll"):
                target["pixels"] = args.get("pixels")
            _live(target)
            _live({"type": "phase", "phase": "act", "step": step, "action": action_type})
            # Read study duration from the Prolific page BEFORE dispatch so we
            # capture the exact study about to be joined (not generic browsing).
            # Pass click coordinates so elementFromPoint targets that specific card.
            pre_click_duration: float | None = None
            if action_type == "left_click" and _on_guarded_site(here):
                coord = args.get("coordinate")
                cx, cy = (coord[0], coord[1]) if coord else (None, None)
                pre_click_duration = _extract_study_duration(cx, cy)
            try:
                result_desc = dispatch_action(action_type, args)
            except Exception as e:
                # One failed browser action (timeout, element gone, tab
                # closed) must not end a 24/7 run -- it used to: a
                # history_back timeout raised straight out of the generator
                # and stopped everything. Tell the model what failed so it
                # can try something else on the next screenshot.
                msg = str(e).splitlines()[0][:200]
                result_desc = f"ACTION FAILED ({action_type}): {msg}"
                pending_user_response = (
                    f"Your last action ({action_type}) failed: {msg}. The page may be in a "
                    "different state than you expect; look at the screenshot and try a "
                    "different approach.")

            # Detect Prolific → external survey transition: start the timer.
            if action_type == "left_click" and _on_guarded_site(here):
                new_url = get_current_url()
                if not _on_guarded_site(new_url) and not study_timer.is_active():
                    if pre_click_duration is not None:
                        study_timer.start(pre_click_duration * 60)
                        _live({"type": "timer_start", "min_seconds": study_timer.min_seconds,
                               "min_minutes": pre_click_duration})
                    else:
                        # Duration not found — timer inactive, no minimum.
                        _live({"type": "timer_start", "min_seconds": 0, "min_minutes": 0})

            step_event = {
                "type": "step", "step": step, "thoughts": thoughts,
                "action_type": action_type, "args": args,
                "result_desc": result_desc, "screenshot": screenshot_path,
                "messages": messages,
            }

            if action_type == "terminate":
                yield step_event
                answer = args.get("answer", "")
                if answer.startswith("CODE:") or answer == "NOCODE":
                    _notify_telegram(f"Fara studio completato ✅\n{answer}\nCiclo: {run_dir.name if run_dir else '?'}")
                yield {"type": "done", "answer": answer, "messages": messages}
                return

            # Reuses the same pending_user_response channel as an
            # ask_user_question answer, but offered proactively after every
            # step instead of only when the model asks: a caller may
            # .send(text) here to steer the agent before its next move.
            injected = yield step_event
            if injected:
                pending_user_response = injected
            # No fixed settle delay here: measured at 0.5s against a ~9.3s
            # step it was ~5% of wall-clock for no benefit -- the next
            # thing the loop does is take a screenshot, and actions that
            # genuinely need settling time (navigation, clicks that load a
            # page) are handled by the model issuing an explicit `wait`.

        yield {"type": "done", "answer": f"max_rounds={max_rounds} reached without terminate",
               "messages": messages}
    finally:
        transcript.close()
