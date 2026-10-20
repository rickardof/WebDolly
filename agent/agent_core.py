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
import gzip
import io
import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse
import urllib.request

from PIL import Image

import fara_prompt as fp
import cancel
import form_fill_pw as form_fill
import study_cache
import study_memory
import os
import re
import pw_bridge

FARA_URL   = os.environ.get("FARA_URL",   "http://127.0.0.1:8090/v1/chat/completions")
FARA_MODEL = os.environ.get("FARA_MODEL", "Fara1.5-27B-Q4_K_M.gguf")
FARA_API_KEY = os.environ.get("FARA_API_KEY", "")

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
# Everything is found relative to the checkout, so moving or renaming the
# folder (or running on a VM with a different user) needs no edits.
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("FARA_DATA_DIR", REPO_ROOT / "fixtures" / "pw"))
LOG_ROOT = Path(os.environ.get("FARA_LOG_ROOT", REPO_ROOT / "var" / "logs" / "pw"))

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
MAX_RUN_DIRS = int(os.environ.get("FARA_MAX_RUNS", "200"))


def _prune_old_runs(keep: int = None):
    """Thin out old runs without losing their history.

    The screenshots are what fills the disk -- one PNG per step, thousands a
    day in a 24/7 loop -- while a transcript is tens of KB and is the only
    record of what the agent actually did. So past the newest `keep` runs the
    screenshots are deleted and the transcript is gzipped in place and kept
    forever. Both the dashboard and the submission counter read `.jsonl` and
    `.jsonl.gz`, so nothing is lost from the stats.
    """
    if keep is None:
        keep = MAX_RUN_DIRS
    run_dirs = sorted(
        (p for p in LOG_ROOT.glob("run_*") if p.is_dir()),
        key=lambda p: p.name,
    )
    for old in (run_dirs[:-keep] if keep > 0 else run_dirs):
        for shot in old.glob("screenshot_*.png"):
            try:
                shot.unlink()
            except Exception:
                pass
        tr = old / "transcript.jsonl"
        if not tr.exists():
            continue
        try:
            with tr.open("rb") as src, gzip.open(old / "transcript.jsonl.gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            tr.unlink()
        except Exception:
            pass

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


# History images are degraded in speed modes to save tokens; the CURRENT
# screenshot stays full-res so the model can click accurately.
# Measured token counts (patches): 1.0→1260, 0.5→714, 0.25→308, 0.15→77
HISTORY_SCALE = {"normal": 1.0, "hurry": 0.5, "super_fast": 0.25}


def _encode_image(img: Image.Image, scale: float = 1.0,
                  jpeg: bool = False) -> str:
    """Encode an image as a data URL at the given scale."""
    h, w = img.height, img.width
    if scale != 1.0:
        h, w = max(1, int(h * scale)), max(1, int(w * scale))
    rh, rw = fp.smart_resize(h, w)
    resized = img.resize((rw, rh), Image.LANCZOS) if scale != 1.0 else img.resize((rw, rh))
    buf = io.BytesIO()
    if jpeg:
        resized.save(buf, format="JPEG", quality=55)
        mime = "image/jpeg"
    else:
        resized.save(buf, format="PNG")
        mime = "image/png"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode()


def encode_for_model(img: Image.Image) -> str:
    """Current screenshot — always full resolution for click accuracy."""
    return _encode_image(img, scale=1.0, jpeg=False)


def encode_for_history(img: Image.Image) -> str:
    """Older screenshot being kept in context — degraded in speed modes to
    save tokens while preserving the current screenshot's full resolution."""
    scale = HISTORY_SCALE.get(_speed_mode, 1.0)
    return _encode_image(img, scale=scale, jpeg=(scale < 1.0))


# Whether the chat template opens a <think> block for the model to reason in.
# Measured per model, not assumed: on the 27B it makes no difference (same
# tokens, same action -- Fara's short narrated "thoughts" come from its
# fine-tuning, not from this switch). On the 9B it is decisive: with the
# default (thinking on) it answered 0/6 clicks, always a bogus mouse_move
# ("I will move the cursor to the drag start point") 80-280px off target;
# with thinking off, 5/6 correct clicks. Set FARA_ENABLE_THINKING=1 to
# restore the template default.
ENABLE_THINKING = os.environ.get("FARA_ENABLE_THINKING", "0") == "1"

_SPEED_RANK = {"normal": 0, "hurry": 1, "super_fast": 2}
SPEED_MODES = tuple(_SPEED_RANK)

_speed_mode: str = "normal"  # what is in force right now
_speed_user: str = "normal"  # what the user explicitly chose (restored after auto-escalation)
_speed_auto: bool = False    # True while speed was auto-escalated by the timer

# Back-compat alias
_hurry_mode: bool = False


def speed_state() -> dict:
    """Current speed for the GUI/API: what is in force, what the user picked,
    and whether the difference comes from the timer escalating on its own."""
    return {"mode": _speed_mode, "user": _speed_user, "auto": _speed_auto}


def _emit_speed():
    _live({"type": "speed", **speed_state()})


def set_speed(mode: str) -> bool:
    """Pick a speed mode explicitly (GUI, API or Telegram). False if unknown."""
    global _speed_mode, _speed_user, _speed_auto, _hurry_mode
    if mode not in _SPEED_RANK:
        return False
    _speed_mode = mode
    _speed_user = mode
    _speed_auto = False
    _hurry_mode = mode != "normal"
    _emit_speed()
    return True


def _auto_escalate(mode: str):
    """Set speed without changing the user's saved preference. Never downgrades."""
    global _speed_mode, _speed_auto, _hurry_mode
    if _SPEED_RANK.get(mode, 0) <= _SPEED_RANK.get(_speed_mode, 0):
        return
    _speed_mode = mode
    _speed_auto = True
    _hurry_mode = mode != "normal"
    _emit_speed()


def _restore_user_speed():
    """Restore the speed mode the user explicitly chose."""
    global _speed_mode, _speed_auto, _hurry_mode
    if _speed_auto:
        _speed_mode = _speed_user
        _speed_auto = False
        _hurry_mode = _speed_mode != "normal"
        _emit_speed()


def set_hurry(enabled: bool):
    set_speed("hurry" if enabled else "normal")


# --- model speed watch -------------------------------------------------------
# Measured on 13 Oct: after hours of prompt-cache reuse on one llama-server
# slot, generation fell from ~33 to 17 and then 2.4 tokens/s while a fresh
# request still ran at 24-33 -- the model was fine, the reused slot was not.
# Nothing in the agent noticed: it just took 50 s a step. So every call's
# timings are kept, logged, and a sustained collapse is reported.
SLOW_TPS = float(os.environ.get("FARA_SLOW_TPS", "10"))
SLOW_WINDOW = 6                 # consecutive calls looked at
SLOW_ALERT_EVERY = 3600         # seconds between two Telegram alerts
_gen_tps: list = []
_last_slow_alert = 0.0
last_timing: dict = {}


def _record_timing(timings: dict):
    """Keep the speed of the last calls and warn when it collapses."""
    global last_timing, _last_slow_alert
    if not timings:
        last_timing = {}
        return
    last_timing = {
        "prompt_n": timings.get("prompt_n"),
        "prompt_tps": round(timings.get("prompt_per_second") or 0, 1),
        "gen_n": timings.get("predicted_n"),
        "gen_tps": round(timings.get("predicted_per_second") or 0, 1),
        "cache_n": timings.get("cache_n"),
    }
    if (timings.get("predicted_n") or 0) < 20:      # too short to say anything
        return
    _gen_tps.append(last_timing["gen_tps"])
    del _gen_tps[:-SLOW_WINDOW]
    if len(_gen_tps) < SLOW_WINDOW:
        return
    med = sorted(_gen_tps)[len(_gen_tps) // 2]
    if med >= SLOW_TPS:
        return
    _live({"type": "notice", "text": f"Modello lento: {med:.1f} token/s di mediana sulle "
                                      f"ultime {SLOW_WINDOW} risposte (soglia {SLOW_TPS:.0f})."})
    if time.time() - _last_slow_alert > SLOW_ALERT_EVERY:
        _last_slow_alert = time.time()
        _notify_telegram(
            f"🐌 Modello lento: {med:.1f} token/s (di solito ~30).\n"
            "Di solito è la cache di llama-server degradata dopo ore di uso: "
            "`docker restart fara-test` la riporta a posto.")


def model_speed() -> dict:
    """Recent generation speed, for the GUI/API."""
    if not _gen_tps:
        return {}
    return {"median_gen_tps": sorted(_gen_tps)[len(_gen_tps) // 2],
            "last": last_timing, "slow_threshold": SLOW_TPS}


def call_fara(messages: list, step: int = None) -> str:
    payload = {
        "model": FARA_MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": {"normal": 1024, "hurry": 512, "super_fast": 384}[_speed_mode],
    }
    # chat_template_kwargs is llama-server-only — skip when using a cloud API
    if not FARA_API_KEY:
        payload["chat_template_kwargs"] = {"enable_thinking": ENABLE_THINKING}
    extra_hdrs = {"Authorization": f"Bearer {FARA_API_KEY}"} if FARA_API_KEY else None
    result = cancel.post_chat(FARA_URL, payload, timeout=120,
                              on_delta=lambda t: _live({"type": "token", "step": step, "text": t}),
                              extra_headers=extra_hdrs)
    _record_timing(result.get("timings") or {})
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
CONTEXT_TOKEN_BUDGET = int(os.environ.get("FARA_TOKEN_BUDGET", "28000"))
MEASURED_IMAGE_TOKENS = 1262
PER_MESSAGE_OVERHEAD_MARGIN = 20
# llama-server tokenizer endpoint — not available for cloud APIs
_TOKENIZE_URL = FARA_URL.rsplit("/v1/", 1)[0] + "/tokenize" if not FARA_API_KEY else ""


def _tokenize_count(text: str) -> int:
    """Count tokens via llama-server, or fall back to a word-based estimate."""
    if not text:
        return 0
    if _TOKENIZE_URL:
        try:
            req = urllib.request.Request(
                _TOKENIZE_URL, data=json.dumps({"content": text}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return len(json.load(r)["tokens"])
        except Exception:
            pass
    # Fallback: ~0.75 tokens per character (conservative for English)
    return max(1, len(text) // 4)


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


# In speed modes, history images are degraded BEFORE trimming counts: re-encoded
# at lower resolution so they cost fewer tokens, letting the model keep more
# visual context without blowing the budget. The last image (current screenshot)
# is never touched — it needs full resolution for accurate clicks.
_TRIM_MAX_IMAGES   = {"normal": 10, "hurry": 8, "super_fast": 5}
_TRIM_TO_IMAGES    = {"normal": 5,  "hurry": 3, "super_fast": 2}


def _degrade_history_images(messages: list):
    """Re-encode older screenshots at the speed-appropriate lower resolution.
    Skips the most recent image (always full-res for clicking)."""
    if _speed_mode == "normal":
        return
    image_idxs = _image_turn_idxs(messages)
    if len(image_idxs) < 2:
        return
    scale = HISTORY_SCALE.get(_speed_mode, 1.0)
    if scale >= 1.0:
        return
    # Degrade all but the last (most recent) image
    for i in image_idxs[:-1]:
        for c in messages[i]["content"]:
            if c.get("type") != "image_url":
                continue
            url = c["image_url"]["url"]
            # Already degraded (JPEG) — skip
            if "image/jpeg" in url:
                continue
            try:
                header, b64 = url.split(",", 1)
                raw = base64.b64decode(b64)
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                c["image_url"]["url"] = _encode_image(img, scale=scale, jpeg=True)
            except Exception:
                pass


def _trim_to_budget(messages: list, budget: int = CONTEXT_TOKEN_BUDGET,
                     max_images: int | None = None,
                     trim_to: int | None = None) -> int:
    """Brings `messages` under `budget`, verified against the real
    tokenizer at every stage rather than assumed. Returns the real token
    count after trimming (0 trims performed if already under budget)."""
    # First pass: degrade history images in speed modes (cheap, no data lost)
    _degrade_history_images(messages)

    if max_images is None:
        max_images = _TRIM_MAX_IMAGES.get(_speed_mode, MAX_IMAGES_IN_CONTEXT)
    if trim_to is None:
        trim_to = _TRIM_TO_IMAGES.get(_speed_mode, TRIM_TO_IMAGES)
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


class BrowserStateError(RuntimeError):
    """The bridge cannot say what page the browser is on."""


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
_STUDY_PATH = re.compile(r"^/studies/([0-9a-fA-F]{24})/?$", re.IGNORECASE)
_COMPLETE_PATH = re.compile(r"^/submissions/complete/?$", re.IGNORECASE)


_FILLER = r"(?:is|below|here|in|on|into|to|prolific|please|enter|paste|copy|the|your|following|this)"
_PAGE_CODE = re.compile(
    r"(?:completion|confirmation|survey|prolific|study)\s*(?:code|id)\b"
    r"(?:[\s:,.\-]+" + _FILLER + r"\b)*[\s:,.\-]*"
    # the code itself can't be the next keyword ("Prolific code / completion
    # code: X" must yield X, not swallow "completion")
    r"(?!(?:completion|confirmation|survey|prolific|study)\b)"
    r"([A-Za-z0-9][A-Za-z0-9_\-]{3,40})", re.IGNORECASE)
_NOT_A_CODE = {"will", "and", "when", "after", "must", "should", "has", "have"}


def _end_page_text(limit: int = 40000) -> str:
    """Page text for code hunting: every frame (iframes included, through
    Playwright, which can enter cross-origin ones) plus the values of text
    fields whose label mentions a code -- end pages often put the code in a
    read-only box to copy, and innerText leaves field values out."""
    _JS = r"""() => {
        const parts = [document.body ? document.body.innerText : ''];
        for (const el of document.querySelectorAll(
                'input:not([type=password]):not([type=hidden]), textarea')) {
            const v = (el.value || '').trim();
            if (!v || v.length > 60) continue;
            const ctx = [el.getAttribute('aria-label'), el.placeholder, el.name, el.id,
                         ...(el.labels ? [...el.labels].map(l => l.innerText) : []),
                         el.parentElement ? el.parentElement.innerText : ''].join(' ');
            if (/code/i.test(ctx)) parts.push('completion code: ' + v);
        }
        return parts.join('\n');
    }"""

    def _collect(b):
        out = []
        for f in b.active_page().frames:
            try:
                out.append(f.evaluate(_JS) or "")
            except Exception:
                pass
        return "\n".join(out)
    try:
        return (pw_bridge.run(_collect) or "")[:limit]
    except Exception:
        return _page_text(limit)


def codes_on_page(text: str) -> list:
    """Completion codes printed on a custom end page ("Your completion code
    is: C1A2B3"). Many studies end on their own page instead of redirecting
    to Prolific, so the code lives only on screen. Used to double-check what
    the model read, never to replace it."""
    out = []
    for m in _PAGE_CODE.finditer(text or ""):
        c = m.group(1)
        if c.lower() in _NOT_A_CODE or (c.isalpha() and c.islower()) or c in out:
            continue          # an ordinary word, or already seen
        out.append(c)
    return out


_CODE_PARAM = re.compile(r"(^|[_\-])(cc|code|completion[_\-]?code|completioncode|pcode)$", re.IGNORECASE)


def codes_in_url(url: str) -> list:
    """Codes carried in an external page's URL (?code=PLKXLQ). Seen on 13 Oct:
    a survey's thank-you page had the code only in its own address, and the
    model typed it into Prolific from there."""
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


def completion_code(url: str) -> str:
    """The completion code Prolific carries in its own redirect, or "".

    A finished survey sends the participant to
    app.prolific.com/submissions/complete?cc=XXXX. Reading it from there is
    exact; reading it off the screen is the model's job only when there is
    nothing in the URL. Not every study has a code (some complete without
    one and the model answers NOCODE): an empty result here means "no
    opinion", never "no code".
    """
    try:
        pr = urlparse(url or "")
        if not _on_guarded_site(url) or not _COMPLETE_PATH.match(pr.path or ""):
            return ""
        cc = (parse_qs(pr.query).get("cc") or [""])[0].strip()
        return cc if re.fullmatch(r"[A-Za-z0-9_\-]{2,64}", cc) else ""
    except Exception:
        return ""


_EMPTY_LIST_MARKERS = ("waiting for studies",)
# "showing: 0/0 studies" (or "0 / 0") — Prolific's counter when the list is empty.
_EMPTY_LIST_RE = re.compile(r"showing\s*:?\s*0\s*/\s*0\s*stud", re.IGNORECASE)


def studies_list_state() -> str:
    """What the studies list shows right now, read from the DOM, no model.

    "empty"   -- the list page with Prolific's waiting message
    "studies" -- the list page without it, i.e. something to look at
    "unknown" -- not on the list, logged out, page not readable...

    Only "empty" is ever trusted to skip the model. Anything else falls back
    to a normal cycle, so a layout change costs a model call, never a study.
    """
    url = get_current_url()
    if not url or not _on_guarded_site(url) or not _LIST_PATH.match(urlparse(url).path or ""):
        return "unknown"
    return list_state_from(url, _page_text(8000))


def list_state_from(url: str, text: str) -> str:
    """The decision behind studies_list_state(), on a URL and the page text."""
    if not url or not _on_guarded_site(url) or not _LIST_PATH.match(urlparse(url).path or ""):
        return "unknown"
    text = (text or "").lower()
    if not text.strip():
        return "unknown"
    if any(m in text for m in _EMPTY_LIST_MARKERS):
        return "empty"
    if _EMPTY_LIST_RE.search(text):
        return "empty"
    return "studies"


CAPTURE_DIR = Path(os.environ.get("FARA_CAPTURE_DIR",
                                  Path(__file__).resolve().parent.parent / "var" / "captures"))


def capture_page(kind: str) -> None:
    """Save the current page's HTML once per kind per day.

    Real Prolific pages (a list with studies, a study page, the completion
    redirect, the return flow) are what the DOM tests and the code paths
    built on selectors need, and they only show up during real runs. They
    land in var/ (ignored by git: they carry the account's name and balance)
    to be trimmed and promoted to fixtures/ by hand.
    """
    try:
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        out = CAPTURE_DIR / f"{kind}_{datetime.now():%Y%m%d}.html"
        if out.exists():
            return
        html = pw_bridge.evaluate("document.documentElement.outerHTML") or ""
        if html:
            out.write_text(f"<!-- {get_current_url()} -->\n{html}")
    except Exception:
        pass


def studies_url() -> str:
    """Where to go back to when a study has to be abandoned."""
    return os.environ.get("FARA_STUDIES_URL", "https://app.prolific.com/studies")


_DOM_HINT_JS = r"""(args) => {
    const [nav, arrows] = args;
    const vh = window.innerHeight;
    let best = null;
    const sels = 'button, input[type=submit], input[type=button], [role=button]';
    for (const el of document.querySelectorAll(sels)) {
        const r = el.getBoundingClientRect();
        if (r.width < 5 || r.height < 5) continue;
        if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
        const t = ((el.tagName === 'INPUT' ? el.value : el.textContent) || '')
            .trim().replace(/\s+/g, ' ');
        const lo = t.toLowerCase();
        const meta = [el.id, el.name, el.getAttribute('aria-label'), el.title]
            .filter(Boolean).join(' ').toLowerCase();
        const match = arrows.includes(lo)
            || nav.some(n => lo === n || lo.startsWith(n + ' '))
            || /(^|[\s_-])next(button|[\s_-]|$)/.test(meta);
        if (!match) continue;
        const cx = Math.round(r.left + r.width / 2);
        const cy = Math.round(r.top + r.height / 2);
        best = {t: t.slice(0, 30), cx, cy, below: cy >= vh, above: cy < 0};
    }
    return best;
}"""

_NAV_LABELS = [
    "next", "continue", "submit", "proceed", "start", "begin",
    "avanti", "continua", "invia", "procedi", "conferma", "inizia",
    "next page", "next question", "save and continue",
]
_NAV_ARROWS = [">>", ">", "→", "»", "›", "->", "=>"]


def _dom_nav_hint() -> str:
    """Find the primary navigation button on the page and report its location."""
    def _search(b):
        page = b.active_page()
        frames = page.frames
        for fr in frames:
            try:
                hit = fr.evaluate(_DOM_HINT_JS, [_NAV_LABELS, _NAV_ARROWS])
                if hit:
                    return hit
            except Exception:
                continue
        return None
    try:
        btn = pw_bridge.run(_search, timeout=5)
    except Exception:
        return ""
    if not btn:
        return ""
    mx = round(btn["cx"] * fp.DISPLAY_SIZE / fp.VIEWPORT_WIDTH)
    my = round(btn["cy"] * fp.DISPLAY_SIZE / fp.VIEWPORT_HEIGHT)
    label = btn["t"]
    if btn.get("below"):
        return f'["{label}" button is below the visible area — scroll down to reach it.]\n'
    if btn.get("above"):
        return f'["{label}" button is above the visible area — scroll up.]\n'
    return f'["{label}" button at ({mx},{my})]\n'


def _page_text(limit: int = 20000) -> str:
    try:
        return (pw_bridge.evaluate("document.body.innerText") or "")[:limit]
    except Exception:
        return ""


def _study_title() -> str:
    """Name of the study on the page being looked at, or "".

    Only used to tell the model which studies to skip: the list page shows
    names, never ids, so a cached entry without a title is useless to it.
    """
    _JS = """() => {
        const sel = ['[data-testid*="study-name"]', '[data-testid*="title"]',
                     'main h1', 'main h2', 'h1', 'h2'];
        for (const s of sel) {
            const el = document.querySelector(s);
            const t = el && (el.innerText || '').trim();
            if (t && t.length > 3 && t.length < 120) return t;
        }
        return (document.title || '').split('|')[0].trim();
    }"""
    try:
        return (pw_bridge.run(lambda b: b.active_page().evaluate(_JS)) or "").strip()[:100]
    except Exception:
        return ""


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
    (r"screened out", "screened out"),
)

# Studies that need headphones, a camera or a mic: opt-in, because skipping
# them is a choice about what work to accept, not a safety rule.
_AV_TEXT_MARKERS = (
    (r"\bheadphones?\b|\bearphones?\b|\bcuffie\b|\bauricolari\b", "cuffie richieste"),
    (r"listen to (?:the |this |an? )?(?:audio|recording|clip|sound|speech)"
     r"|ascolt(?:a|erai) (?:l'|un |il )?(?:audio|registrazione)", "audio da ascoltare"),
    (r"audio (?:clip|recording|stimuli|task)", "stimoli audio"),
)


def _detect_dead_end(text: str) -> str:
    for pattern, reason in _DEAD_END_MARKERS:
        if re.search(pattern, text, re.IGNORECASE):
            return reason
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
        found = pw_bridge.run(lambda b: b.active_page().evaluate(_JS)) or ""
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


def _close_extra_tabs():
    """Close all tabs except the active one. Called after study completion."""
    try:
        def _do(b):
            active = b.active_page()
            for p in b.pages():
                if p is not active:
                    try:
                        p.close()
                    except Exception:
                        pass
        pw_bridge.run(_do)
    except Exception:
        pass


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
        if not re.search(r"\d+\s*(?:minute|min)", text, re.IGNORECASE):
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

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.start_time: float | None = None
        self.min_seconds: float = 0

    def start(self, min_seconds: float):
        self.start_time = self.clock()
        self.min_seconds = float(min_seconds)

    def reset(self):
        self.start_time = None
        self.min_seconds = 0

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
            return (f"Study timer: {self.elapsed():.0f}s elapsed / "
                    f"{self.min_seconds:.0f}s minimum — OK to submit now.")
        return (f"Study timer: {self.elapsed():.0f}s elapsed, "
                f"{rem:.0f}s still needed (minimum {self.min_seconds:.0f}s). "
                "Do NOT submit yet — use the `wait` action on the last page.")


# Submitting a few seconds early is not worth another whole model step: past
# this margin the agent is allowed to finish, under it it is told to wait.
TERMINATE_GRACE_SECONDS = 30


def terminate_refusal(timer: "StudyTimer") -> str:
    """Why `terminate` must not go through yet, or "" if it may."""
    if timer.is_active() and timer.remaining() > TERMINATE_GRACE_SECONDS:
        return (f"TERMINATE REFUSED: study timer has {int(timer.remaining())}s remaining "
                f"(minimum {timer.min_seconds:.0f}s). "
                "Use the `wait` action to wait (e.g. wait 30), then try again.")
    return ""


def _near_checkbox_state(x: float, y: float):
    """Return checked state of nearest checkbox within 80px, or None."""
    _JS = """([x, y]) => {
        const cbs = [...document.querySelectorAll('input[type=checkbox]')];
        let best = null, minD = Infinity;
        for (const cb of cbs) {
            const r = cb.getBoundingClientRect();
            const d = Math.hypot(r.left + r.width/2 - x, r.top + r.height/2 - y);
            if (d < 80 && d < minD) { minD = d; best = cb.checked; }
        }
        return best;
    }"""
    try:
        return pw_bridge.run(lambda b: b.active_page().evaluate(_JS, [x, y]))
    except Exception:
        return None


def _js_click_near_checkbox(x: float, y: float):
    """JS-click the nearest checkbox within 80px (fallback for missed clicks)."""
    _JS = """([x, y]) => {
        const cbs = [...document.querySelectorAll('input[type=checkbox]')];
        let best = null, minD = Infinity;
        for (const cb of cbs) {
            const r = cb.getBoundingClientRect();
            const d = Math.hypot(r.left + r.width/2 - x, r.top + r.height/2 - y);
            if (d < 80 && d < minD) { minD = d; best = cb; }
        }
        if (best) best.click();
    }"""
    try:
        pw_bridge.run(lambda b: b.active_page().evaluate(_JS, [x, y]))
    except Exception:
        pass


def _check_disabled_near(x: float, y: float) -> bool:
    """Return True if the clicked element or nearest button/submit is disabled."""
    _JS = """([x, y]) => {
        const el = document.elementFromPoint(x, y);
        if (!el) return false;
        // Check the element itself and walk up to find a button/input
        let node = el;
        for (let i = 0; i < 10 && node && node !== document.body; i++) {
            const tag = node.tagName;
            if (tag === 'BUTTON' || tag === 'INPUT' || tag === 'A') {
                if (node.disabled || node.getAttribute('aria-disabled') === 'true'
                    || node.classList.contains('disabled'))
                    return true;
            }
            node = node.parentElement;
        }
        return false;
    }"""
    try:
        return pw_bridge.run(lambda b: b.active_page().evaluate(_JS, [x, y]))
    except Exception:
        return False


def dispatch_action(action_type: str, args: dict) -> str:
    if action_type == "left_click":
        x, y = args["coordinate"]
        cb_before = _near_checkbox_state(x, y)
        docker_exec("xdotool", "mousemove", str(round(x)), str(round(y)), "click", "1")
        if cb_before is not None:
            cb_after = _near_checkbox_state(x, y)
            if cb_after == cb_before:
                _js_click_near_checkbox(x, y)
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

    if action_type == "run_js":
        code = args.get("code", "")
        if not code.strip():
            return "run_js: no code provided"
        stripped = code.strip()
        # Don't double-wrap if the model already sent an IIFE
        if stripped.startswith('(') and stripped.rstrip(';').endswith(')'):
            wrapped = stripped
        else:
            wrapped = f"(async () => {{ {stripped} }})()"
        try:
            result = pw_bridge.run(
                lambda b: b.active_page().evaluate(wrapped),
                timeout=120)
            if result is None:
                return ("run_js: completed (no return value). The code ran but "
                        "returned nothing — check the next screenshot for effects.")
            return f"run_js: {str(result)[:500]}"
        except Exception as e:
            return f"run_js error: {type(e).__name__}: {str(e)[:200]}"

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
_STANDING_RULES_FULL = (
    "\n\nSTANDING RULES (always apply):\n"
    "1. Choosing a study: compare ALL study cards first; among the eligible ones open the "
    "one with the highest reward rate per hour (the £/hr or $/hr shown), not just the first.\n"
    "2. NO AUDIO, EVER: a study that requires LISTENING to audio (sounds, speech, music, "
    "recordings, 'audio playback', headphones or speakers) is not eligible. An 'Audio' "
    "icon/label on the card means skip it. Videos are OK as long as the study does not "
    "require hearing their sound — you can watch silent or muted videos. Microphone or "
    "camera requirements alone do NOT disqualify a study. "
    "Before pressing 'Take part in this study', read the requirements section; if it "
    "mentions listening or audio playback, do not take part.\n"
    "3. Answer quality: read every question and its FULL text carefully before answering. "
    "ATTENTION CHECKS are critical — questions like 'select Strongly Agree', 'choose option 3', "
    "'what colour is the sky', or 'please select X to show you are paying attention' MUST be "
    "answered exactly as instructed, even if the answer seems wrong in context. Failing an "
    "attention check can invalidate the entire submission. Keep every answer consistent with "
    "the persona and with your earlier answers in the same survey. Write specific, relevant "
    "open-ended answers in the survey's language, never generic filler.\n"
    "4. Scope: your only job is to take part in studies that meet the criteria and, when none "
    "is available, to wait and re-check the list of studies for new ones. Never open the "
    "'About you' section of the site or answer profile questionnaires outside a study.\n"
    "4a. After clicking 'Take part in this study', the button turns grey/disabled while loading — "
    "this is NORMAL. Wait a few seconds for the study to open; do NOT go back or assume it failed.\n"
    "4b. After finishing a survey: ALWAYS switch back to the Prolific tab before calling "
    "terminate. On the Prolific tab: (a) click 'I'm finished'; (b) if a completion-code field "
    "appears, enter the code you received, or if no code was given click 'I didn't receive a "
    "completion code' (or type NOCODE in the field if that link is absent); (c) confirm/submit. "
    "Only after this Prolific-side step call terminate with CODE:<code> or NOCODE.\n"
    "4c. Broken / screened-out studies: if the external survey is stuck, broken, or you were "
    "screened out and cannot finish, use Ctrl+Shift+Tab to switch to the Prolific tab (do NOT "
    "use history_back — the survey is in a separate tab). On the Prolific tab click "
    "'Cancel participation' (a red button), confirm in the modal, then terminate with NOCODE. "
    "Do NOT keep refreshing or waiting — cancel and move on.\n"
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
    "and never claim qualifications the persona does not have.\n"
    "7. Speed: when a survey has many identical repeated trials (e.g. 60 A/B comparisons, "
    "repeated Likert scales), use `run_js` to batch-complete them in one step instead of "
    "clicking through each one manually. Write JS that clicks the selection button, submits, "
    "waits for the next trial to load, and repeats in a loop for the remaining trials. "
    "IMPORTANT: use standard DOM APIs only — ':contains()' is NOT valid CSS. To find a "
    "button by text, use: Array.from(document.querySelectorAll('button'))"
    ".find(b => b.textContent.includes('Submit')). Do NOT wrap your code in (async () => "
    "{ ... })() — the runner already does that."
)

_STANDING_RULES_COMPACT = (
    "\n\nRULES: "
    "1. Pick the highest £/hr study. "
    "2. NO AUDIO — skip studies requiring listening/speakers/headphones. Videos OK if no audio needed. "
    "3. Read FULL question text before answering. ATTENTION CHECKS are critical — answer exactly as instructed even if it seems wrong. Stay consistent with persona; no filler. "
    "4. Only do studies, nothing else. After 'Take part' button goes grey, WAIT — it's loading. "
    "After survey: Prolific tab → 'I'm finished' → enter code → confirm → terminate. "
    "If study is broken/screened out: Ctrl+Shift+Tab to Prolific tab → 'Cancel participation' → confirm → terminate NOCODE. "
    "5. Respect minimum time — wait if timer says time remains. "
    "6. Screener: emphasise true persona traits that match; never invent contradicting facts. "
    "7. For repetitive trials (many A/B comparisons, repeated scales), use run_js to batch them "
    "in one step. Use standard DOM APIs — ':contains()' is NOT CSS. Do NOT wrap in (async () => {})()."
)


def _standing_rules() -> str:
    if _speed_mode == "normal":
        return _STANDING_RULES_FULL
    return _STANDING_RULES_COMPACT


def summarize_transcript(lines: list, started: datetime = None, ended: datetime = None) -> dict:
    """One run, reduced to what the dashboard and the daily summary need.

    Where the steps went is read from the per-step diag lines: on the
    studies list, inside a study (the external questionnaire, timer
    running), or elsewhere on Prolific.
    """
    steps, answer, source = 0, "", None
    where = {"list": 0, "study": 0, "elsewhere": 0}
    gen = []
    studies = []
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if isinstance(d.get("step"), int):
            steps = max(steps, d["step"])
        if d.get("action_type") == "terminate":
            answer = (d.get("args") or {}).get("answer", "")
            source = d.get("code_source") or source or "model"
        if d.get("type") == "diag":
            url = d.get("url") or ""
            if d.get("timer_active") and not d.get("guarded"):
                where["study"] += 1
            elif d.get("guarded") and _LIST_PATH.match(urlparse(url).path or ""):
                where["list"] += 1
            else:
                where["elsewhere"] += 1
            m = _STUDY_PATH.match(urlparse(url).path or "")
            if m and m.group(1) not in studies:
                studies.append(m.group(1))
            if d.get("code_source"):
                source = d["code_source"]
            tps = (d.get("model") or {}).get("gen_tps")
            if tps:
                gen.append(tps)
    if answer.startswith("CODE:"):
        outcome = "CODE"
    elif answer in ("NOCODE", "NONE", "STOPPED"):
        outcome = answer
    else:
        outcome = "INCOMPLETE" if not answer else "TERMINATE"
    out = {"steps": steps, "outcome": outcome, "answer": answer[:80],
           "code_source": source if outcome in ("CODE", "NOCODE") else None,
           "steps_where": where, "studies": studies,
           "gen_tps_median": sorted(gen)[len(gen) // 2] if gen else None}
    if started:
        out["started"] = started.isoformat(timespec="seconds")
    if started and ended:
        out["ended"] = ended.isoformat(timespec="seconds")
        out["duration_s"] = max(0, round((ended - started).total_seconds()))
    return out


def write_run_summary(run_dir: Path) -> None:
    """summary.json next to the transcript, so the dashboard reads one small
    file per run instead of re-parsing every transcript on every request."""
    try:
        started = datetime.strptime(run_dir.name.replace("run_", ""), "%Y%m%d_%H%M%S")
        lines = (run_dir / "transcript.jsonl").read_text(errors="replace").splitlines()
        summary = summarize_transcript(lines, started, datetime.now())
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    except Exception:
        pass


def run_agent(task: str, start_url: str, max_rounds: int, run_dir: Path = None,
              stop_event=None, messages: list = None, batch_fill: bool = False,
              wander_seconds: float = 0, skip_av: bool = False):
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

    `skip_av`, when true, abandons a study whose page needs audio, video, a
    webcam or a microphone, instead of relying on the task text to tell the
    model to avoid them. Either way, a study ruled out here is remembered in
    study_cache so later cycles do not open it again.
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
        _last_guarded_duration: float | None = None  # persists across clicks within a study entry
        _prev_was_guarded: bool = False  # tracks previous step's site so ANY action triggers timer
        _current_study_id: str | None = None  # id of the study being looked at / taken
        _current_study_title: str = ""  # its name, read on Prolific while we can still see it
        _seen_codes: list = []  # completion codes spotted on the study's own pages
        _used_run_js: bool = False  # whether run_js was used during this cycle
        _survey_url: str = ""  # URL of the external survey (for study_memory)

        for step in range(1, max_rounds + 1):
            if stop_event is not None and stop_event.is_set():
                yield {"type": "done", "answer": "STOPPED", "messages": messages}
                return

            here = get_current_url()
            if not here:
                # An empty URL is not "no page": it is the bridge failing to
                # read one, and every rule below keys off this string. Read as
                # "not on Prolific" it silently switches off the guarded-site
                # rules, the wander guard, the study timer and the batch-fill
                # suppression, and the run carries on blind -- which is exactly
                # what happened with a tab whose renderer had wedged. Give the
                # page a few seconds (a navigation in flight reads empty too),
                # then hand the cycle back to the driver, which waits and
                # recovers the tab before retrying.
                for _ in range(3):
                    time.sleep(1.0)
                    here = get_current_url()
                    if here:
                        break
            if not here:
                raise BrowserStateError(
                    "the browser bridge reports no URL for the active tab "
                    "(wedged renderer or lost CDP connection)")
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

            # Back on Prolific with the completion code in the URL: the
            # submission is already made, so finish here instead of asking the
            # model to read the code off the screen.
            cc = completion_code(here)
            if cc:
                capture_page("complete")
                answer = f"CODE: {cc}"
                transcript.write(json.dumps({
                    "step": step, "action_type": "terminate", "args": {"answer": answer},
                    "code_source": "url", "url": here}) + "\n")
                transcript.flush()
                _restore_user_speed()
                _close_extra_tabs()
                _notify_telegram(f"Fara studio completato ✅\n{answer} (dall'URL)\n"
                                 f"Ciclo: {run_dir.name if run_dir else '?'}")
                yield {"type": "notice", "text": f"Codice di completamento letto dall'URL: {cc}",
                       "messages": messages}
                yield {"type": "done", "answer": answer, "messages": messages}
                return

            if batch_fill and not _on_guarded_site(here):
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

            # Stateful transition check: if previous step was on Prolific and now we're
            # external, start the timer regardless of HOW the navigation happened
            # (left_click, key press, Enter in a modal, JS redirect, etc.)
            here_guarded = _on_guarded_site(here)
            _timer_started_this_step = False
            if _prev_was_guarded and not here_guarded and not study_timer.is_active():
                effective_duration = _last_guarded_duration
                if effective_duration is not None:
                    study_timer.start(effective_duration * 60)
                    _live({"type": "timer_start", "min_seconds": study_timer.min_seconds,
                           "min_minutes": effective_duration})
                else:
                    study_timer.start(0)
                    _live({"type": "timer_start", "min_seconds": 0, "min_minutes": 0})
                _timer_started_this_step = True
                _survey_url = here
                _live({"type": "notice", "text": f"[timer] transition detected: guarded→external | duration={effective_duration}min"})
                dur_str = f" (~{effective_duration:.0f} min)" if effective_duration else ""
                _notify_telegram(f"📝 Studio iniziato{dur_str}\n{here}")
            _prev_was_guarded = here_guarded

            # Reset timer when back at the studies list (timer re-arms for next study).
            on_list = here_guarded and bool(_LIST_PATH.match(urlparse(here).path or ""))
            if on_list:
                study_timer.reset()
                _restore_user_speed()
                _last_guarded_duration = None
                _prev_was_guarded = False
                _current_study_id = None
                _current_study_title = ""
                _seen_codes = []

            # --- studies that lead nowhere (and, opt-in, audio/video ones) ---
            # A study that is listed but not doable gets opened again on every
            # cycle: the model has no memory across cycles, so without this it
            # re-discovers the same dead end for as long as it stays listed.
            # The wording is only read on the study page, where it is a
            # description; on an external questionnaire only real media
            # elements count, because aborting a survey halfway on a false
            # positive costs much more than one wasted click.
            m_study = _STUDY_PATH.match(urlparse(here).path or "") if here_guarded else None
            if m_study:
                _current_study_id = m_study.group(1)
                capture_page("study")
                # Read the name here, on Prolific: once the agent is inside the
                # external questionnaire the page title is the survey tool's,
                # which is no use for telling the model what to skip.
                _current_study_title = _study_title() or _current_study_title

            skip_reason = ""
            if m_study:
                known = study_cache.entries().get(_current_study_id)
                if known:
                    # Opened again anyway (the title hint is only a hint): bounce
                    # out before the model spends steps rediscovering it.
                    skip_reason = known.get("reason", "scartato in un ciclo precedente")
                else:
                    page_text = _page_text()
                    skip_reason = _detect_dead_end(page_text)
                    if not skip_reason and skip_av:
                        av = _detect_av(page_text, check_text=True)
                        if av:
                            skip_reason = f"audio/video: {av}"
            elif skip_av and not here_guarded and study_timer.is_active():
                av = _detect_av(check_text=False)
                if av:
                    skip_reason = f"audio/video: {av}"

            if skip_reason:
                title = _current_study_title or _study_title()
                key = _current_study_id or title or here
                study_cache.mark(key, skip_reason, title=title, url=here)
                abandoned = study_timer.is_active()
                study_timer.reset()
                _restore_user_speed()
                _last_guarded_duration = None
                _current_study_id = None
                _current_study_title = ""
                try:
                    pw_bridge.goto(studies_url())
                    pw_bridge.wait_for_content()
                    _prev_was_guarded = True
                except Exception:
                    pass
                pending_user_response = (
                    f"That study was ruled out automatically ({skip_reason}) and you were "
                    "taken back to the list of studies. Do not open it again: pick another "
                    "study, or follow your task instructions if none is eligible.")
                label = f"«{title}»" if title else "lo studio aperto"
                link = (f"\nhttps://app.prolific.com/studies/{key}"
                        if _STUDY_PATH.match(f"/studies/{key}") else "")
                _notify_telegram(f"⏭ Studio saltato: {label}\n{skip_reason}{link}\n"
                                 "Se ti interessa, puoi farlo a mano.")
                injected = yield {"type": "notice",
                                  "text": (f"Studio saltato ({skip_reason}): {label}"
                                           + (", survey abbandonata" if abandoned else "")
                                           + ". Non verrà riaperto nei prossimi cicli."),
                                  "messages": messages}
                if injected:   # an instruction typed while this was happening
                    pending_user_response += "\n\n" + injected

            # Codes shown on the study's own pages, collected as they appear:
            # by the time the model reports one it is back on Prolific, where
            # the original page can no longer be checked.
            if study_timer.is_active() and not here_guarded:
                for c in codes_in_url(here) + codes_on_page(_end_page_text()):
                    if c not in _seen_codes:
                        _seen_codes.append(c)

            skip_hint = study_cache.prompt_line() if on_list else ""
            nav_hint = ""
            mem_hint = ""
            if not on_list:
                nav_hint = _dom_nav_hint()
                if not here_guarded and study_timer.is_active():
                    mem_hint = study_memory.hint_for(here)

            _live({"type": "phase", "phase": "observe", "step": step})
            img = take_screenshot(run_dir, step)
            data_url = encode_for_model(img)

            if pending_user_response:
                text = pending_user_response
                pending_user_response = ""
            elif step == 1:
                resume_hint = ""
                if not on_list:
                    resume_hint = f"\n\nCurrent URL: {here}\n"
                    if here_guarded and m_study:
                        resume_hint += (
                            "You are resuming on a Prolific study page. A previous run was "
                            "working on this study. Continue it — open the survey if needed, "
                            "or enter the completion code if the survey is done. "
                            "Do NOT go back to the studies list.")
                    else:
                        resume_hint += (
                            "You are resuming inside an external survey. A previous run was "
                            "working through it. Continue where you left off — complete the "
                            "current question and proceed. Do NOT navigate back to Prolific "
                            "until the survey is complete.")
                text = task + _standing_rules() + (f"\n\n{skip_hint}" if skip_hint else "") + resume_hint
            else:
                url = get_current_url()
                prefix = f"Current URL: {url}\n" if url else ""
                prefix += skip_hint + nav_hint + mem_hint
                timer_line = study_timer.status_line()
                if timer_line:
                    prefix += timer_line + "\n"
                    _live({"type": "timer", "elapsed": study_timer.elapsed(),
                           "remaining": study_timer.remaining(),
                           "min_seconds": study_timer.min_seconds})
                    if study_timer.min_seconds > 0:
                        over = study_timer.elapsed() - study_timer.min_seconds
                        if over > 120:
                            _auto_escalate("super_fast")
                        elif over > 0:
                            _auto_escalate("hurry")
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
                if pre_click_duration is not None:
                    _last_guarded_duration = pre_click_duration
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

            if action_type == "run_js" and not result_desc.startswith("run_js error"):
                _used_run_js = True

            if action_type == "left_click" and not pending_user_response:
                coord = args.get("coordinate")
                if coord and _check_disabled_near(coord[0], coord[1]):
                    pending_user_response = (
                        "The button you clicked appears to be DISABLED (greyed out). "
                        "This usually means a required field on the page has not been "
                        "filled in yet. Look carefully at the page for any unanswered "
                        "questions, unselected radio buttons, empty required fields, "
                        "or unchecked mandatory checkboxes. Fill them in first, then "
                        "try clicking the button again.")


            # --- diagnostic log entry (separate from model transcript) ---
            diag: dict = {
                "type": "diag", "step": step,
                "url": here,
                "guarded": here_guarded,
                "prev_guarded": _prev_was_guarded,  # value BEFORE this step updated it
                "action": action_type,
                "result": result_desc,
                "timer_active": study_timer.is_active(),
                "timer_elapsed": round(study_timer.elapsed(), 1) if study_timer.is_active() else None,
                "timer_remaining": round(study_timer.remaining(), 1) if study_timer.is_active() else None,
                "timer_min_sec": study_timer.min_seconds if study_timer.is_active() else None,
                "model": last_timing or None,
                "last_guarded_duration": _last_guarded_duration,
            }
            if action_type == "left_click" and here_guarded:
                diag["pre_click_duration"] = pre_click_duration
            diag["timer_started"] = _timer_started_this_step
            transcript.write(json.dumps(diag) + "\n")
            transcript.flush()
            # ---------------------------------------------------------------

            step_event = {
                "type": "step", "step": step, "thoughts": thoughts,
                "action_type": action_type, "args": args,
                "result_desc": result_desc, "screenshot": screenshot_path,
                "messages": messages,
            }

            if action_type == "terminate":
                refusal = terminate_refusal(study_timer)
                if refusal:
                    pending_user_response = refusal
                    continue
                yield step_event
                answer = args.get("answer", "")
                _restore_user_speed()
                _close_extra_tabs()
                check = None
                if answer.startswith("CODE:") or answer == "NOCODE":
                    # Compare what the model reported with every code seen on
                    # the study's pages, plus the one Prolific shows after the
                    # submission ("Completion code: PLKXLQ"). Never overrides
                    # the model: it only flags what needs a human look.
                    seen = _seen_codes + [c for c in codes_on_page(_end_page_text())
                                          if c not in _seen_codes]
                    check = check_code(answer, seen)
                    if check in ("mismatch", "missed"):
                        what = ("ha chiuso senza codice" if check == "missed"
                                else f"ha inserito {answer.split(':', 1)[1].strip()}")
                        _notify_telegram(f"⚠️ Codice da verificare: il modello {what}, "
                                         f"ma sulle pagine dello studio c'era "
                                         f"{', '.join(seen[:3])}.\n{here}")
                transcript.write(json.dumps({"step": step, "type": "diag", "code_source": "model",
                                             "answer": answer, "code_check": check,
                                             "codes_seen": _seen_codes[:5]}) + "\n")
                transcript.flush()
                if answer.startswith("CODE:") or answer == "NOCODE":
                    _notify_telegram(f"Fara studio completato ✅\n{answer}\nCiclo: {run_dir.name if run_dir else '?'}")
                if _survey_url:
                    study_memory.record(_survey_url, run_js_used=_used_run_js,
                                        steps=step, outcome=answer[:20])
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
        write_run_summary(run_dir)
