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
import math
import random
import shutil
import subprocess
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse, urlunparse
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
import sites

FARA_URL   = os.environ.get("FARA_URL",   "http://127.0.0.1:8090/v1/chat/completions")
FARA_MODEL = os.environ.get("FARA_MODEL", "Fara1.5-27B-Q4_K_M.gguf")
FARA_API_KEY = os.environ.get("FARA_API_KEY", "")
# Deterministic stub model, used only by gui_live_e2e so the end-of-run GUI
# state (feed row, live card cleared, "DONE-STUB" answer) can be asserted
# without depending on the real Fara model's non-deterministic output.
# Enabled only in test instances (FARA_TEST_INSTANCE=1) AND only when the
# task text starts with the "scripted" prefix the e2e uses.
FARA_TEST_INSTANCE = os.environ.get("FARA_TEST_INSTANCE", "") not in ("", "0")
_SCRIPTED_TASK_PREFIX = "do the scripted steps"

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


# The run's open transcript, so notices survive the run instead of existing
# only in the live SSE stream. The notices are the harness explaining itself
# -- "[timer] transition detected", "[guard] off the app host", "[launch]
# humanised launch: ..." -- and they were exactly what was needed to explain
# a bad run afterwards, yet they were lost the moment nobody was watching
# live. A 404 misread as a running study (run_20261108_154719) was caught
# only because a monitor happened to be attached at that minute.
_live_sink = None


def _live(msg: dict):
    sink = _live_sink
    if sink is not None and msg.get("type") in ("notice", "timer_start", "error"):
        try:
            sink(msg)
        except Exception:
            pass
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
    return subprocess.run(
        ["docker", "exec", "-e", "LANG=it_IT.UTF-8", "-e", "LC_ALL=it_IT.UTF-8", CONTAINER, *args],
        capture_output=True, text=True, check=True,
    )


# ── Human-like mouse movement ──────────────────────────────────────
def _get_cursor_pos() -> tuple[int, int]:
    """Current cursor position via xdotool."""
    try:
        out = docker_exec("xdotool", "getmouselocation", "--shell")
        vals = {}
        for line in out.stdout.strip().split("\n"):
            k, _, v = line.partition("=")
            vals[k] = int(v)
        return vals.get("X", 0), vals.get("Y", 0)
    except Exception:
        return 0, 0


_chromium_window_id: str | None = None


def _chromium_window() -> str | None:
    """X window id of the kiosk Chromium (cached). Needed as `--window` on
    wheel-button clicks: Chromium with ozone-x11 and no window manager
    ignores synthetic button-4/5 events that aren't addressed to its
    specific window (measured on Xvfb; buttons 1-3 and mousemove work
    globally, only buttons 4/5 need the explicit target)."""
    global _chromium_window_id
    if _chromium_window_id is not None:
        return _chromium_window_id
    try:
        out = docker_exec("xdotool", "search", "--onlyvisible", "--name", "chromium")
        ids = out.stdout.strip().split()
        if ids:
            _chromium_window_id = ids[0]
    except Exception:
        pass
    return _chromium_window_id


def _wheel_click_args(button: str, repeat: int, delay_ms: int) -> list[str]:
    """Build an xdotool wheel-click arg list, targeted at Chromium if we
    can find it. Falls back to untargeted click on legacy stacks that
    accept it (production kiosk pre-Ozone)."""
    base = ["click", "--repeat", str(repeat), "--delay", str(delay_ms), button]
    wid = _chromium_window()
    if wid:
        return ["click", "--window", wid, "--repeat", str(repeat),
                "--delay", str(delay_ms), button]
    return base


def _bezier_path(x0, y0, x1, y1):
    """Generate a list of (x, y) waypoints along a quadratic Bézier curve
    with a random control point (simulates natural hand arc)."""
    dist = math.hypot(x1 - x0, y1 - y0)
    n_points = max(8, min(45, int(dist / 15) + random.randint(3, 10)))

    # Control point: offset perpendicular to the line, random side & magnitude
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    if dist > 1:
        nx, ny = -(y1 - y0) / dist, (x1 - x0) / dist
    else:
        nx, ny = 0, 1
    bulge = random.gauss(0, dist * 0.15)
    cx, cy = mx + nx * bulge, my + ny * bulge

    points = []
    for i in range(n_points + 1):
        t = i / n_points
        bx = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t ** 2 * x1
        by = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t ** 2 * y1
        # Gaussian jitter, smaller near endpoints for natural easing
        ease = 4 * t * (1 - t)  # peaks at 0.5
        jx = random.gauss(0, 0.8 * ease + 0.2)
        jy = random.gauss(0, 0.8 * ease + 0.2)
        points.append((int(round(bx + jx)), int(round(by + jy))))
    # Ensure we end exactly on target
    points[-1] = (int(round(x1)), int(round(y1)))
    return points


def _human_move(x: int, y: int):
    """Move cursor to (x, y) along a natural-looking Bézier curve."""
    cx, cy = _get_cursor_pos()
    path = _bezier_path(cx, cy, x, y)
    # Single xdotool invocation with chained mousemove+sleep commands.
    args = []
    for i, (px, py) in enumerate(path):
        args += ["mousemove", str(px), str(py)]
        if i < len(path) - 1:
            t = i / max(1, len(path) - 1)
            ease = 1.0 + 2.0 * (1 - 4 * (t - 0.5) ** 2)
            delay = random.uniform(0.003, 0.012) * ease
            args += ["sleep", f"{delay:.4f}"]
    docker_exec("xdotool", *args)


_TYPO_NEIGHBOURS = {
    "a": "sqwz", "b": "vnhg", "c": "xvdf", "d": "sferc", "e": "wrsd",
    "f": "dgrvc", "g": "fhtbv", "h": "gjybn", "i": "uojk", "j": "hknum",
    "k": "jlmi", "l": "kop", "m": "nj,k", "n": "bmhj", "o": "ipkl",
    "p": "ol[;", "q": "wa12", "r": "etdf", "s": "awedxz", "t": "ryfg",
    "u": "yihj", "v": "cbgf", "w": "qesa", "x": "zcds", "y": "tuhg",
    "z": "asx",
}


def _human_type(text: str):
    """Type text character by character with human-like timing.

    `type` in xdotool chained mode consumes ALL subsequent args as text
    unless `--args 1` is passed — without it, every `sleep T` and the
    following `type --clearmodifiers X` get typed as literal text into
    the field.

    Reliability on Italian/accented text requires `setxkbmap it` in the
    container entrypoint: with the default `us` layout, xdotool must
    dynamically map à/è/é/ì/ò/ù to free keysym slots, which races the
    keypress and drops the char ~70% of the time on Xvfb."""
    if not text:
        return
    # NB: no fast path for short text. A single `xdotool type` fires its
    # keystrokes at a uniform cadence with no variance, which is exactly the
    # "typing speed consistent with software, not a human" signal Prolific's
    # bot check looks for. Short answers (an age like "34", a ZIP, yes/no, a
    # single word) are the common case in surveys, so they must be typed with
    # the same per-key human timing as long ones -- go through the loop below
    # for ANY length.
    # Single xdotool invocation: chain type + sleep per character.
    args = []
    n = len(text)
    for i, ch in enumerate(text):
        # Typo + backspace. Only on an alpha char not at the very start/end,
        # with another alpha just before and after (mid-word), so a stray
        # Backspace cannot delete into a field label we did not type.
        if (0 < i < n - 1
                and ch.lower() in _TYPO_NEIGHBOURS
                and text[i-1].isalpha()
                and text[i+1].isalpha()
                and random.random() < 0.015):
            wrong = random.choice(_TYPO_NEIGHBOURS[ch.lower()])
            if ch.isupper():
                wrong = wrong.upper()
            args += ["type", "--args", "1", "--clearmodifiers", wrong]
            # Noticing the mistake: a short pause before fixing it.
            args += ["sleep", f"{random.uniform(0.15, 0.40):.4f}"]
            args += ["key", "BackSpace"]
            args += ["sleep", f"{random.uniform(0.05, 0.15):.4f}"]
        args += ["type", "--args", "1", "--clearmodifiers", ch]
        if i < n - 1:
            delay = random.uniform(0.03, 0.12)
            if ch in " \t":
                delay += random.uniform(0.05, 0.25)
            elif ch in ".,;:!?\n":
                delay += random.uniform(0.08, 0.35)
            if random.random() < 0.04:
                delay += random.uniform(0.2, 0.6)
            args += ["sleep", f"{delay:.4f}"]
    docker_exec("xdotool", *args)


def _wheel_scroll(down: bool, ticks: int):
    """A real mouse-wheel scroll (xdotool), so batch-fill brings fields into
    view the way the vision path does -- never JS scrollIntoView."""
    btn = "5" if down else "4"
    docker_exec("xdotool", *_wheel_click_args(btn, max(1, int(ticks)), random.randint(25, 60)))


# Hand batch-fill (form_fill_pw) the real xdotool input backend so it drives the
# same cursor the vision path uses, never CDP Input.dispatch* or DOM writes
# (which behavioural fraud scoring flags). Defined here, after the primitives
# above exist; form_fill is form_fill_pw, imported at the top with no cycle back.
form_fill.set_real_input({
    "move": _human_move,
    "click": lambda: docker_exec("xdotool", "click", "1"),
    "wheel": _wheel_scroll,
    "type": _human_type,
    "key": lambda keys, repeat=1: docker_exec(
        "xdotool", "key", "--clearmodifiers", "--repeat", str(max(1, int(repeat))), keys),
})




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


def _scripted_stub_response(messages: list, step: int | None) -> str:
    """Deterministic response for the e2e scripted session.

    Emits token deltas through `_live` so the live card streams gradually
    (gui_live_e2e C2 wants >=6 distinct lengths during 'think'), then
    returns a tool_call. Step 1-3 click/scroll, step 4 terminates with
    answer DONE-STUB. The click coordinate is picked inside the fixture
    page's viewport so C3 (marker alignment) can match it."""
    # Count how many prior model turns we have had -- the test re-uses one
    # process for several scripted runs, so a module-level counter would
    # drift between runs; derive it from the message history instead.
    turns = sum(1 for m in messages if m.get("role") == "assistant")
    thinking = (
        "Looking at the current page, I can see the test fixture with three "
        "coloured buttons. I will interact with it step by step: first click, "
        "then scroll to inspect the rest, then finish once the scripted sequence "
        "is complete.")
    # Stream the thinking in small chunks so the live card shows gradual growth
    # (and the agent's ambient "think" phase is observed by the test).
    acc = ""
    for chunk in re.findall(r".{1,24}(?:\s+|$)", thinking):
        acc += chunk
        try:
            _live({"type": "token", "step": step, "text": chunk})
        except Exception:
            pass
        time.sleep(0.03)
    head = f"<think>\n{thinking}\n</think>\n\n"
    if turns == 0:
        tool = ('{"name":"computer_use","arguments":'
                '{"action":"left_click","coordinate":[520,300]}}')
    elif turns == 1:
        tool = ('{"name":"computer_use","arguments":'
                '{"action":"scroll","pixels":-200}}')
    elif turns == 2:
        tool = ('{"name":"computer_use","arguments":'
                '{"action":"left_click","coordinate":[520,420]}}')
    else:
        tool = ('{"name":"computer_use","arguments":'
                '{"action":"terminate","answer":"DONE-STUB"}}')
    return head + f"<tool_call>\n{tool}\n</tool_call>"


def _is_scripted_test_session(messages: list) -> bool:
    """True if the current task is the e2e scripted-steps session."""
    if not FARA_TEST_INSTANCE:
        return False
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content") or ""
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        if _SCRIPTED_TASK_PREFIX in str(c).lower():
            return True
    return False


def call_fara(messages: list, step: int = None) -> str:
    if _is_scripted_test_session(messages):
        return _scripted_stub_response(messages, step)
    payload = {
        "model": FARA_MODEL,
        "messages": messages,
        "temperature": 0,
        # hurry inherits what super_fast carried; super_fast caps generation
        # even lower because its no-thinking mode has nothing long to emit.
        "max_tokens": {"normal": 1024, "hurry": 384, "super_fast": 256}[_speed_mode],
    }
    # chat_template_kwargs is llama-server-only — skip when using a cloud API.
    # super_fast force-disables thinking regardless of ENABLE_THINKING, because
    # the whole point of this mode is a single-line thought + tool_call: a
    # 400-800 token <think> preamble per step burns prompt-cache and clock
    # time with no measured benefit on repetitive select+submit surveys.
    if not FARA_API_KEY:
        thinking_on = False if _speed_mode == "super_fast" else ENABLE_THINKING
        payload["chat_template_kwargs"] = {"enable_thinking": thinking_on}
    extra_hdrs = {"Authorization": f"Bearer {FARA_API_KEY}"} if FARA_API_KEY else None
    result = cancel.post_chat(FARA_URL, payload, timeout=120,
                              on_delta=lambda t: _live({"type": "token", "step": step, "text": t}),
                              extra_headers=extra_hdrs)
    _record_timing(result.get("timings") or {})
    return result["choices"][0]["message"]["content"]


def _lenient_parse_action(action_text: str) -> dict:
    """Best-effort extraction when a tool_call is neither valid JSON nor valid
    Python -- typically a long free-text field (a terminate `status`, an
    ask_user_question `question`) in which the model left quotes, newlines or
    markdown unescaped, which breaks both json.loads and ast.literal_eval and
    used to fail the whole cycle. Pulls the action type and structured args
    out by regex so a chatty summary can no longer take the step down; the
    free-text fields are captured loosely (inner quotes/newlines tolerated)
    since they are only used for reporting, never for control flow."""
    args: dict = {}
    m = re.search(r'"action"\s*:\s*"([^"\\]+)"', action_text)
    if m:
        args["action"] = m.group(1)
    m = re.search(r'"coordinate"\s*:\s*\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]',
                  action_text)
    if m:
        args["coordinate"] = [float(m.group(1)), float(m.group(2))]
    for k in ("time", "pixels", "amount", "clicks", "ticks"):
        m = re.search(r'"' + k + r'"\s*:\s*(-?\d+(?:\.\d+)?)', action_text)
        if m:
            args[k] = float(m.group(1)) if "." in m.group(1) else int(m.group(1))
    for k in ("direction", "keys", "code"):   # short, no inner quotes expected
        m = re.search(r'"' + k + r'"\s*:\s*"([^"\\]*)"', action_text)
        if m:
            args[k] = m.group(1)
    for k in ("text", "question", "status", "summary"):   # free text, lenient
        m = re.search(r'"' + k + r'"\s*:\s*"(.*)"\s*[},]', action_text, re.DOTALL)
        if m:
            args[k] = m.group(1)
    if "action" not in args:
        raise ValueError(f"no action extractable from tool_call: {action_text[:200]!r}")
    return {"arguments": args}


def parse_thoughts_and_action(message: str):
    """Port of Fara15Agent._parse_thoughts_and_action."""
    tmp = message.split("<tool_call>\n")
    if len(tmp) < 2:
        raise ValueError(f"No <tool_call> block in model output: {message!r}")
    thoughts = tmp[0].strip()
    action_text = tmp[1].split("\n</tool_call>")[0]
    try:
        action = json.loads(action_text, strict=False)
    except json.JSONDecodeError:
        try:
            action = ast.literal_eval(action_text)
        except (ValueError, SyntaxError):
            action = _lenient_parse_action(action_text)
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
# Speed modes shrink the budget so trimming kicks in sooner. Measured
# problem: with the flat 28000 budget, super_fast steady-state landed at
# ~27500 prompt tokens with cache_n 10240, meaning ~17000 fresh tokens
# every step = ~17s of prompt-eval alone at prompt_tps≈1000. Dropping to
# 12000 in super_fast makes the trim loop drop oldest text turns much
# earlier and keeps prompt processing in the 5-6s band.
_CONTEXT_BUDGETS = {
    "normal":     int(os.environ.get("FARA_TOKEN_BUDGET",       "28000")),
    # hurry now inherits what super_fast used to be: a 12k-token budget made
    # a 100-step run end with ~5s/step prompt processing (vs. ~17s on 28k).
    "hurry":      int(os.environ.get("FARA_TOKEN_BUDGET_HURRY", "12000")),
    # super_fast goes further still -- plus it disables Fara's <think>
    # preamble entirely (see call_fara) so each step emits ~1 line of
    # thought + tool_call instead of a 400-800 token reasoning block.
    "super_fast": int(os.environ.get("FARA_TOKEN_BUDGET_FAST",  "9000")),
}
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
# hurry inherits what super_fast used to carry; super_fast is tighter again.
_TRIM_MAX_IMAGES   = {"normal": 10, "hurry": 5, "super_fast": 3}
_TRIM_TO_IMAGES    = {"normal": 5,  "hurry": 2, "super_fast": 1}

# Speed-mode text compaction: once there are more than _MAX_THOUGHTS
# un-stripped past assistant turns, batch-strip all but the last
# _TRIM_TO_THOUGHTS down to just the <tool_call> block. Each turn's
# <think>...</think> preamble can be ~400-800 tokens; after ~80 steps
# that reasoning bloat alone was pushing prompt_n above 27k and
# costing ~17s per step. Batched like the image trim so a prefix-based
# KV cache invalidates once per cycle, not every step.
# hurry inherits what super_fast used to be. In super_fast, assistant turns
# emit a trivial thought block anyway (enable_thinking=False), so the trim
# matters less and the smaller numbers just keep the past turns compact.
_MAX_THOUGHTS      = {"normal": 999, "hurry": 6, "super_fast": 4}
_TRIM_TO_THOUGHTS  = {"normal": 999, "hurry": 3, "super_fast": 2}


def _current_budget() -> int:
    """Context-token budget for the current speed mode. Called at trim time
    (not at import) so changing speed mid-run takes effect right away."""
    return _CONTEXT_BUDGETS.get(_speed_mode, _CONTEXT_BUDGETS["normal"])


def _strip_old_thoughts(messages: list):
    """In speed modes, replace older assistant turns with just their
    <tool_call> block, dropping the <think> reasoning and the preamble
    the model wrote around it.

    Idempotent: a message that already starts with "<tool_call>" is
    left byte-identical so the KV-cache prefix match is preserved.
    Only kicks in when enough un-stripped turns have accumulated, so
    the cache invalidation happens in batches (same philosophy as the
    image trim just above)."""
    if _speed_mode == "normal":
        return
    max_t = _MAX_THOUGHTS.get(_speed_mode, 999)
    trim  = _TRIM_TO_THOUGHTS.get(_speed_mode, 999)
    uncompacted = [
        i for i, m in enumerate(messages)
        if m.get("role") == "assistant"
        and isinstance(m.get("content"), str)
        and "<tool_call>" in m["content"]
        and not m["content"].lstrip().startswith("<tool_call>")
    ]
    if len(uncompacted) <= max_t:
        return
    keep_from = len(uncompacted) - trim
    for i in uncompacted[:keep_from]:
        c = messages[i]["content"]
        messages[i]["content"] = "<tool_call>" + c.split("<tool_call>", 1)[1]


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


_COMPACT_RECAP_MARKER = "[CONTEXT COMPACTED]"


def _collect_recent_actions(messages: list) -> list[str]:
    """Pull short action signatures from assistant tool_calls across the
    current history, oldest first. Returns a trimmed list suitable for a
    recap line -- just the action types, no coordinates. Used by the hard
    compact so the model knows WHAT was done, even once the full turns are
    collapsed."""
    out: list[str] = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        c = m.get("content") or ""
        if not isinstance(c, str):
            continue
        # Tool call JSON lives inside <tool_call>...</tool_call>. Extract the
        # action name only; coordinates/text are irrelevant for the recap.
        mo = re.search(r'"action"\s*:\s*"([^"\\]+)"', c)
        if mo:
            out.append(mo.group(1))
    return out


def _hard_compact_context(messages: list, keep_recent: int = 2) -> int:
    """Collapse mid-history turns into ONE short recap, preserving the
    cache-stable prefix [system, persona] and the last `keep_recent` turns
    byte-identical.

    Why: `_trim_to_budget` used to `del messages[2]` iteratively, shifting
    every later token and invalidating llama-server's prompt cache mid-step.
    Measured in run_20261030_012624: cache_n crashed from 21212 to 1717
    every ~6 steps, prompt_n jumped to 14-17K, gen_tps collapsed from 33
    to 10. This replaces the iterative delete with a single, intentional
    collapse: the cache invalidates ONCE, the new short prefix stays stable,
    the next 10+ steps cache cheaply.

    Returns the token count after compacting (same contract as
    `_trim_to_budget`). A no-op if there's nothing worth collapsing
    (len(messages) <= 2 + keep_recent + 1)."""
    # Nothing to compact: just [sys, persona] + a few recent turns.
    min_before_recent = 3  # need at least sys + persona + 1 turn to collapse
    if len(messages) <= min_before_recent + keep_recent:
        return _real_token_count(messages)
    # Message to collapse: indices 2 .. len-keep_recent-1 inclusive.
    cut_start = 2
    cut_end = len(messages) - keep_recent     # exclusive
    if cut_end <= cut_start:
        return _real_token_count(messages)
    # Mine recent action signatures from the slice we're about to drop so
    # the model keeps a crude sense of what has already been tried.
    collapsed_slice = messages[cut_start:cut_end]
    actions = _collect_recent_actions(collapsed_slice)
    # Keep the recap tiny: at most the last 12 actions, joined, no coords.
    tail = actions[-12:] if len(actions) > 12 else actions
    recap_text = (f"{_COMPACT_RECAP_MARKER} {len(collapsed_slice)} earlier turns "
                  f"were collapsed to save tokens. Prior action sequence on this "
                  f"task: {', '.join(tail) if tail else '(no actions yet)'}. "
                  f"Continue from the current page shown below.")
    recap_turn = {"role": "user", "content": recap_text}
    messages[cut_start:cut_end] = [recap_turn]
    return _real_token_count(messages)


def _trim_to_budget(messages: list, budget: int | None = None,
                     max_images: int | None = None,
                     trim_to: int | None = None) -> int:
    """Brings `messages` under `budget`, verified against the real
    tokenizer at every stage rather than assumed. Returns the real token
    count after trimming (0 trims performed if already under budget)."""
    if budget is None:
        budget = _current_budget()
    # First pass: degrade history images in speed modes (cheap, no data lost)
    _degrade_history_images(messages)
    # Second pass: strip reasoning preambles from older assistant turns.
    # Idempotent and batched — see _strip_old_thoughts for the cache contract.
    _strip_old_thoughts(messages)

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

    # Still over budget: the bloat is coming from text, not images. Pin
    # index 0 (system prompt) AND index 1 (the step-1 user turn:
    # persona/profile + standing rules + identity hint). Dropping index 1
    # evicted the persona on long studies -- the model then no longer knew
    # demographics that ARE in the profile (age, nationality, Prolific ID)
    # and invented them, e.g. typing age 34 when the profile says 25,
    # contradicting Prolific prescreening and getting the submission flagged.
    #
    # Instead of `del messages[2]` iteratively (which shifted every later
    # token and invalidated llama-server's prompt cache on every call),
    # collapse the mid-history into a single recap turn ONCE. The cache is
    # invalidated once, the new prefix is stable, subsequent steps cache
    # cheaply. If after that one compact we still exceed the budget we send
    # slightly over rather than lose the persona or the latest page.
    if current > budget:
        current = _hard_compact_context(messages, keep_recent=2)

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


# -- the platform's URL model ------------------------------------------------
#
# Everything below asks the active site profile (sites.current) what its URLs
# mean. With the generic profile no URL belongs to a platform, so every one of
# these answers "no" and the platform-only branches of the loop stay off.


def _on_guarded_site(url: str) -> bool:
    """True when `url` belongs to the active platform (its domain or a
    subdomain). A third-party site -- the survey a task launches, a search
    result -- never does, so platform rules never touch it."""
    try:
        host = (urlparse(url or "").hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in sites.current.hosts)
    except Exception:
        return False


def is_forbidden_url(url: str) -> bool:
    """A section of the platform the agent must never be in (see the
    profile's forbidden_path). Enforced in code -- every step, every
    visit_url, every cycle restart -- not left to the model's judgement."""
    try:
        return _on_guarded_site(url) and bool(sites.current.forbidden_path.match(urlparse(url).path or ""))
    except Exception:
        return False


def _learn_app_host(url: str) -> None:
    """Adopt the start URL's host as the platform's app host, so a different
    deployment of the same platform needs no code change."""
    host = (urlparse(url or "").hostname or "").lower()
    if host and _on_guarded_site(url):
        sites.current.app_host = host


def _off_app_host(url: str) -> bool:
    """On the platform's domain but NOT on its app host (e.g. its marketing
    site): a different site that only shares the domain."""
    try:
        host = (urlparse(url or "").hostname or "").lower()
        app = sites.current.app_host
        return bool(host) and bool(app) and _on_guarded_site(url) and host != app
    except Exception:
        return False


def _unknown_app_path(url: str) -> bool:
    """A typed navigation to the app host that goes nowhere real.

    Clicking is unaffected -- this only rejects addresses the model composes
    itself, which it has been seen to invent from a task's name or from an
    id read elsewhere. A path that is not the list, not a task page and not
    one of the profile's app_visitable_prefixes is refused before it loads.
    """
    try:
        if not _on_guarded_site(url):
            return False
        site = sites.current
        host = (urlparse(url).hostname or "").lower()
        if not site.app_host or host != site.app_host:
            return False          # handled by the wrong-host guard
        path = urlparse(url).path or "/"
        norm = path.rstrip("/") or "/"
        if norm == "/" or site.home_path.match(path) or site.task_path.match(path):
            return False
        return not any(norm == p or norm.startswith(p + "/")
                       for p in site.app_visitable_prefixes)
    except Exception:
        return False


def _to_app_host(url: str) -> str:
    """Same path and query, moved onto the app host."""
    try:
        p = urlparse(url)
        return urlunparse(p._replace(netloc=sites.current.app_host))
    except Exception:
        return url


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
    if not _on_guarded_site(url):
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
    if not _on_guarded_site(url):
        return False
    site = sites.current
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
        if not _on_guarded_site(url) or not sites.current.complete_path.match(pr.path or ""):
            return ""
        cc = (parse_qs(pr.query).get(sites.current.complete_code_param) or [""])[0].strip()
        return cc if re.fullmatch(r"[A-Za-z0-9_\-]{2,64}", cc) else ""
    except Exception:
        return ""


# What the model writes in the code slot when it means "this study has no
# code". It is told to answer plain NOCODE, and it mostly does, but in a month
# of runs 15 cycles answered "CODE: NOCODE" instead.
_NOT_A_CODE_RE = re.compile(r"(no.?code|none|n/?a|nessun\w*|-+)\Z", re.IGNORECASE)


def completion_code_of(answer: str) -> str:
    """The real completion code in a terminate answer, or "".

    Taking the "CODE:" prefix as proof of a completion files a "CODE: NOCODE"
    answer as a successful submission carrying a copyable code that reads
    NOCODE, so the prefix is not enough: what follows it has to look like a
    code. Kept here, next to completion_code(), because the dashboard, the
    daily summary and the per-cycle Telegram ping must all agree on what
    counts as a completed study.
    """
    a = (answer or "").strip()
    if not a.upper().startswith("CODE:"):
        return ""
    code = a.split(":", 1)[1].strip()
    if not code or _NOT_A_CODE_RE.match(code):
        return ""
    return code


def studies_list_state() -> str:
    """What the studies list shows right now, read from the DOM, no model.

    "empty"   -- the list page showing the platform's "nothing yet" message
    "studies" -- the list page without it, i.e. something to look at
    "unknown" -- not on the list, logged out, page not readable...

    Only "empty" is ever trusted to skip the model. Anything else falls back
    to a normal cycle, so a layout change costs a model call, never a study.
    """
    url = get_current_url()
    if not url or not _on_guarded_site(url) or not sites.current.home_path.match(urlparse(url).path or ""):
        return "unknown"
    return list_state_from(url, _page_text(8000))


def list_state_from(url: str, text: str) -> str:
    """The decision behind studies_list_state(), on a URL and the page text."""
    if not url or not _on_guarded_site(url) or not sites.current.home_path.match(urlparse(url).path or ""):
        return "unknown"
    text = (text or "").lower()
    if not text.strip():
        return "unknown"
    if any(m in text for m in sites.current.empty_list_markers):
        return "empty"
    if sites.current.empty_list_re.search(text):
        return "empty"
    return "studies"


CAPTURE_DIR = Path(os.environ.get("FARA_CAPTURE_DIR",
                                  Path(__file__).resolve().parent.parent / "var" / "captures"))


def capture_page(kind: str) -> None:
    """Save the current page's HTML once per kind per day.

    Real platform pages (a list with tasks, a task page, the completion
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
    """Where to go back to when a study has to be abandoned (the platform's
    task list), or "" when the active site has no such list."""
    return sites.current.home_url()


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


def _orphan_reservation_visible() -> bool:
    try:
        return bool(_ORPHAN_BANNER_RE.search(_page_text(4000) or ""))
    except Exception:
        return False


def _reservation_nudge(fresh_unlaunched: bool, detail_id: str) -> str:
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


def _launch_control_visible() -> bool:
    """Is a way INTO the study still on the page? See
    pw_bridge.launch_control_present. Safe-false so a bridge hiccup never
    turns into a cancellation."""
    try:
        return bool(pw_bridge.launch_control_present())
    except Exception:
        return False


def _viewport_wh() -> tuple[int, int]:
    try:
        vp = pw_bridge.viewport_size() or {}
        return int(vp.get("width") or 0), int(vp.get("height") or 0)
    except Exception:
        return 0, 0


def _box_on_screen(box, vw: int, vh: int) -> bool:
    """Is the whole box inside the viewport? Unknown viewport => assume yes,
    so a failed measurement never blocks an otherwise good click."""
    if not box:
        return False
    if vw <= 0 or vh <= 0:
        return True
    return (box["x"] >= 0 and box["y"] >= 0
            and box["x"] + box["width"] <= vw
            and box["y"] + box["height"] <= vh)


_SCROLL_TICKS_PER_ROUND = 3
_SCROLL_TICK_BUDGET = 60


def _scroll_box_into_view(getter, max_ticks: int = _SCROLL_TICK_BUDGET):
    """Box from `getter()`, wheel-scrolled into view with REAL xdotool ticks if
    it starts off-screen. None if it cannot be brought in.

    Prolific's 'Start study' dialog is taller than this 900px kiosk viewport,
    so its confirm link sits below the fold. It DOES scroll -- but barely: in
    run_20261106_124620 four wheel ticks moved it 36px (measured by diffing
    that run's screenshot_06 against screenshot_07), so a fixed handful of
    ticks falls hundreds of pixels short, which is why the model gave up and
    cancelled a GBP 2,60 study. Hence progress-driven rather than round-
    capped: keep scrolling while the box is still moving, stop after two
    consecutive rounds that move it nothing (the caller's keyboard fallback
    then takes over), and bound the total ticks.

    The wheel, not JS scrollIntoView, so this stays on the same input path as
    the vision loop."""
    vw, vh = _viewport_wh()
    box = getter()
    spent = 0
    stalled = 0
    while box is not None and spent < max_ticks:
        if _box_on_screen(box, vw, vh):
            return box
        down = box["y"] >= max(1, vh) / 2
        ax = max(1, min((vw or 1440) - 2, int(box["x"] + box["width"] / 2)))
        ay = max(1, min((vh or 900) - 2, int((vh or 900) / 2)))
        _human_move(ax, ay)
        _wheel_scroll(down, _SCROLL_TICKS_PER_ROUND)
        spent += _SCROLL_TICKS_PER_ROUND
        cancel.sleep(random.uniform(0.15, 0.35))
        nxt = getter()
        if nxt is None:
            return None
        # Moving at all (however little) means scrolling still has somewhere
        # to go; two dead rounds mean it has not.
        stalled = 0 if abs(nxt["y"] - box["y"]) >= 1 else stalled + 1
        box = nxt
        if stalled >= 2:
            break
    return box if _box_on_screen(box, vw, vh) else None


def _box_into_view(selector: str, max_ticks: int = _SCROLL_TICK_BUDGET):
    return _scroll_box_into_view(
        lambda: pw_bridge.element_box(selector, scroll=False), max_ticks)


def _humanized_click_box(box, pre=(0.25, 0.7), settle=(0.05, 0.18)) -> bool:
    """Human-like click at a random point inside a viewport box (as returned by
    pw_bridge.element_box): dwell, Bezier cursor move with aim jitter, click.
    Uses the same real xdotool cursor as the vision path -- never CDP
    Input.dispatch* or DOM .click(), which behavioural fraud scoring flags.

    Returns False without clicking when the box lies outside the viewport: an
    off-screen bounding box would send the cursor to a point the user cannot
    see, which lands on whatever is at the clamped screen edge."""
    vw, vh = _viewport_wh()
    if not _box_on_screen(box, vw, vh):
        return False
    x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
    ax = round(x + random.gauss(0, 1.4))
    ay = round(y + random.gauss(0, 1.4))
    cancel.sleep(random.uniform(*pre))
    _human_move(ax, ay)
    cancel.sleep(random.uniform(*settle))
    docker_exec("xdotool", "click", "1")
    return True


def _humanized_launch_reserved_study(include_take_part: bool = True) -> dict:
    """Launch the external survey of a Prolific study the model has opened,
    with humanised xdotool clicks grounded on stable data-testid selectors:
    (optionally) 'Take part in this study' to reserve -> 'Open study in new
    window' -> tick the dialog checkbox(es) -> 'Start study', then follow the
    survey tab. The model chose the study; this only performs the mechanical
    launch it keeps fumbling (the confirm link sits below the dialog fold).

    Returns {'status': ...}. Never raises (except on cancellation). status:
      launched           -- survey tab is open and now the active/front page
      no-reserve-control -- un-reserved but no 'Take part' button found
      no-launch-control  -- reserved but no 'Open study in new window'
      no-dialog          -- 'Open' clicked but the Start dialog never appeared
      no-start-control   -- dialog open but no 'Start study' link
      clicked-no-tab     -- Start clicked but no survey tab appeared
      error              -- unexpected failure (detail included)

    Every control is wheel-scrolled into view before it is clicked
    (_box_into_view); the dialog is taller than the kiosk viewport, so the
    confirm link is usually below the fold. When even scrolling cannot reveal
    it (the page behind is already at its scroll end, as in
    run_20261106_124620), the link is focused and activated with a real Enter.
    """
    try:
        pw_bridge.front()   # pin the study tab to the foreground for xdotool
        # 1. Reserve, if still on the un-reserved detail page.
        if (include_take_part
                and not pw_bridge.has_selector(pw_bridge._SEL_OPEN_DIALOG)
                and not pw_bridge.has_selector(pw_bridge._SEL_DIALOG)):
            rbox = _box_into_view(pw_bridge._SEL_RESERVE)
            if rbox is None or not _humanized_click_box(rbox):
                return {"status": "no-reserve-control"}
            pw_bridge.wait_selector(pw_bridge._SEL_OPEN_DIALOG, 9000)
        # 2. Open the 'Start study' dialog.
        if not pw_bridge.has_selector(pw_bridge._SEL_DIALOG):
            obox = _box_into_view(pw_bridge._SEL_OPEN_DIALOG)
            if obox is None or not _humanized_click_box(obox):
                return {"status": "no-launch-control"}
            if not pw_bridge.wait_selector(pw_bridge._SEL_DIALOG, 6000):
                return {"status": "no-dialog"}
        # 3. Tick every unchecked checkbox (device + any confidentiality one);
        #    the 'Start study' link stays disabled until they are ticked. Taken
        #    one at a time and re-queried, because ticking one can reflow the
        #    dialog and invalidate the boxes measured for the others.
        def _first_unchecked():
            boxes = pw_bridge.unchecked_checkbox_boxes(
                pw_bridge._SEL_DIALOG, scroll=False)
            return boxes[0] if boxes else None

        for _ in range(6):
            if _first_unchecked() is None:
                break
            cbox = _scroll_box_into_view(_first_unchecked)
            if cbox is None or not _humanized_click_box(cbox, pre=(0.2, 0.5)):
                break
            cancel.sleep(random.uniform(0.15, 0.4))
        # 4. Activate 'Start study' and follow the survey tab it opens.
        before = pw_bridge.page_ids()
        sbox = _box_into_view(pw_bridge._SEL_START)
        clicked = bool(sbox) and _humanized_click_box(sbox)
        if not clicked:
            # Nothing scrolls and the confirm link stays off-screen. Focus it
            # in the DOM and press Enter with a REAL xdotool keystroke: the
            # activation event is genuine, only the focus is programmatic.
            if not pw_bridge.focus_element(pw_bridge._SEL_START):
                return {"status": "no-start-control"}
            cancel.sleep(random.uniform(0.2, 0.5))
            docker_exec("xdotool", "key", "--clearmodifiers", "Return")
        surl = pw_bridge.follow_new_tab(before, 9000)
        if surl:
            return {"status": "launched", "survey_url": surl}
        return {"status": "clicked-no-tab"}
    except cancel.Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "detail": repr(e)}


_WS_RE = re.compile(r"\s+")


# A researcher's own words when their automatic checks reject the work that was
# just submitted. These pages are the only place the verdict ever appears: the
# study still returns a completion code, Prolific still shows "awaiting review",
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
    return _WS_RE.sub(" ", text[start:m.end() + 110]).strip()[:220]


def _page_fingerprint(limit: int = 8000) -> str:
    """Normalized visible page text PLUS the srcs of the visible images, used
    by _page_change_hint to tell the model how much its previous action
    changed the page.

    Problem it solves: on multi-trial surveys the URL stays the same when
    "Submit" advances to the next trial. A binary "same/different" hash
    was not enough: left_click on an option also changes the text
    (adds "Choice 1 selected"), which the binary signal flagged as
    "page CHANGED → navigation" and the model happily accepted even when
    it was still on the same trial. Returning the normalized text lets
    the hint compute token-level similarity and distinguish minor UI
    updates from real navigation.

    innerText is text only, so an image-comparison trial that swaps its
    images but keeps the same layout labels (Select 1/2, Submit, ...) read as
    "unchanged" and the model thought it was stuck. The srcs of the visible
    images are appended (query strings stripped so cache-busters don't churn,
    tiny icons skipped) so an image swap registers as a real content change.

    Whitespace is collapsed so reflow-only differences (window resize,
    tooltip ghost, cursor-induced :hover text) don't register as changes.
    """
    _JS = r"""(limit) => {
        const txt = (document.body ? document.body.innerText : '').slice(0, limit);
        const imgs = [];
        for (const el of document.querySelectorAll('img')) {
            const r = el.getBoundingClientRect();
            if (r.width < 32 || r.height < 32) continue;       // skip icons/pixels
            let s = el.currentSrc || el.src || '';
            if (!s || s.startsWith('data:')) continue;
            imgs.push(s.split('?')[0]);                        // drop cache-busters
            if (imgs.length >= 30) break;
        }
        return txt + ' ' + imgs.join(' ');
    }"""
    try:
        out = pw_bridge.run(lambda b: b.active_page().evaluate(_JS, limit)) or ""
        return _WS_RE.sub(" ", out).strip()
    except Exception:
        # Fall back to text only if the combined read fails.
        try:
            return _WS_RE.sub(" ", _page_text(limit)).strip()
        except Exception:
            return ""


def _text_similarity(a: str, b: str) -> float:
    """Jaccard over the token sets. Cheap (O(n+m)), robust to word order,
    good enough to tell "same page with one new label" from "whole new
    screen." Returns 1.0 when both sides are empty."""
    sa = set(a.split())
    sb = set(b.split())
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# -- what the study has already shown -------------------------------------
#
# run_20261113_114533 answered "Health and well-being" to "Thinking back to
# the shopping scenario, the family member's main goal was primarily...".
# The scenario, six pages earlier, said the opposite in bold three times:
# "a pleasurable eating experience", "prioritising enjoyment and pleasure".
# Batch-fill reads the page in front of it and nothing else, and the
# screenshot of a page we left is not an answer either once context is
# trimmed. So the gist of those pages is kept for the length of the study.
_URLISH_RE = re.compile(r"\bhttps?://\S+")
_STUDY_PAGE_CHARS = 700
_STUDY_PAGES_KEPT = 4


def _remember_study_page(pages: list, fp: str) -> None:
    """Keep the gist of a page this study has already shown.

    Only pages the vision step handles ever reach here: one with a fillable
    form is taken by batch-fill, which returns before this point. What
    accumulates is therefore the set worth keeping -- the scenario, the
    instructions, the stimulus -- and never the question pages themselves.
    """
    text = _URLISH_RE.sub("", fp or "").strip()[:_STUDY_PAGE_CHARS]
    if len(text) < 40:
        return
    for old in pages:
        if _text_similarity(old, text) > 0.8:
            return                      # same page again (a scroll, a hover)
    pages.append(text)
    del pages[:-_STUDY_PAGES_KEPT]


_PROGRESS_RE = re.compile(r"\b(\d{1,3})\s*/\s*(\d{1,3})\b")


def _progress_counter(fp: str):
    """The first 'N/M' progress counter in the page text (e.g. 4/35), as
    (n, m), or None.

    Multi-trial surveys show one and advance it each trial while the URL and
    the rest of the layout stay identical. A change in it is a reliable
    "moved to the next item" signal that token similarity -- dominated by the
    unchanged layout (Select 1/2, Submit, ...) -- otherwise misses, reading
    two different trials as "mostly unchanged". The timer ("Time: 9:24") uses
    ':' so it never matches, and a static date like 10/27 never changes
    step-to-step, so only a real counter move triggers the signal."""
    m = _PROGRESS_RE.search(fp or "")
    if not m:
        return None
    n, tot = int(m.group(1)), int(m.group(2))
    if tot == 0 or n > tot:
        return None
    return (n, tot)


# Which action types might legitimately advance the page content. For
# scroll/mouse_move/wait, innerText is expected to stay the same, so
# emitting a "did not change" signal would be noise.
_ADVANCING_ACTIONS = frozenset(("left_click", "double_click", "triple_click",
                                "right_click", "key", "type"))


def _page_change_hint(prev_fp: str, prev_action: str, curr_fp: str) -> str:
    """One line injected into the next step's prompt. Empty when the
    previous action couldn't change the page or we have no baseline.

    Four tiers based on token-set Jaccard similarity:
      ==1.00  : identical → the action had no visible effect
      >=0.92  : near-identical → minor UI update (option highlighted,
                inline label, modal); NOT a navigation
      >=0.70  : partial change → ambiguous
      < 0.70  : major change → likely new view/trial
    The 0.92 cutoff is the one that distinguishes "clicked an option on
    the same trial" from "submitted and moved to the next trial", which
    is the concrete bug the hint has to fix.
    """
    if not prev_fp or not curr_fp or prev_action not in _ADVANCING_ACTIONS:
        return ""
    # A multi-trial progress counter (N/M) that moved is a definitive advance
    # to a new item, even though the layout text barely changed. Say the new
    # position explicitly so the model stops re-doing the previous trial --
    # the exact failure where it reads the small counter wrong and loops.
    prev_prog, curr_prog = _progress_counter(prev_fp), _progress_counter(curr_fp)
    if (prev_prog and curr_prog and prev_prog[1] == curr_prog[1]
            and prev_prog[0] != curr_prog[0]):
        return (f"[page-signal] You ADVANCED: the progress counter moved from "
                f"{prev_prog[0]}/{prev_prog[1]} to {curr_prog[0]}/{curr_prog[1]}. "
                f"Your previous {prev_action} registered and you are now on item "
                f"{curr_prog[0]} of {curr_prog[1]}. Do NOT redo the previous item; "
                "continue with this one.\n")
    if prev_fp == curr_fp:
        return (f"[page-signal] Your previous {prev_action}: page innerText is "
                "IDENTICAL (0% change). The action had no visible effect — try "
                "a different approach.\n")
    sim = _text_similarity(prev_fp, curr_fp)
    pct = int(round(sim * 100))
    if sim >= 0.92:
        return (f"[page-signal] Your previous {prev_action}: page is MOSTLY "
                f"unchanged ({pct}% similar). Likely a MINOR update (option "
                "highlighted, inline label, modal) — NOT a navigation. "
                "Verify the trial/question number on screen before assuming "
                "you advanced.\n")
    if sim >= 0.70:
        return (f"[page-signal] Your previous {prev_action}: page partially "
                f"changed ({pct}% similar). Content may have shifted but you "
                "may still be in the same view — verify from the screenshot.\n")
    return (f"[page-signal] Your previous {prev_action}: page content CHANGED "
            f"significantly ({pct}% similar) — likely a navigation to a new "
            "trial/question. Do not assume it failed just because the layout "
            "looks similar.\n")


# -- pre-screenshot auto-scroll: bring response controls into view -----------
#
# Observed on eval.services (and similar long-content survey pages): each
# trial starts with videos/images and context text, with the response
# buttons placed below one full viewport. The model needs 1-3 scroll
# steps per trial to find them -- each step is a full ~16s model
# round-trip. Measured: 1.43 pre-click scrolls/trial × 16s = ~23s wasted
# per trial just finding where to click.
#
# This JS pre-step moves the viewport (does not click or submit) so the
# screenshot the model sees already has selectable options in frame.
# It is idempotent: when a candidate is already in-view it returns
# 'in-view' and does nothing. When nothing matches (e.g. on the Prolific
# dashboard, or a non-survey page) it returns 'nothing' and does nothing.
# Selectors are narrow -- unselected radio/checkbox/aria-radio inputs and
# likert/choice/option/answer button classes -- so a stray page button
# (nav, menu, cookie banner) never wins.
_AUTOSCROLL_TO_CONTROLS_JS = r"""
(() => {
  const sel = [
    '.likert-btn:not(.selected):not(.is-selected):not(.is-active):not(.chosen):not(.checked)',
    'button[class*="choice"]:not([aria-pressed="true"]):not(.selected):not(.is-selected)',
    'button[class*="option"]:not([aria-pressed="true"]):not(.selected):not(.is-selected)',
    'button[class*="answer"]:not([aria-pressed="true"]):not(.selected):not(.is-selected)',
    'button[class*="likert"]:not(.selected):not(.is-selected)',
    'input[type="radio"]:not(:checked)',
    'input[type="checkbox"]:not(:checked)',
    '[role="radio"][aria-checked="false"]',
    '[role="checkbox"][aria-checked="false"]'
  ].join(',');
  const vh = window.innerHeight;
  let cands;
  try { cands = Array.from(document.querySelectorAll(sel)); }
  catch (e) { return 'error:' + e.message; }
  // Custom survey UIs (Tailwind/div-based, no radio semantics) expose their
  // choice controls only as <button>s whose TEXT is the action, e.g.
  // eval.services "Select (1)" / "Select (2)". Match those by label, but
  // never the image zoom/preview openers ("Open option 1 ... preview").
  try {
    const actionRe = /^\s*(select|choose|pick|vote|rate|confirm)\b/i;
    const previewRe = /\b(preview|zoom|expand|enlarge|open)\b/i;
    document.querySelectorAll('button, [role="button"]').forEach(e => {
      const t = (e.innerText || e.textContent || '').trim();
      if (t && actionRe.test(t) && !previewRe.test(t)) cands.push(e);
    });
  } catch (e) { /* keep the CSS-selector candidates */ }
  // An answered radio GROUP still leaves its other options unchecked, so
  // ':not(:checked)' kept matching them forever: the nudge re-centred a
  // question that was already answered on every single step, dragging the
  // viewport back the moment the model scrolled anywhere else. The
  // screenshot then never changed, the model read that as "the page will
  // not scroll, a required field must be hidden above" and
  // run_20261108_181732 burnt 20 steps (five minutes) on it before
  // cancelling a study it could have submitted. An answered group is not a
  // candidate.
  const answered = e => {
    try {
      if (e.type === 'radio' && e.name) {
        const esc = (window.CSS && CSS.escape) ? CSS.escape(e.name) : e.name;
        const root = e.form || document;
        return !!root.querySelector('input[type="radio"][name="' + esc + '"]:checked');
      }
      if (e.getAttribute && e.getAttribute('role') === 'radio') {
        const grp = e.closest('[role="radiogroup"]');
        return !!(grp && grp.querySelector('[role="radio"][aria-checked="true"]'));
      }
    } catch (err) { /* exotic name/selector: keep the candidate */ }
    return false;
  };
  cands = cands.filter(e => {
    if (!e.offsetParent || e.disabled) return false;
    if (answered(e)) return false;
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  if (cands.length === 0) return 'nothing';
  // If any candidate is already comfortably in the viewport, do nothing.
  // 'Comfortably' = top >= 0 and bottom <= viewport (fully inside).
  const inView = cands.find(e => {
    const r = e.getBoundingClientRect();
    return r.top >= 0 && r.bottom <= vh;
  });
  if (inView) return 'in-view';
  // Scroll the first (DOM-order) candidate to the vertical centre.
  // 'instant' is sync: the scroll position is updated before this returns.
  cands[0].scrollIntoView({ block: 'center', behavior: 'instant' });
  return 'scrolled';
})();
"""


def _prescroll_to_controls() -> str:
    """Playwright-driven viewport nudge, run just before each screenshot in
    speed modes while a study is active. Returns the JS result tag
    ('scrolled' / 'in-view' / 'nothing' / 'error:...') or '' on failure --
    the caller treats '' and 'nothing' / 'in-view' identically (no settle
    wait needed). Any exception becomes '': the agent falls back to the
    old behaviour (model scrolls itself) rather than failing the step."""
    try:
        return pw_bridge.evaluate(_AUTOSCROLL_TO_CONTROLS_JS) or ""
    except Exception:
        return ""


# -- auto-chain Enter after left_click when Enter DIRECTIVE is active --------
#
# On domains where the user has told the agent "after you select an
# option, just press Enter" (study_memory counts >= 1 Enter hints, same
# threshold that fires the Enter DIRECTIVE), we press Enter ourselves
# right after the click. Saves the full ~16s model round-trip that it
# would otherwise take for the model to look at the post-click
# screenshot and emit `key Return`.
#
# Gated hard so this never fires outside the intended pattern:
#   - URL's domain has >= 1 Enter hint (user asked for it explicitly)
#   - speed mode is not 'normal' (opt-in: 'normal' keeps old behaviour)
#   - study_timer is active (we are inside a Prolific study)
#   - click did not land on a disabled control (pending_user_response
#     branch handles that path; we skip auto-Enter there)
# Even then, pressing Enter on a page with no selection is a benign
# no-op on the surveys we have seen -- it either submits a selected
# option or does nothing. We never chain Enter on a non-click action.
def _should_auto_chain_enter(url: str) -> bool:
    if _speed_mode == "normal":
        return False
    try:
        if study_memory.enter_hint_count(url) < 1:
            return False
    except Exception:
        return False
    return True


# -- loop detector: break out of same-action-same-page repetition -------------
#
# Observed: an agent stuck on a validation error can emit the same
# "scroll up to find the missing required field" action 6-10 times in a
# row with no page change. The context trim keeps the per-step cost low
# but does not break the loop. _action_signature folds coordinates to
# a coarse grid so natural jitter is not counted as a new action; the URL
# PATH (not the full URL) is included so a hash fragment change (SPA tab
# switch) still looks like the same page. _stuck_warning emits a short
# prefix line when the last STUCK_THRESHOLD recorded signatures all match.
_STUCK_THRESHOLD = 3
# Scrolls get a higher threshold: the signature folds the magnitude away
# (see _action_signature) so a model hunting for an off-screen control by
# scrolling the same direction with varied tick counts reads as one
# repeated action. Measured: successful survey runs never exceed 2-3
# consecutive same-direction scrolls, while a wedged run did 39 in a row,
# so 5 catches the loop early without nagging normal long-page reading.
_SCROLL_STUCK_THRESHOLD = 5
_SCROLL_ACTIONS = ("scroll", "hscroll")


def _action_signature(action_type: str, args: dict, url: str) -> str:
    """Compact (url_path, action, bucketed_args) tuple as a string. The
    coordinate bucket (50px) absorbs the pw humanization jitter and small
    model drift so a human-level 'same click' still looks identical."""
    try:
        path = urlparse(url).path or ""
    except Exception:
        path = ""
    parts = [path, action_type]
    if action_type in _SCROLL_ACTIONS:
        # Direction only, NOT magnitude: a stuck model varies the tick count
        # each step (-590, -366, -120 ...) which used to make every scroll a
        # fresh signature and let an endless scroll-hunt dodge the detector.
        px = args.get("pixels")
        try:
            parts.append("up" if float(px) > 0 else "down")
        except (TypeError, ValueError):
            parts.append("?")
    elif action_type == "key":
        keys = args.get("keys")
        parts.append(",".join(str(k) for k in keys) if isinstance(keys, list) else str(keys))
    elif action_type == "type":
        parts.append(str(len(str(args.get("text", "")))))  # length only, no content
    elif args.get("coordinate"):
        c = args["coordinate"]
        parts.append(f"{int(c[0]) // 50},{int(c[1]) // 50}")
    return "|".join(parts)


def _stuck_warning(sigs: deque) -> str:
    """Prefix line for the model when it is clearly in a loop. Empty when
    there are fewer than the applicable threshold of signatures or they are
    not all equal. The warning names the repeated action so the model can
    tell which loop to break (vs. a generic 'try something different'
    nudge, which was ignored in prior runs).

    Scrolls use the higher _SCROLL_STUCK_THRESHOLD; everything else uses
    _STUCK_THRESHOLD."""
    if not sigs:
        return ""
    # Signature is path|action|arg. The threshold depends on the action of
    # the most recent signature.
    last_parts = sigs[-1].split("|")
    action = last_parts[1] if len(last_parts) > 1 else ""
    threshold = _SCROLL_STUCK_THRESHOLD if action in _SCROLL_ACTIONS else _STUCK_THRESHOLD
    if len(sigs) < threshold:
        return ""
    last = list(sigs)[-threshold:]
    if len(set(last)) != 1:
        return ""
    # Strip the path for the user-facing message (the model already sees the
    # URL on its own line).
    _, _, tail = last[0].partition("|")
    if action in _SCROLL_ACTIONS:
        return (f"[STUCK WARNING: you have scrolled ({tail}) {threshold} times "
                "in a row on this page without making progress. STOP "
                "scrolling. The control you are looking for is likely NOT "
                "reachable this way. Try: press End or Page Down to jump to "
                "the bottom, look for a required field ABOVE that is blocking "
                "submission, or if the page cannot be completed cancel/return "
                "the study. Do NOT scroll again.]\n")
    return (f"[STUCK WARNING: you have performed `{tail}` {threshold} "
            "times in a row on this page and nothing changed. STOP "
            "repeating it. Look at the screenshot and try a DIFFERENT "
            "approach: a different element, the opposite scroll direction, "
            "a different action type (key press, type, or terminate), or "
            "ask the user if truly stuck.]\n")


def _study_title() -> str:
    """Name of the study on the page being looked at, or "".

    Only used to tell the model which studies to skip: the list page shows
    names, never ids, so a cached entry without a title is useless to it.
    Returns "" on any non-study page so junk headings (the sidebar account
    name was ending up cached as the study title) can't poison the cache.
    """
    _JS = """() => {
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
    try:
        title = (pw_bridge.run(lambda b: b.active_page().evaluate(_JS)) or "").strip()[:100]
    except Exception:
        return ""
    # Belt-and-braces: common non-title strings that have slipped into the
    # cache before. Add anything else you see here as a false positive.
    low = title.lower()
    if not title or low in ("prolific", "studies", "my submissions"):
        return ""
    return title


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
        for pattern, reason in _DEAD_END_MARKERS:
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

    The clock runs only while the active tab LINGERS on the Prolific launch
    surface without a task running: a study detail page, account pages,
    results... It resets whenever the agent is on the list, or anywhere a
    study is actually in progress -- that must never be cut short. "In
    progress" is two cases, and both reset the clock:
      * an external survey host (off prolific.com), and
      * a Prolific-HOSTED task such as /data-collection-tool/..., which is
        still on prolific.com and is NOT the list, so the old
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


def safe_page_for(url: str) -> str:
    """The list of studies on the same site as `url`."""
    return sites.current.home_on(url)


def loop_home_for(url: str) -> str:
    """The page a loop cycle must return to between studies: always the
    Prolific studies list.

    A study detail page, a submission/completion page, or an external survey
    host (a finished study's own "thank you / Return to Prolific" page) is
    never a valid home. Returning there each cycle makes the loop re-open a
    completed study and re-read its code instead of looking for a new one.
    This also protects against the loop being (re)started while the browser
    sits on such a page: the home resolves to the studies list wherever it was
    started from, so only an actual guarded (Prolific) studies list is kept."""
    pr = urlparse(url or "")
    if _on_guarded_site(url) and sites.current.home_path.match(pr.path or ""):
        return safe_page_for(url)
    return studies_url()


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


_TIME_REMAINING_RE = re.compile(
    r"time\s+remaining\W{0,12}"
    r"(?:(\d+)\s*(?:hours?|hrs?|h)\W{0,4})?"
    r"(?:(\d+)\s*(?:minutes?|mins?|m)\W{0,4})?"
    r"(?:(\d+)\s*(?:seconds?|secs?|s)\b)?",
    re.IGNORECASE)


def _extract_time_remaining() -> float | None:
    """Seconds left on Prolific's own countdown for the reserved slot, read
    from the study detail page ("Time remaining  13 Mins 49 Secs"), or None.

    This is the deadline that decides whether the work is paid: overrun it
    and Prolific auto-returns the submission. Nothing used to read it -- the
    harness tracked only the MINIMUM duration -- so a study answered
    correctly but slowly was lost with no warning (run_20261106_154221,
    22m40s against a ~15m window; the page text is kept in
    var/fixtures/prolific/detail_reserved.mhtml).
    """
    try:
        m = _TIME_REMAINING_RE.search(_page_text(6000) or "")
        if not m or not any(m.groups()):
            return None
        h, mi, se = (float(g or 0) for g in m.groups())
        total = h * 3600 + mi * 60 + se
        return total if total > 0 else None
    except Exception:
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
        # Prolific's OWN countdown for the reserved slot ("Time remaining:
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
        """Seconds before Prolific takes the slot back, or None if unknown."""
        if self.start_time is None or self.deadline_seconds <= 0:
            return None
        return self.deadline_seconds - self.elapsed()

    def deadline_pressure(self) -> float | None:
        """0.0 at the start of the window, 1.0 at the deadline, >1 past it.
        None when Prolific showed no countdown."""
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
        """The hard half of the clock: how long before Prolific takes the slot
        back. Appended to the status line so the model sees both bounds, not
        only the minimum it must not undercut."""
        left = self.deadline_remaining()
        if left is None:
            return None
        if left <= 0:
            return (f" DEADLINE PASSED: Prolific's countdown for this slot ran "
                    f"out {abs(left) / 60:.0f} min ago. Submit whatever you can "
                    f"right now; do not start new sections.")
        mins = left / 60
        if mins <= 3:
            return (f" DEADLINE {mins:.1f} min away: Prolific returns this "
                    f"submission automatically when its countdown ends, and the "
                    f"work is lost. Finish and submit NOW -- answer the quickest "
                    f"acceptable way, skip anything optional.")
        if mins <= 8:
            return (f" Deadline in {mins:.0f} min (Prolific auto-returns the "
                    f"submission then). Keep moving and do not re-read pages.")
        return f" Deadline in {mins:.0f} min."


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


def saw_prolific_submission_confirmation(text: str) -> bool:
    """True if the Prolific page shows a post-submit/post-cancel confirmation.

    Used by the NOCODE guard: a legit NOCODE either comes AFTER having visited
    the external survey, or AFTER the Prolific-side 'I didn't receive a code' /
    'Cancel participation' flow landed on a confirmation screen. The guard
    refuses NOCODE only when BOTH signals are absent -- i.e. the model is
    still on the study-detail page, having done neither.
    """
    return bool(text) and bool(_PROLIFIC_SUBMITTED_RE.search(text))


def _is_nocode_answer(answer: str) -> bool:
    """True if the model's terminate answer means 'no completion code'.

    Catches the plain "NOCODE" and every answer that announces a code and
    then carries none ("CODE: NOCODE", "CODE: N/A", a bare "CODE:"). Defined
    as the complement of completion_code_of() over those answers rather than
    as its own list of words: the two used to decide "CODE: NOCODE"
    separately, and the statistics drifted into counting 15 such cycles as
    completed studies while this guard treated them as no-code.
    """
    a = (answer or "").strip()
    if a == "NOCODE":
        return True
    return a.upper().startswith("CODE:") and not completion_code_of(a)


def guarded_nocode_refusal(answer: str, url: str, saw_external: bool,
                           saw_cancel_confirm: bool, attempts: int) -> str:
    """Refuse a NOCODE emitted on Prolific without having done the study.

    Narrow conjunctive guard that reproduces exactly the bug in
    run_20261029_184717: the agent is still on a Prolific study-detail page,
    the external survey was never opened, and no Prolific-side submission /
    cancel confirmation ever appeared -- yet the model calls terminate NOCODE.
    That marks the study DONE on our side while Prolific still has it in
    'awaiting submission', and the next cycle's _wait_for_studies sits forever
    on an empty list.

    Also catches "CODE: NOCODE" (the model treating the word NOCODE as a
    completion code): same root cause, same guard.

    Returns the refusal string, or "" to let the terminate through. A safety
    valve lets the 3rd attempt pass regardless so a stale Prolific label can
    never genuinely wedge the loop.
    """
    if not _is_nocode_answer(answer):
        return ""
    if not _on_guarded_site(url):
        return ""
    if saw_external or saw_cancel_confirm:
        return ""
    if attempts >= 2:
        return ""                      # safety valve: 3rd try passes
    return ("NOCODE REFUSED: you are still on the Prolific study detail page, "
            "you never opened the external survey, and no 'cancelled' or "
            "'submitted' confirmation has appeared. Clicking 'I'm finished' on "
            "the detail page does NOT submit the study. To close it properly: "
            "click the red 'Cancel participation' button, confirm in the modal, "
            "and only after the confirmation page appears call terminate NOCODE. "
            "If instead you did complete the survey and no code was shown, "
            "switch to the Prolific tab, click 'I'm finished', then "
            "'I didn't receive a completion code', confirm, and terminate.")


# A forward control whose label matches this SUBMITS/FINISHES the whole survey,
# as opposed to a "next page"/"continue" that only advances within it. Clicking
# it redirects to Prolific with the completion code, which returns the study
# irreversibly -- so while the study timer is still under its minimum this one
# click must be held, even though intermediate next/continue advances are free.
# Stems (no trailing boundary) so complet->complete/completa, termin->termina,
# finaliz->finalize/finalise, conclud->concludi all match. Kept deliberately
# narrow: "next"/"avanti"/arrows are NOT here, they must keep flowing.
_SUBMIT_RE = re.compile(
    r"(?i)\b(submit|invia|finish|complet|finali[sz]|conclud|termin|done|end survey)")

_NEXT_RE = re.compile(
    r"(?i)(^>>$|^>$|^→$|\bnext\b|\bcontinue\b|\bavanti\b|\bcontinua\b|\bweiter\b"
    r"|\bsuivant\b|\bproceed\b|\bforward\b)")


def _is_terminal_submit(label: str) -> bool:
    """True if a forward-button label means 'submit/finish the whole survey'."""
    return bool(label and _SUBMIT_RE.search(label))


def _is_nav_button(label: str) -> bool:
    """True if a button label is a page-navigation control (Next/Continue/>>)."""
    return bool(label and _NEXT_RE.search(label.strip()) and not _SUBMIT_RE.search(label))


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
        label = pw_bridge.run(lambda b: b.active_page().evaluate(_JS, [x, y]))
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


def _completion_return_box():
    """Viewport box of the control that returns this survey to Prolific, or
    None. Read-only: finding it never clicks anything."""
    try:
        return pw_bridge.evaluate(_COMPLETION_RETURN_JS) or None
    except Exception:
        return None


# How far outside the control a click may land and still count as aimed at it.
# run_20261109_120846 missed by 8px on a 36px-tall button; 40 leaves room for a
# worse estimate without reaching a neighbouring control (and the inert-point
# test below is what actually makes the snap safe, not this number).
_RETURN_NEAR_MISS_PX = 40

_COMPLETION_NEAR_MISS_JS = r"""([x, y, margin]) => {
  const box = """ + _COMPLETION_RETURN_JS.strip() + r""";
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


def _completion_return_near_miss(x: float, y: float,
                                 margin: int = _RETURN_NEAR_MISS_PX):
    """Box of the return-to-Prolific control when the click at (x, y) was
    plainly aimed at it and missed, else None.

    run_20261109_120846 step 50: a 36px-tall "Return to Prolific and collect
    your reward" button (y 566..601), clicked at y=609 -- eight pixels under
    its bottom edge. The click hit the page background, nothing happened, and
    the model concluded the button was broken: it waited, switched tab back to
    Prolific, pressed "I'm finished" with no code in hand, read the resulting
    spinner as a disabled button, and sat one click away from "I didn't
    receive a completion code" before coming back and hitting the same button
    correctly (same screenshot, y=584). Six wasted steps at the one moment of
    a run where a wrong click costs the whole study.

    Same remedy as the checkbox retry in dispatch_action: trust the DOM box
    over the pixel estimate and re-aim. The snap fires only when the point is
    on inert background, so a click that did land on some other control is
    never hijacked -- and because it only rewrites the coordinate, the
    minimum-time hold still sees (and can still refuse) the corrected click.
    """
    try:
        return pw_bridge.run(lambda b: b.active_page().evaluate(
            _COMPLETION_NEAR_MISS_JS, [x, y, margin])) or None
    except Exception:
        return None


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


def _nocode_link_box():
    """Viewport box of Prolific's "I didn't receive a completion code"
    control, or None. Read-only: finding it never clicks anything."""
    try:
        return pw_bridge.evaluate(_NOCODE_LINK_JS) or None
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
             closed on Prolific, the answer is not a no-code one, or the
             safety valve has been reached.
    """
    if not _is_nocode_answer(answer):
        return ""
    if not _on_guarded_site(url):
        return ""
    if saw_cancel_confirm:
        return ""                      # already submitted/cancelled on Prolific
    if not link_found:
        return ""
    if attempts >= 2:
        return ""                      # safety valve: 3rd try passes
    if seen_codes or page_codes:
        return "codes"
    return "retry" if already_clicked else "click"


def nocode_codes_refusal(seen: list) -> str:
    code = next((c for c in seen if c), "")
    return (f"NOCODE REFUSED: you are ending this study as 'no completion code', but "
            f"{code} was shown on the study's own pages. Type {code} into the "
            "'Completion Code' field of this panel, click 'Complete study', and "
            f"terminate with CODE:{code}. Only if Prolific rejects that code should "
            "you fall back to 'I didn't receive a completion code'.")


_NOCODE_LINK_REFUSAL = (
    "NOCODE REFUSED: Prolific's 'Confirm submission' panel is still open on this "
    "page, which means the study has NOT been submitted -- the slot stays reserved "
    "and the survey you just filled in is lost. On that panel, under the greyed-out "
    "'Complete study' button, there is a link reading 'I didn't receive a completion "
    "code': left_click it, confirm in the dialog that follows, and call terminate "
    "NOCODE only once the page says the submission was saved ('Awaiting review'). "
    "If instead you never actually completed the survey, press 'Cancel' and use "
    "'Cancel participation' before terminating.")


def completion_pushback(answer: str, url: str, seen_codes: list,
                        page_codes: list, already_pushed: bool) -> str:
    """One-shot nudge when a free-text `terminate` has not actually reached
    Prolific, or "" if the termination may stand.

    A Prolific study is only really finished once you are back on Prolific
    with a completion code (that case is caught earlier, from the URL) or a
    code was shown on the study's own pages. A free-text "I completed the
    study" while still on the survey host, with no code seen anywhere, almost
    always means the model stopped on the host's own "thank you"/validation
    screen and never clicked the final control that submits and returns to
    Prolific -- so nothing was registered. Generic on purpose: it keys off the
    invariant (back on Prolific OR a code), not on any one platform, trial
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
    if _on_guarded_site(url):
        return ""                      # already back on Prolific
    if seen_codes or page_codes:
        return ""                      # a code did appear somewhere
    host = urlparse(url or "").hostname or "this site"
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


def _nearest_checkbox(x: float, y: float, radius: float = 140.0):
    """Centre and checked-state of the nearest checkbox-like control within
    `radius` px of (x, y), as {"cx", "cy", "checked"}, or None.

    Returns the DOM centre, not just the state, so a click that lands just off
    the control (e.g. a box indented inside a padded panel, where the label x
    differs from the input x) can be retried aimed at the control itself.
    Covers real checkboxes plus ARIA checkbox/switch widgets."""
    _JS = """([x, y, radius]) => {
        const els = document.querySelectorAll(
            'input[type=checkbox], [role=checkbox], [role=switch]');
        let best = null, minD = Infinity;
        for (const el of els) {
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) continue;            // hidden control
            const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
            const d = Math.hypot(cx - x, cy - y);
            if (d < radius && d < minD) {
                minD = d;
                const checked = (el.checked !== undefined && el.tagName === 'INPUT')
                    ? el.checked
                    : el.getAttribute('aria-checked') === 'true';
                best = { cx: Math.round(cx), cy: Math.round(cy), checked: checked };
            }
        }
        return best;
    }"""
    try:
        return pw_bridge.run(lambda b: b.active_page().evaluate(_JS, [x, y, radius]))
    except Exception:
        return None




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
        label = pw_bridge.run(lambda b: b.active_page().evaluate(_JS, [x, y]))
    except Exception:
        return ""
    return label if _is_terminal_submit(label) else ""


def dispatch_action(action_type: str, args: dict) -> str:
    if action_type == "left_click":
        x, y = args["coordinate"]
        cb = _nearest_checkbox(x, y)
        # Aim imperfection: always a small gaussian offset (±1-2px) so
        # clicks don't always land on the dead centre of a button. The
        # final cursor position is still exactly (ax,ay), so the retry
        # branch's checkbox check and the result log stay accurate.
        ax = round(x + random.gauss(0, 1.4))
        ay = round(y + random.gauss(0, 1.4))
        _human_move(ax, ay)
        docker_exec("xdotool", "click", "1")
        if cb is not None:
            after = _nearest_checkbox(x, y)
            if after is not None and after.get("checked") == cb.get("checked"):
                # The click did not toggle the box. A blind ±3px nudge is
                # useless when the aim was off by more (seen on Prolific's
                # "confidential" confirm box, indented inside a padded panel:
                # the model kept clicking the panel's left padding, ~27px off
                # the control). Retry aimed at the checkbox's real DOM centre
                # -- still a trusted xdotool click, just grounded correctly.
                tx = cb["cx"] + round(random.gauss(0, 1.0))
                ty = cb["cy"] + round(random.gauss(0, 1.0))
                _human_move(tx, ty)
                docker_exec("xdotool", "click", "1")
        return f"left_click at ({x:.0f},{y:.0f})"

    if action_type == "double_click":
        x, y = args["coordinate"]
        _human_move(round(x), round(y))
        docker_exec("xdotool", "click", "--repeat", "2", "1")
        return f"double_click at ({x:.0f},{y:.0f})"

    if action_type == "right_click":
        x, y = args["coordinate"]
        _human_move(round(x), round(y))
        docker_exec("xdotool", "click", "3")
        return f"right_click at ({x:.0f},{y:.0f})"

    if action_type == "triple_click":
        x, y = args["coordinate"]
        _human_move(round(x), round(y))
        docker_exec("xdotool", "click", "--repeat", "3", "1")
        return f"triple_click at ({x:.0f},{y:.0f})"

    if action_type == "mouse_move":
        x, y = args["coordinate"]
        _human_move(round(x), round(y))
        return f"mouse_move to ({x:.0f},{y:.0f})"

    if action_type == "left_click_drag":
        x, y = args["coordinate"]
        docker_exec("xdotool", "mousedown", "1")
        _human_move(round(x), round(y))
        docker_exec("xdotool", "mouseup", "1")
        return f"left_click_drag to ({x:.0f},{y:.0f})"

    if action_type == "type":
        text = str(args.get("text", ""))
        _human_type(text)
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
        down = pixels < 0
        btn_main, btn_rev = ("5", "4") if down else ("4", "5")
        # --repeat without --delay fires every wheel event in <10ms, which is
        # mechanical. A 25-60ms gap matches a real wheel's burst cadence.
        # ~35% of the time: overshoot a notch or two then return (humans
        # often spin the wheel a bit further than they meant and come back).
        if ticks >= 3 and random.random() < 0.35:
            over = random.choice([1, 2])
            docker_exec("xdotool", *_wheel_click_args(btn_main, ticks + over,
                                                      random.randint(25, 60)))
            cancel.sleep(random.uniform(0.08, 0.22))
            docker_exec("xdotool", *_wheel_click_args(btn_rev, over,
                                                      random.randint(30, 75)))
        else:
            docker_exec("xdotool", *_wheel_click_args(btn_main, ticks,
                                                      random.randint(25, 60)))
        return f"scroll {'up' if not down else 'down'} ({ticks} ticks)"

    if action_type == "hscroll":
        pixels = fp.scale_scroll_pixels(args.get("pixels", 0), fp.VIEWPORT_WIDTH)
        ticks = max(1, abs(pixels) // 100)
        btn = "6" if pixels < 0 else "7"
        docker_exec("xdotool", *_wheel_click_args(btn, ticks,
                                                  random.randint(25, 60)))
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
        # www.prolific.com is the public marketing site, not the app: typing
        # the wrong subdomain used to strand the agent there for a whole
        # cycle. Same path, correct host.
        if _unknown_app_path(target):
            return ("visit_url REFUSED: there is no such page on Prolific. "
                    "Study pages are /studies/<id> with the id exactly as the "
                    "list links it -- do NOT build a URL from a study's name or "
                    "from an id you saw elsewhere. Go back to the studies list "
                    "and click the study, rather than typing an address.")
        if _off_app_host(target):
            fixed = _to_app_host(target)
            # Correcting the host must not launder a path that does not
            # exist: run_20261108_154719 typed www.prolific.com/study/<uuid>,
            # the host guard rewrote it faithfully, and the agent landed on
            # the app's 404 instead of the marketing site's. Send it where it
            # was actually trying to go.
            if _unknown_app_path(fixed):
                home = sites.current.app_home_url()
                pw_bridge.goto(home)
                return (f"visit_url {home} (CORRECTED: {target} is not a real "
                        f"page -- wrong host AND no such path. Study pages are "
                        f"{sites.current.app_host}/studies/<id> with the id exactly as the "
                        f"list links it; never build one from a name or a UUID. "
                        f"You are back on the studies list: click the study.)")
            pw_bridge.goto(fixed)
            return (f"visit_url {fixed} (CORRECTED: you asked for {target}, which is "
                    f"Prolific's public marketing site. The studies only exist on "
                    f"{sites.current.app_host} -- always use that host.)")
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
        return ("run_js is disabled — it triggers bot detection. "
                "Use normal clicks and keyboard actions instead.")

    if action_type in ("pause_and_memorize_fact", "read_page_answer_question"):
        return f"{action_type} (not implemented, no-op)"

    if action_type == "terminate":
        return args.get("answer", "")

    raise ValueError(f"Unknown action: {action_type}")


# How many times batch-fill may attempt the same page/field-set before it
# cedes to the vision path. >1 so a validation-blocked page gets a second shot
# at a still-empty required field; small so it never hammers submit in a loop.
_MAX_BATCH_ATTEMPTS = 2


def _maybe_batch_fill(task: str, attempted_signatures: dict, memory: list = None,
                      timer: "StudyTimer | None" = None, study_pages: list = None):
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
    # Allow a bounded retry instead of ceding the page forever after one try.
    # A page that did not advance (a validation error: a required field still
    # empty or invalid) keeps the same signature, and ceding it immediately to
    # vision is how an unfilled age/country field got stuck. A second attempt
    # lets batch-fill re-answer what is still missing; the cap stops it from
    # re-clicking submit forever on a page it genuinely cannot satisfy.
    attempts = attempted_signatures.get(signature, 0)
    if attempts >= _MAX_BATCH_ATTEMPTS:
        return None, None, (f"already attempted this page/field-set "
                            f"{attempts}x ({len(fields)} fields)")
    attempted_signatures[signature] = attempts + 1

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
             and not result.get("undecided")
             # Questions whose text the DOM never gave us were answered from
             # their options alone. Advancing on that is how six pages of
             # run_20261113_114533 went by with no record and no eyes.
             and not result.get("blind"))
    advance = None
    if clean:
        # Minimum-time guard. Before auto-advancing, peek at the forward
        # button: if it SUBMITS the whole survey (not just a next page) and
        # the study timer has not reached its minimum yet, do NOT click it.
        # This is the deterministic replacement for the status-line hint,
        # which the autonomous batch-fill path never shows the model -- that
        # gap is what returned a 10-min study ~30s after it began. Intermediate
        # next/continue advances are left free so multi-page surveys still flow.
        held_label = ""
        if timer is not None and timer.is_active() and timer.remaining() > TERMINATE_GRACE_SECONDS:
            try:
                peek = form_fill.find_advance_button()
            except Exception:
                peek = None
            if peek and peek.get("found") and _is_terminal_submit(peek.get("label")):
                held_label = peek.get("label")
        if held_label:
            advance = {"attempted": False, "held_for_timer": True,
                       "label": held_label, "remaining": int(timer.remaining())}
        else:
            # A human glances over the filled page before clicking Next, they
            # do not submit the instant the last field is set. Without this the
            # page-to-page submit cadence is machine-fast, which feeds the
            # "completion speed" side of a bot check. Cheap, bridge-free.
            try:
                cancel.sleep(random.uniform(0.8, 2.4))
            except cancel.Cancelled:
                raise
            except Exception:
                pass
            try:
                advance = form_fill.click_advance_button()
            except Exception as e:
                advance = {"attempted": False, "reason": f"error: {e}"}
        result["advance"] = advance

    if advance and advance.get("held_for_timer"):
        rem = advance.get("remaining", 0)
        summary += (f" The page is filled, but the final '{advance['label']}' button was "
                    f"NOT clicked: the study's minimum time is not reached yet "
                    f"(~{rem}s remaining).")
        next_text = (
            f"{summary} Do NOT submit yet. Use the `wait` action (e.g. wait 30) and keep "
            "waiting until the Study timer line says 'OK to submit now', then submit.")
    elif advance and advance.get("advanced"):
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

    # A question that points back at something shown earlier ("Thinking back
    # to the shopping scenario...") is deferred to the vision step, but the
    # screenshot in front of that step is the page the question is NOT about.
    # Hand it what the study showed before, so the answer is there to be read
    # rather than guessed. Only when something was actually deferred.
    if (result.get("deferred_visual") or []) and study_pages:
        recap = "\n---\n".join(p[:400] for p in study_pages[-3:])
        next_text += (
            "\n\nEARLIER IN THIS STUDY, oldest first (text of pages you have already "
            "passed through). A question that refers back to a scenario, an instruction "
            "or a stimulus must be answered from THIS, not from what sounds "
            f"plausible:\n{recap}")

    # What was actually answered, question by question. Until now the
    # transcript kept only "filled 8/8 fields successfully": a rejected study
    # whose middle pages were batch-filled left no record of a single question
    # or answer, and no screenshot either, because this path takes no model
    # turn. Five pages could vanish that way.
    by_id = {f["id"]: f for f in fields}
    qa = [[(by_id.get(fid, {}).get("text") or fid).strip().replace("\n", " ")[:120],
           (", ".join(map(str, v)) if isinstance(v, list) else str(v))[:80]]
          for fid, v in (result.get("answers") or {}).items()][:40]

    step_event = {
        "type": "batch_fill",
        "thoughts": "Detected a multi-question form; filled it via DOM extraction instead of per-field clicking.",
        "result": result,
        "result_desc": summary,
        "qa": qa,
    }
    return step_event, next_text, summary


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


def _standing_rules() -> str:
    if _speed_mode == "normal":
        return _STANDING_RULES_FULL
    return _STANDING_RULES_COMPACT


def _identity_hint_text(idv: dict) -> str:
    """The line that gives the model its EXACT Prolific ID / email (from
    identity.json or the task) so it types them when a study asks, instead of
    refusing as 'not provided'. Empty when nothing is known."""
    parts = []
    if (idv or {}).get("prolific_id"):
        parts.append(f"Prolific ID is exactly {idv['prolific_id']}")
    if (idv or {}).get("email"):
        parts.append(f"Prolific email is exactly {idv['email']}")
    if not parts:
        return ""
    return ("\n\nYOUR ACCOUNT DETAILS (known, from file -- use the EXACT value, never alter or "
            "invent): " + "; ".join(parts) + ". When a study or survey asks for your Prolific ID "
            "or email, type exactly this value. It is already available, so never say it is "
            "missing and never refuse, cancel, or return a study over a Prolific ID / email field.")


# Bumped whenever summarize_transcript classifies a run differently, so the
# summaries already on disk are recomputed instead of keeping a verdict this
# version of the code would no longer reach. v2: a "CODE:" answer whose code
# slot holds the model's own no-code word is NOCODE, not a completion.
SUMMARY_V = 2


def summarize_transcript(lines: list, started: datetime = None, ended: datetime = None) -> dict:
    """One run, reduced to what the dashboard and the daily summary need.

    Where the steps went is read from the per-step diag lines: on the
    studies list, inside a study (the external questionnaire, timer
    running), or elsewhere on Prolific.
    """
    steps, answer, source = 0, "", None
    flagged = ""
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
            if d.get("qc_flag") and not flagged:
                flagged = d["qc_flag"]
            if d.get("timer_active") and not d.get("guarded"):
                where["study"] += 1
            elif d.get("guarded") and sites.current.home_path.match(urlparse(url).path or ""):
                where["list"] += 1
            else:
                where["elsewhere"] += 1
            m = sites.current.task_path.match(urlparse(url).path or "")
            if m and m.group(1) not in studies:
                studies.append(m.group(1))
            if d.get("code_source"):
                source = d["code_source"]
            tps = (d.get("model") or {}).get("gen_tps")
            if tps:
                gen.append(tps)
    code = completion_code_of(answer)
    if code:
        outcome = "CODE"
    elif _is_nocode_answer(answer):
        # Plain NOCODE, and "CODE: NOCODE" and friends: a run that banked
        # nothing, not a completion. Same predicate the NOCODE guard uses.
        outcome = "NOCODE"
    elif answer in ("NONE", "STOPPED"):
        outcome = answer
    else:
        outcome = "INCOMPLETE" if not answer else "TERMINATE"
    out = {"v": SUMMARY_V,
           "steps": steps, "outcome": outcome, "answer": answer[:80],
           "code": code,
           "code_source": source if outcome in ("CODE", "NOCODE") else None,
           "steps_where": where, "studies": studies,
           "gen_tps_median": sorted(gen)[len(gen) // 2] if gen else None}
    if flagged:
        # The researcher said the submission failed their checks. Kept out
        # of the outcome itself: the code was really earned and really
        # banked, so it stays a CODE run, with the verdict beside it.
        out["flagged"] = flagged
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

    global _live_sink
    _live_sink_step = {"n": 0}

    def _write_notice(msg: dict):
        rec = dict(msg)
        rec.setdefault("step", _live_sink_step["n"])
        transcript.write(json.dumps(rec) + "\n")
        transcript.flush()

    _live_sink = _write_notice

    try:
        if start_url:
            _learn_app_host(start_url)
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
        # True while pending_user_response holds a USER inject (a mid-run
        # steer), as opposed to harness/batch-fill continuation text. A user
        # inject must reach the model, so while this is set the autonomous
        # batch-fill is skipped for a step (it would otherwise auto-advance
        # with no model turn and overwrite the inject with its own text). See
        # the batch-fill branch and the two inject-capture sites below.
        _user_inject_pending = False
        attempted_form_signatures = {}   # signature -> attempt count (see _MAX_BATCH_ATTEMPTS)
        survey_memory = []  # (question, answer) given so far in this run
        wander = WanderGuard(wander_seconds)
        study_timer = StudyTimer()
        _last_guarded_duration: float | None = None  # persists across clicks within a study entry
        # Prolific's remaining-time countdown, read off the detail page
        # before the launch click. See _extract_time_remaining.
        _last_guarded_deadline: float | None = None
        _prev_was_guarded: bool = False  # tracks previous step's site so ANY action triggers timer
        _model_moved_viewport: bool = False  # last action was the model's own scroll/page key
        _prev_on_launch: bool = True  # prev step was on the Prolific launch surface (list/detail/chrome)
        # Page object the launch surface was last seen in, and the chrome
        # URLs we have already explained once (see _reached_in_launch_tab).
        _launch_page_id = None
        _cur_page_id = None
        _chrome_guard_noticed: set = set()
        _prev_here: str = ""  # URL at the previous step, for detecting navigation landings
        _current_study_id: str | None = None  # id of the study being looked at / taken
        _current_study_title: str = ""  # its name, read on Prolific while we can still see it
        _seen_codes: list = []  # completion codes spotted on the study's own pages
        # _used_run_js removed — run_js is disabled for anti-detection
        _survey_url: str = ""  # URL of the external survey (for study_memory)
        # Completion guard: nudge a free-text "I'm done" that never got back to
        # Prolific / a code, at most once per study so a re-terminate is obeyed.
        _completion_pushed_back: bool = False
        # One shot per study at clicking the survey's own return-to-Prolific
        # control ourselves (see _COMPLETION_RETURN_JS).
        _completion_return_tried: bool = False
        # NOCODE guard: refuse a terminate NOCODE emitted while still on the
        # Prolific detail page without having visited the external survey or
        # seen a Prolific-side cancel/submit confirmation. See
        # guarded_nocode_refusal for the full rationale.
        _saw_external_this_study: bool = False
        _saw_cancel_confirm: bool = False
        _nocode_guard_attempts: int = 0
        # Sibling guard, Prolific side: a no-code terminate while the
        # 'Confirm submission' panel is still open. See _NOCODE_LINK_JS.
        _nocode_link_attempts: int = 0
        _nocode_link_tried: bool = False
        # The scenario / instruction / stimulus pages of the study in
        # progress. See _remember_study_page.
        _study_pages: list = []
        # Reservation-loop guard: once a slot is reserved on Prolific the URL
        # becomes /studies/{id} and stays there — the only legitimate exits
        # are 'Start study' or 'Cancel participation'. The model sometimes
        # tries to escape with history_back / visit_url instead (seen in
        # run_20261103_194847 after an older user_hint was misread as a
        # standing rule to cancel). Count those abort attempts on the same
        # detail URL; after RESERVED_LOOP_THRESHOLD we inject a nudge that
        # names the only two valid exits.
        _reserved_loop_url: str = ""
        _reserved_loop_count: int = 0
        # Any step that ends still on /studies/{id} with no URL change from
        # the previous step counts as 'stuck'. The model in run_20261104_073003
        # wasted 13 steps tab-switching and pressing Win/Ctrl+L while the
        # URL never moved -- none counted as abort attempts, so the original
        # history_back-only guard never fired. Fallback threshold is lower
        # than the abort-attempt one because 'stuck' already means no useful
        # browser transition is happening.
        _reserved_stuck_count: int = 0
        # One-shot per reservation. Gates BOTH the deterministic launcher (when
        # the agent is stuck on a fresh, un-launched reservation the harness
        # opens the external survey itself via pw_bridge.launch_reserved_study,
        # using stable data-testid selectors, instead of leaving a fragile
        # pixel-click + below-the-fold confirm to the vision model) AND the
        # text nudge that is its fallback.
        _reserved_loop_nudged: bool = False
        # Orphan-reservation guard: see _orphan_reservation_visible. On the
        # reserved detail page the model has been seen to hallucinate a
        # 'Take part in this study' button that does not exist (the panel
        # actually shows 'I'm finished' + 'Cancel participation'), then
        # click into the Confirm-submission flow with no code to enter.
        # Nudged once per visit to the detail page, reset on return to
        # /studies so re-entry re-fires it after context compaction.
        _orphan_nudged: bool = False
        # Content-change signal: see _page_fingerprint / _page_change_hint.
        # Reset per run so one cycle's final page doesn't bleed into the next.
        _last_page_fp: str = ""
        _last_action_type_for_signal: str = ""
        # First "your submission failed our checks" wording seen this run,
        # read off the fingerprint the change signal already computes, so
        # noticing it costs no extra page read. Kept per run: it goes into
        # the step's diag line, and from there into summary.json.
        _qc_flag: str = ""
        # Loop detector: last few action signatures with their URL path. When
        # the SAME signature on the SAME path repeats its threshold number of
        # times in a row (STUCK_THRESHOLD, or SCROLL_STUCK_THRESHOLD for
        # scrolls), step N+1 gets a prepended STUCK WARNING in its prefix so
        # the model is told explicitly to break the loop instead of
        # continuing. maxlen must be >= the largest threshold. Reset per run.
        _recent_action_sigs: deque = deque(maxlen=_SCROLL_STUCK_THRESHOLD + 1)
        # Identity (Prolific ID / email) the user put in identity.json or the
        # task, surfaced to the model so it can TYPE the exact value when a study
        # asks for it. The DOM identity-fill stays primary; this is the fallback
        # for pages the extractor can't tag (e.g. a Prolific-ID page the vision
        # model otherwise refused, believing the ID "was not provided"). Fetched
        # once: these values are account-level constants.
        _identity_hint = ""
        try:
            _identity_hint = _identity_hint_text(form_fill.identity_values(task))
        except Exception:
            _identity_hint = ""

        def _record_user_correction(text: str, persist: bool = True) -> None:
            """A user instruction (question answer or injected steer) arrived.

            Always logged to the transcript -- these were previously invisible,
            which made it impossible to see afterwards what the user had to
            step in and tell the agent.

            `persist` controls whether it is also stored as a durable
            study_memory hint (surfaced on every later step and future run on
            this domain). Proactive steers injected mid-run are durable
            guidance and persist. Answers to the agent's OWN ask_user_question
            are transient and instance-specific ("cancel this study",
            "answer 25") -- persisting them made the agent carry a one-off
            reply into every future study on the domain (e.g. a "cancel the
            study" answer made it cancel later studies it should complete), so
            those are logged only, never stored.
            """
            text = (text or "").strip()
            if not text:
                return
            url = get_current_url()
            try:
                transcript.write(json.dumps(
                    {"step": step, "type": "user_correction", "text": text,
                     "url": url, "persisted": persist}) + "\n")
                transcript.flush()
            except Exception:
                pass
            if not persist:
                return
            try:
                study_memory.record_user_hint(url, text)
            except Exception:
                pass

        for step in range(1, max_rounds + 1):
            _live_sink_step["n"] = step
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
                # Backstop: the submit guards above hold the final submit until
                # the timer clears, so reaching the completion URL early should
                # be impossible. If it happens anyway (e.g. a survey whose only
                # end control is an unlabeled arrow that the label classifier
                # cannot recognise as a submit), the study is already returned
                # and cannot be undone -- but flag it loudly instead of logging
                # a silent success, so the slipping survey can be handled.
                early = (study_timer.is_active()
                         and study_timer.remaining() > TERMINATE_GRACE_SECONDS)
                transcript.write(json.dumps({
                    "step": step, "action_type": "terminate", "args": {"answer": answer},
                    "code_source": "url", "url": here,
                    "early_return": bool(early),
                    "timer_remaining": round(study_timer.remaining(), 1) if early else None,
                }) + "\n")
                transcript.flush()
                _restore_user_speed()
                _close_extra_tabs()
                if early:
                    rem = int(study_timer.remaining())
                    _live({"type": "notice",
                           "text": f"⚠️ Studio restituito {rem}s SOTTO il tempo minimo "
                                   f"(min {study_timer.min_seconds:.0f}s) via URL: submit sfuggito alle gate."})
                    _notify_telegram(f"⚠️ Fara: studio restituito TROPPO PRESTO\n{answer}\n"
                                     f"{rem}s sotto il minimo ({study_timer.min_seconds:.0f}s)\n{here}")
                else:
                    _notify_telegram(f"Fara studio completato ✅\n{answer} (dall'URL)\n"
                                     f"Ciclo: {run_dir.name if run_dir else '?'}")
                yield {"type": "notice", "text": f"Codice di completamento letto dall'URL: {cc}",
                       "messages": messages}
                yield {"type": "done", "answer": answer, "messages": messages}
                return

            # Stateful transition check: if previous step was on Prolific and now we're
            # external, start the timer regardless of HOW the navigation happened
            # (left_click, key press, Enter in a modal, JS redirect, etc.).
            # Done BEFORE the batch-fill block so the timer is already active when
            # batch-fill decides whether to hold a final submit on the very first
            # external page (a one-page fill-and-submit survey would otherwise be
            # submitted before the timer ever started).
            here_guarded = _on_guarded_site(here)
            # Stranded on the marketing site (a click on its logo/nav, or a
            # redirect, not just a mistyped visit_url): walk straight back to
            # the app. Nothing on www.prolific.com can advance a study, and
            # the model does not recognise it as the wrong site -- it treated
            # /ai-task-builder as a broken studies list for 23 steps
            # (run_20261106_142347). Never fires inside a study: an external
            # survey is not on the guarded domain at all.
            if _off_app_host(here):
                back = sites.current.app_home_url()
                _live({"type": "notice",
                       "text": f"[guard] off the app host ({urlparse(here).hostname}) "
                               f"-> back to {back}"})
                try:
                    pw_bridge.goto(back)
                    pw_bridge.wait_for_content()
                    here = pw_bridge.current_url() or back
                except Exception:
                    here = back
                here_guarded = _on_guarded_site(here)
                if not pending_user_response:
                    pending_user_response = (
                        f"You had navigated to Prolific's PUBLIC MARKETING SITE "
                        f"(www.prolific.com), which is a different website: it "
                        f"advertises Prolific to researchers and has no studies, no "
                        f"study list and no account of yours on it. You have been "
                        f"returned to {back}. The studies only ever live on "
                        f"{sites.current.app_host} -- stay on it.")
            _timer_started_this_step = False
            # Proactive, cache-aware compaction at natural checkpoints: when
            # we cross the Prolific/external boundary (either direction),
            # most of the history for the OTHER side (list browsing, old
            # studies, prior survey pages) is no longer useful. Collapse it
            # once BEFORE this step grows the context further, so the model
            # starts the new page with a small stable prefix and llama's
            # prompt cache can warm cheaply for the next 10+ steps.
            if _prev_was_guarded != here_guarded and len(messages) > 6:
                _hard_compact_context(messages, keep_recent=2)
            here_on_launch = _on_launch_surface(here)
            try:
                _cur_page_id = pw_bridge.active_page_id()
            except Exception:
                _cur_page_id = None
            if not here_on_launch and _reached_in_launch_tab(
                    here, _cur_page_id, _launch_page_id):
                here_on_launch = True
                if here not in _chrome_guard_noticed:
                    _chrome_guard_noticed.add(here)
                    _live({"type": "notice", "text":
                           f"[guard] {here} is Prolific account chrome reached "
                           f"in the launch tab, not a study: no timer started"})
            # "Inside a task" = off the launch surface: an external survey host
            # OR a Prolific-hosted task tool (e.g. /data-collection-tool/...).
            # The study timer and in-task framing key on THIS transition, not on
            # "left prolific.com", so first-party Prolific surveys are treated as
            # the running task they are instead of looking like the reservation
            # page (which made the model cancel completable studies).
            entered_task = _prev_on_launch and not here_on_launch
            if _prev_was_guarded and not here_guarded:
                # NOCODE-guard signal: strictly "reached an EXTERNAL survey host"
                # (unchanged on purpose -- see guarded_nocode_refusal).
                _saw_external_this_study = True
            if entered_task and not study_timer.is_active():
                effective_duration = _last_guarded_duration
                if effective_duration is not None:
                    study_timer.start(effective_duration * 60,
                                      deadline_seconds=_last_guarded_deadline or 0)
                    _live({"type": "timer_start", "min_seconds": study_timer.min_seconds,
                           "min_minutes": effective_duration})
                else:
                    study_timer.start(0, deadline_seconds=_last_guarded_deadline or 0)
                    _live({"type": "timer_start", "min_seconds": 0, "min_minutes": 0})
                _timer_started_this_step = True
                _survey_url = here
                dest = "external" if not here_guarded else "prolific-hosted task"
                _live({"type": "notice", "text": f"[timer] transition detected: launch→{dest} | duration={effective_duration}min"})
                dur_str = f" (~{effective_duration:.0f} min)" if effective_duration else ""
                _notify_telegram(f"📝 Studio iniziato{dur_str}\n{here}")
            _prev_was_guarded = here_guarded
            _prev_on_launch = here_on_launch
            if here_on_launch and _cur_page_id is not None:
                _launch_page_id = _cur_page_id
            # Keep the previous URL available for the just_landed check in the
            # dwell block below. _prev_here must be updated at the END of the
            # step, not here, or the check never sees a different value.

            # Gate on the LAUNCH SURFACE, not on the host. Batch-fill must
            # never touch Prolific's own chrome (the studies list, a detail
            # page, submissions/account) -- but a Prolific-HOSTED task
            # (/data-collection-tool/...) is a running questionnaire like any
            # other, and `here_guarded` locked it out of batch-fill purely
            # because it shares the domain. run_20261106_154221 answered a
            # 3-minute dropdown questionnaire with 139 separate vision clicks
            # at 8.3s each, took 22m40s against Prolific's ~15m window, and
            # the submission was auto-returned. Same confusion the wander
            # guard had in 1ffb26d.
            if batch_fill and not here_on_launch and not _user_inject_pending:
                batch_step_event, batch_next_text, batch_diag = _maybe_batch_fill(
                    task, attempted_form_signatures, survey_memory, timer=study_timer,
                    study_pages=_study_pages)
                transcript.write(json.dumps(
                    {"step": step, "batch_fill_check": batch_diag}) + "\n")
                if (batch_step_event or {}).get("qa"):
                    transcript.write(json.dumps(
                        {"step": step, "batch_fill_qa": batch_step_event["qa"]}) + "\n")
                transcript.flush()
                if batch_step_event is not None:
                    pending_user_response = batch_next_text
                    batch_step_event["step"] = step
                    batch_step_event["messages"] = messages
                    injected = yield batch_step_event
                    if injected:
                        pending_user_response = injected
                        _user_inject_pending = True
                        _record_user_correction(injected)
                    continue

            # Reset timer when back at the studies list (timer re-arms for next study).
            on_list = here_guarded and bool(sites.current.home_path.match(urlparse(here).path or ""))
            if on_list:
                study_timer.reset()
                _restore_user_speed()
                _last_guarded_duration = None
                _last_guarded_deadline = None
                _prev_was_guarded = False
                _current_study_id = None
                _current_study_title = ""
                _seen_codes = []
                _completion_pushed_back = False
                _completion_return_tried = False
                _saw_external_this_study = False
                _saw_cancel_confirm = False
                _nocode_guard_attempts = 0
                _nocode_link_attempts = 0
                _nocode_link_tried = False
                _study_pages = []
                _reserved_loop_url = ""
                _reserved_loop_count = 0
                _reserved_stuck_count = 0
                _reserved_loop_nudged = False
                _orphan_nudged = False
                # Back on the studies list: everything from the previous
                # study is history that will never be relevant again --
                # collapse it in one shot so the next cycle starts with a
                # tiny cache-stable context.
                if len(messages) > 6:
                    _hard_compact_context(messages, keep_recent=1)

            # --- studies that lead nowhere (and, opt-in, audio/video ones) ---
            # A study that is listed but not doable gets opened again on every
            # cycle: the model has no memory across cycles, so without this it
            # re-discovers the same dead end for as long as it stays listed.
            # The wording is only read on the study page, where it is a
            # description; on an external questionnaire only real media
            # elements count, because aborting a survey halfway on a false
            # positive costs much more than one wasted click.
            m_study = sites.current.task_path.match(urlparse(here).path or "") if here_guarded else None
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
            # Gated on the launch surface, not the host: a Prolific-HOSTED
            # task (/data-collection-tool/...) that turns out to need a
            # microphone or webcam has to be abandoned exactly like an
            # external one. `here_guarded` excluded it for sharing the
            # domain. Same confusion as 1ffb26d and 88c15c4.
            elif skip_av and not here_on_launch and study_timer.is_active():
                av = _detect_av(check_text=False)
                if av:
                    skip_reason = f"audio/video: {av}"

            if skip_reason:
                title = _current_study_title or _study_title()
                key = _current_study_id or title or here
                study_cache.mark(key, skip_reason, title=title, url=here)
                transcript.write(json.dumps(
                    {"step": step, "type": "skip", "reason": skip_reason,
                     "study": key, "title": title, "url": here,
                     "matched": _last_dead_end_sentence}) + "\n")
                transcript.flush()
                abandoned = study_timer.is_active()
                study_timer.reset()
                _restore_user_speed()
                _last_guarded_duration = None
                _last_guarded_deadline = None
                _current_study_id = None
                _current_study_title = ""
                try:
                    pw_bridge.goto(studies_url())
                    pw_bridge.wait_for_content()
                    _prev_was_guarded = True
                    _prev_on_launch = True   # back on the studies list (a launch surface)
                except Exception:
                    pass
                pending_user_response = (
                    f"That study was ruled out automatically ({skip_reason}) and you were "
                    "taken back to the list of studies. Do not open it again: pick another "
                    "study, or follow your task instructions if none is eligible.")
                label = f"«{title}»" if title else "lo studio aperto"
                link = (f"\n{sites.current.task_url(key)}"
                        if sites.current.task_url(key) else "")
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

            # Flag Prolific-side submit/cancel confirmations so the NOCODE
            # guard knows this terminate came after a legitimate close
            # ("I didn't receive a code" confirmation OR Cancel participation
            # confirmation), not from clicking "I'm finished" on the detail
            # page without ever submitting.
            if here_guarded and not _saw_cancel_confirm:
                try:
                    if saw_prolific_submission_confirmation(_end_page_text()):
                        _saw_cancel_confirm = True
                except Exception:
                    pass

            skip_hint = study_cache.prompt_line() if on_list else ""
            nav_hint = ""
            mem_hint = ""
            if not on_list:
                nav_hint = _dom_nav_hint()
                # User corrections apply on any study page (survey or the
                # Prolific study detail), so they surface whenever we are off
                # the list. The run_js batch hint is survey-specific, so it
                # stays gated on an active study timer.
                mem_hint = study_memory.user_hints_for(here)

            # Reading dwell: simulate the time a human spends scanning the
            # page before acting. Only on survey pages (not the Prolific list
            # where there is no question to "read") and only from step 2
            # onward (step 1 has its own resume delay). Proportional to
            # visible text length, capped at 8s.
            # Gated on the launch surface, not the host. Skipping the dwell
            # on a Prolific-HOSTED questionnaire made the agent answer real
            # questions with no reading time at all -- the exact behavioural
            # signature the humanised input path exists to avoid -- purely
            # because the task shares Prolific's domain. `on_list` stays:
            # there is nothing to read on the studies list.
            # ...but never at the cost of the slot: reading dwell is the
            # first thing to drop once Prolific's countdown is inside its
            # last quarter. A page read like a human but submitted after the
            # deadline is auto-returned and pays nothing.
            _dl_pressure = study_timer.deadline_pressure()
            _dwell_ok = _dl_pressure is None or _dl_pressure < 0.75
            if step > 1 and not on_list and not here_on_launch and _dwell_ok:
                # Landing behaviour: when the URL just changed (navigation
                # landed, redirect finished, or a new survey page replaced
                # the previous one), a human spends an extra ~1-2s orienting
                # and often micro-scrolls before reading. Keyed on path or
                # host change — a pure hash fragment change (SPA tab switch)
                # does not re-trigger it, keeping repeated-trial surveys fast.
                def _path_key(u: str) -> str:
                    try:
                        p = urlparse(u); return f"{p.netloc}{p.path}"
                    except Exception:
                        return u
                just_landed = bool(_prev_here) and _path_key(here) != _path_key(_prev_here)
                try:
                    if just_landed:
                        # Orient idle: short settle before even reading.
                        cancel.sleep(random.uniform(0.6, 1.6))
                        # Micro-scroll with a bell-curve of small deltas,
                        # then a smaller reversal (humans often overshoot
                        # and come back). Skipped on the Prolific list via
                        # the outer guard.
                        if random.random() < 0.75:
                            total_down = random.randint(60, 150)
                            steps_down = random.choice([3, 4, 5])
                            # Bell-ish distribution: peak in the middle
                            weights = [1, 3, 5, 3, 1][:steps_down]
                            total_w = sum(weights)
                            for w in weights:
                                px = int(round(total_down * w / total_w))
                                n = max(1, px // 40)
                                # --repeat without --delay fires all wheel
                                # events in <10ms, which looks mechanical;
                                # a 25-60ms gap between notches matches a
                                # real wheel's natural burst cadence.
                                docker_exec("xdotool",
                                            *_wheel_click_args("5", n,
                                                               random.randint(25, 60)))
                                cancel.sleep(random.uniform(0.03, 0.08))
                            cancel.sleep(random.uniform(0.3, 0.8))
                            # Return a bit (not all the way) ~60% of the time.
                            if random.random() < 0.6:
                                up_px = int(total_down * random.uniform(0.3, 0.7))
                                n_up = max(1, up_px // 40)
                                docker_exec("xdotool",
                                            *_wheel_click_args("4", n_up,
                                                               random.randint(30, 70)))
                                cancel.sleep(random.uniform(0.15, 0.45))
                    page_text = _end_page_text(limit=3000)
                    dwell = min(5.0, max(1.5, len(page_text) * 0.035))
                    dwell *= random.uniform(0.7, 1.3)
                    cancel.sleep(dwell)
                except cancel.Cancelled:
                    raise
                except Exception:
                    pass

            _live({"type": "phase", "phase": "observe", "step": step})
            # Pre-screenshot viewport nudge: in speed modes, inside a study,
            # on non-guarded pages (Prolific dashboard is excluded), bring
            # the first unselected response control into view BEFORE scrot
            # captures. Saves 1-3 model-driven scroll steps per trial on
            # long-content surveys. Safe no-op when nothing matches, when
            # a candidate is already in view, or when pw_bridge throws.
            # ...and never on the step straight after the model scrolled or
            # paged itself. The nudge and the model were pulling the viewport
            # in opposite directions with no way for either to notice
            # (run_20261108_181732). An explicit scroll is an explicit intent
            # to look somewhere else: it wins for one step.
            prescroll_result = ""
            if _model_moved_viewport:
                prescroll_result = "skipped-after-scroll"
            elif (_speed_mode != "normal" and study_timer.is_active()
                    and not here_guarded):
                prescroll_result = _prescroll_to_controls()
                if prescroll_result == "scrolled":
                    # Scroll is sync (behavior: instant) but repaint still
                    # needs one frame; without this scrot can land mid-paint
                    # and the model sees a torn screenshot.
                    try:
                        cancel.sleep(0.05)
                    except Exception:
                        pass
            # Pin the tracked active tab to the foreground right before scrot,
            # so what the model SEES (screenshot) and CLICKS (xdotool) is the
            # same tab the URL guards and DOM reads use (active_page). Without
            # this they drift after a survey opens in a new tab, and the model
            # has been seen acting on, and cancelling, the wrong study.
            try:
                pw_bridge.front()
            except Exception:
                pass
            img = take_screenshot(run_dir, step)
            data_url = encode_for_model(img)
            # Fingerprint the page NOW so step N+1 can tell whether this
            # step's action moved the page. Done after screenshot so the
            # action-settle delay has already run.
            curr_page_fp = _page_fingerprint()
            # Prolific's own pages list past rejections with their reasons; reading
            # one there would stamp this run with someone else's verdict. The
            # researcher's end page is never on a guarded host.
            qc_hit = "" if (_qc_flag or here_guarded) else qc_failure_in(curr_page_fp)
            if qc_hit:
                _qc_flag = qc_hit
            # Only pages with no fillable form reach this far (batch-fill
            # returns before it), which is exactly the scenario / instruction
            # / stimulus set a later question can refer back to.
            if not here_guarded:
                _remember_study_page(_study_pages, curr_page_fp)
            page_signal = _page_change_hint(_last_page_fp,
                                            _last_action_type_for_signal,
                                            curr_page_fp)

            if step == 1:
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
                text = task + _standing_rules() + _identity_hint + (f"\n\n{skip_hint}" if skip_hint else "") + resume_hint
                if pending_user_response:
                    text += f"\n\n[User (mid-run instruction)]: {pending_user_response}"
                    pending_user_response = ""
                    _user_inject_pending = False
            else:
                url = get_current_url()
                prefix = f"Current URL: {url}\n" if url else ""
                prefix += page_signal
                prefix += _stuck_warning(_recent_action_sigs)
                prefix += skip_hint + nav_hint + mem_hint
                # Prolific-hosted task surface (e.g. the data-collection-tool):
                # it lives on app.prolific.com, so the model keeps mistaking a
                # trial it is actively doing for the studies list / reservation
                # page and cancels a study it is completing. State plainly where
                # it is. (Guarded + off the launch surface == a first-party task.)
                if here_guarded and not here_on_launch:
                    prefix += (
                        "[You are INSIDE the study task right now — this is a "
                        "Prolific-hosted survey, NOT the studies list and NOT the "
                        "reservation/detail page. A study is open and in progress. "
                        "Keep answering the trials and advancing. Do NOT click "
                        "'Cancel participation' or 'Back to studies', and do NOT "
                        "terminate as if no study were open. Only leave if the task "
                        "itself is genuinely impossible (needs a microphone/camera, "
                        "or a CAPTCHA blocks you).]\n")
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
                # Pending user instructions used to replace the whole prompt,
                # which stripped mem_hint/nav_hint/URL and let the model treat
                # the correction as a passing comment instead of a steer. Now
                # they ride along with the normal context so the model still
                # sees past user hints and the standing per-step cue.
                if pending_user_response:
                    inj = pending_user_response
                    pending_user_response = ""
                    _user_inject_pending = False
                    text = (f"{prefix}[User (mid-run instruction)]: {inj}\n\n"
                            f"{fp.USER_MESSAGE}")
                else:
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
            _model_moved_viewport = (
                action_type in ("scroll", "hscroll")
                or (action_type == "key"
                    and any((k or "").lower() in
                            ("home", "end", "page_up", "page_down",
                             "pageup", "pagedown", "prior", "next")
                            for k in (args.get("keys") or []))))

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
                _record_user_correction(answer, persist=False)
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
                # Prolific's countdown is only on the detail page, so it has
                # to be read before the click that leaves it.
                _pcd = _extract_time_remaining()
                if _pcd is not None:
                    _last_guarded_deadline = _pcd
            # Remember what we're about to do so step N+1's page-change hint
            # can name the action. Saved BEFORE dispatch because dispatch may
            # raise, and even on failure we want the next step to know what
            # was attempted. Only kept for actions that may advance the page.
            if action_type in _ADVANCING_ACTIONS:
                _last_action_type_for_signal = action_type
            else:
                _last_action_type_for_signal = ""
            _last_page_fp = curr_page_fp
            _recent_action_sigs.append(_action_signature(action_type, args, here))

            # Minimum-time guard, model-driven path: on an external survey, while
            # the study timer is still below its minimum, a left_click that lands
            # on a submit/finish control is held and turned into a wait instead of
            # being dispatched. Once submitted the survey redirects to Prolific
            # with the completion code and the study is returned irreversibly, so
            # the hold has to happen here, before dispatch. Next/Continue clicks
            # (and every click off a submit control) are not affected. Setting
            # pending_user_response also suppresses the auto-chain-Enter below,
            # which would otherwise submit the page the click did not.
            # Near-miss on the control that ends the study: re-aim, don't
            # waste the click. Runs BEFORE the minimum-time guard below so the
            # corrected coordinate is what that guard judges -- a re-aimed
            # click can still be held back if the study minimum is not up.
            snap_note = ""
            if action_type == "left_click" and not here_guarded:
                _coord = args.get("coordinate")
                if _coord:
                    _sbox = _completion_return_near_miss(_coord[0], _coord[1])
                    if _sbox:
                        _nx = _sbox["x"] + _sbox["width"] * random.uniform(0.3, 0.7)
                        _ny = _sbox["y"] + _sbox["height"] * random.uniform(0.35, 0.65)
                        snap_note = (f" [re-aimed {_coord[1]:.0f}->{_ny:.0f}y onto "
                                     f"the return control]")
                        args["coordinate"] = [_nx, _ny]
                        _live({"type": "notice", "text":
                               "[completion] click missed the survey's return control "
                               f"({_sbox.get('text')!r}) by {_sbox.get('miss')}px: "
                               "re-aimed onto it"})

            submit_held = ""
            if (action_type == "left_click"
                    and not here_guarded
                    and study_timer.is_active()
                    and study_timer.remaining() > TERMINATE_GRACE_SECONDS):
                coord = args.get("coordinate")
                if coord:
                    submit_held = _click_hits_submit(coord[0], coord[1])

            if submit_held:
                rem = int(study_timer.remaining())
                result_desc = (f"SUBMIT HELD (study timer): '{submit_held}' not clicked, "
                               f"~{rem}s below the study minimum")
                _live({"type": "notice",
                       "text": f"[timer] submit '{submit_held}' trattenuto: {rem}s sotto il minimo"})
                try:
                    cancel.sleep(min(30.0, max(5.0, study_timer.remaining())))
                except cancel.Cancelled:
                    raise
                except Exception:
                    pass
                pending_user_response = (study_timer.status_line() or
                                         "Do NOT submit yet; wait for the study minimum time.")
            else:
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
                                page_text = _page_text(12000)
                                dwell = _reading_dwell(page_text)
                                if dwell > 0:
                                    _live({"type": "notice",
                                           "text": f"[dwell] reading pause {dwell:.1f}s before '{nav_label}'"})
                                    cancel.sleep(dwell)
                            except cancel.Cancelled:
                                raise
                            except Exception:
                                pass

                try:
                    result_desc = dispatch_action(action_type, args) + snap_note
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

            # Resting cursor drift: after a click or mouse_move, ~40% of the
            # time a human's hand settles the cursor a few pixels away from
            # the target (not perfectly still). Skipped on terminate / page
            # navigation / typing / scroll (keyboard-driven or already busy).
            if (action_type in ("left_click", "double_click", "right_click",
                                "triple_click", "mouse_move")
                    and random.random() < 0.40):
                try:
                    cx, cy = _get_cursor_pos()
                    dx = int(round(random.gauss(0, 4)))
                    dy = int(round(random.gauss(0, 4)))
                    if abs(dx) + abs(dy) > 0:
                        _human_move(max(0, cx + dx), max(0, cy + dy))
                except Exception:
                    pass

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

            # Reservation-loop guard: on a Prolific /studies/{id} detail page
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
            m_resv = sites.current.task_path.match(urlparse(here).path or "") if here_guarded else None
            if m_resv:
                detail_id = m_resv.group(1)
                # A visit to a different detail page resets everything for it.
                if _reserved_loop_url != detail_id:
                    _reserved_loop_url = detail_id
                    _reserved_loop_count = 0
                    _reserved_stuck_count = 0
                    _reserved_loop_nudged = False
                    _orphan_nudged = False

                # Launch state of this reserved slot. A study whose survey was
                # never opened this run (_saw_external_this_study False) and
                # that STILL OFFERS a way in ('Take part', 'Open study in new
                # window', or an open 'Start study' dialog) is mid-launch, not
                # a dead slot: both nudges must push the LAUNCH, never 'Cancel
                # participation', because Prolific's dialog hides its confirm
                # link below the fold and abandoning there returns a
                # completable study (run_20261105_181001 lost a GBP 4 eval,
                # run_20261106_124620 a GBP 2,60 one, exactly this way).
                #
                # The orphan BANNER cannot carry that distinction, in either
                # direction. Captured: PRESENT on a never-launched
                # reservation that still offered 'Open study in new window'
                # (run_20261106_124620 screenshot_03), ABSENT on a reserved
                # study with its Start dialog open (fixture study 6aec5848,
                # var/fixtures/prolific/). Keying fresh_unlaunched off it made
                # the flag permanently False on the pages it was meant to
                # protect, which silently disabled the auto-launcher and
                # routed every stuck launch to the cancel steer. The DOM
                # launch control is the honest signal; the banner only says
                # whether the slot is already held.
                orphan_visible = _orphan_reservation_visible()
                fresh_unlaunched = (_launch_control_visible()
                                    and not _saw_external_this_study)

                # (A) Held-slot one-shot informational nudge, branched on the
                #     same launch state so a held-but-launchable study is told
                #     to start, not to cancel.
                if not pending_user_response and not _orphan_nudged:
                    if orphan_visible and fresh_unlaunched:
                        _orphan_nudged = True
                        pending_user_response = _reservation_nudge(
                            True, detail_id)
                    elif orphan_visible:
                        _orphan_nudged = True
                        pending_user_response = (
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
                            "then terminate with NOCODE."
                        )

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
                # Clicks on the top Prolific nav header (the Studies /
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
                    _reserved_loop_count += 1
                # Stuck: current URL matches previous URL and both are this
                # detail page. _prev_here is "" on the very first iteration,
                # so no false positive at run start.
                if (_prev_here
                        and urlparse(_prev_here).path == urlparse(here).path):
                    _reserved_stuck_count += 1

                # A fresh un-launched reservation gets a LOWER stuck threshold:
                # the harness steps in to launch the survey before the vision
                # model fumbles the dialog and drifts toward 'Cancel'
                # (run_20261105_181001 clicked Cancel at step 12; stuck hit 4 at
                # step 8). An orphan/held slot keeps the original 6.
                stuck_limit = 4 if fresh_unlaunched else 6
                if (not _reserved_loop_nudged
                        and not pending_user_response
                        and (_reserved_loop_count >= 2
                             or _reserved_stuck_count >= stuck_limit)):
                    _reserved_loop_nudged = True
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
                        _live({"type": "notice",
                               "text": f"[launch] humanised launch: {status}"})
                        if launched:
                            _survey_url = (res or {}).get("survey_url") or _survey_url
                            pending_user_response = (
                                "The study's external survey has just been opened "
                                "for you in the survey window (the 'Start study' "
                                "dialog was confirmed automatically). You are now "
                                "IN the survey: read the current page and answer "
                                "the questions/trials, then proceed. Do NOT go back "
                                "to the Prolific tab to cancel.")
                            _notify_telegram("🚀 Survey aperta automaticamente\n" + str(_survey_url))
                    if not launched:
                        # Launch not possible (still un-reserved, control
                        # missing, or click blocked): fall back to guidance.
                        pending_user_response = _reservation_nudge(
                            fresh_unlaunched, detail_id)
            else:
                # Off the detail page: counters reset.
                _reserved_loop_url = ""
                _reserved_loop_count = 0
                _reserved_stuck_count = 0
                _reserved_loop_nudged = False
                _orphan_nudged = False

            # Auto-chain Enter after left_click on domains where the user
            # repeatedly asked for the click-then-Enter pattern. Saves one
            # full model round-trip (~16s) per trial on surveys like
            # eval.services. See _should_auto_chain_enter above for gating.
            # Skipped when the disabled-near branch above flagged the click,
            # since Enter on a disabled page would likely re-trigger whatever
            # validation message is already blocking the user.
            if (action_type == "left_click"
                    and not pending_user_response
                    and study_timer.is_active()
                    and _should_auto_chain_enter(here)):
                try:
                    # Humanized delay matching a hand reaching for the
                    # keyboard after a mouse click.
                    cancel.sleep(random.uniform(0.15, 0.35))
                    docker_exec("xdotool", "key", "Return")
                    result_desc = f"{result_desc} +auto-Enter"
                except Exception:
                    # Any failure (bridge gone, xdotool missing) leaves the
                    # click intact and the next step runs the model as
                    # usual -- back to the old behaviour.
                    pass


            # --- diagnostic log entry (separate from model transcript) ---
            diag: dict = {
                "type": "diag", "step": step,
                "url": here,
                "guarded": here_guarded,
                "prev_guarded": _prev_was_guarded,  # value BEFORE this step updated it
                "action": action_type,
                "result": result_desc,
                "prescroll": prescroll_result,
                "timer_active": study_timer.is_active(),
                "timer_elapsed": round(study_timer.elapsed(), 1) if study_timer.is_active() else None,
                "timer_remaining": round(study_timer.remaining(), 1) if study_timer.is_active() else None,
                "timer_min_sec": study_timer.min_seconds if study_timer.is_active() else None,
                "model": last_timing or None,
                "last_guarded_duration": _last_guarded_duration,
            }
            if action_type == "left_click" and here_guarded:
                diag["pre_click_duration"] = pre_click_duration
            if qc_hit:
                diag["qc_flag"] = qc_hit
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
                answer = args.get("answer", "")
                # NOCODE emitted while still on Prolific without ever visiting
                # the external survey or seeing a cancel/submit confirmation
                # means the study is still "assigned" on Prolific. Letting
                # that NOCODE through wedges the next cycle in
                # _wait_for_studies (empty list, slot still held). Narrow
                # conjunctive guard; see guarded_nocode_refusal.
                nocode_block = guarded_nocode_refusal(
                    answer, here, _saw_external_this_study,
                    _saw_cancel_confirm, _nocode_guard_attempts)
                if nocode_block:
                    _nocode_guard_attempts += 1
                    pending_user_response = nocode_block
                    continue
                # The other half of the same mistake, one page later: the
                # survey WAS done, we are back on Prolific, and the model
                # gives up with the 'Confirm submission' panel still open and
                # 'I didn't receive a completion code' one click away. The
                # link's presence is proof the study is unsubmitted; press it
                # rather than argue. See _NOCODE_LINK_JS.
                if (_is_nocode_answer(answer) and _on_guarded_site(here)
                        and not _saw_cancel_confirm
                        and _nocode_link_attempts < 2):
                    _lbox_seen = _nocode_link_box()
                    _codes_now = codes_on_page(_end_page_text())
                    _nc = nocode_link_decision(
                        answer, here, _lbox_seen is not None, _saw_cancel_confirm,
                        _seen_codes, _codes_now, _nocode_link_attempts,
                        _nocode_link_tried)
                    if _nc:
                        _nocode_link_attempts += 1
                    if _nc == "codes":
                        pending_user_response = nocode_codes_refusal(
                            _seen_codes + _codes_now)
                        continue
                    if _nc == "click":
                        _lbox = _scroll_box_into_view(_nocode_link_box)
                        if _lbox and _humanized_click_box(_lbox):
                            _nocode_link_tried = True
                            cancel.sleep(random.uniform(1.2, 2.2))
                            try:
                                _after = pw_bridge.current_url()
                            except Exception:
                                _after = here
                            _live({"type": "notice", "text":
                                   "[completion] the study was not submitted: clicked "
                                   f"Prolific's {_lbox.get('text')!r} -> {_after}"})
                            pending_user_response = (
                                "HARNESS ACTION: you were ending this study as 'no "
                                "completion code' with Prolific's 'Confirm submission' "
                                "panel still open, so the study was NOT submitted. The "
                                f"link {_lbox.get('text')!r} has been clicked for you. "
                                "Look at the screenshot: confirm the submission in the "
                                "dialog that opened (the button that submits / "
                                "continues), wait for the page to say the submission "
                                "was saved ('Awaiting review'), and only then terminate "
                                "NOCODE. If you did NOT actually complete the survey, "
                                "dismiss the dialog and use 'Cancel participation' "
                                "instead.")
                            continue
                        _nc = "retry"
                    if _nc == "retry":
                        pending_user_response = _NOCODE_LINK_REFUSAL
                        continue
                # Ending on the survey host's own thank-you/validation page
                # with the final "submit & return to Prolific" control
                # skipped. Before saying anything to the model, try to finish
                # the job: find that control and click it with the real
                # cursor. See _COMPLETION_RETURN_JS for why prose alone was
                # not enough.
                _page_codes_now = codes_on_page(_end_page_text())
                if (not _completion_return_tried
                        and not answer.startswith("CODE:")
                        and not _on_guarded_site(here)
                        and not _seen_codes and not _page_codes_now):
                    _rbox = _scroll_box_into_view(_completion_return_box)
                    if _rbox:
                        _completion_return_tried = True
                        if _humanized_click_box(_rbox):
                            cancel.sleep(random.uniform(1.2, 2.2))
                            try:
                                _after = pw_bridge.current_url()
                            except Exception:
                                _after = here
                            _live({"type": "notice", "text":
                                   "[completion] the study was not submitted: clicked "
                                   f"the survey's return control ({_rbox.get('why')}, "
                                   f"{_rbox.get('text')!r}) -> {_after}"})
                            pending_user_response = (
                                "HARNESS ACTION: you were ending the task on the survey "
                                "site without ever submitting the study. The control "
                                "that returns it to Prolific "
                                f"({_rbox.get('text')!r}) has been clicked for you and "
                                f"the page is now {_after}. Look at the screenshot: if "
                                "a completion code is shown, terminate with it; if you "
                                "are back on Prolific with the submission registered, "
                                "terminate NOCODE; if the click went nowhere, find and "
                                "click the right control yourself.")
                            continue
                # Prose fallback: no such control was found, or clicking it
                # failed. Nudge once.
                nudge = completion_pushback(
                    answer, here, _seen_codes,
                    _page_codes_now, _completion_pushed_back)
                if nudge:
                    _completion_pushed_back = True
                    pending_user_response = nudge
                    continue
                yield step_event
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
                    study_memory.record(_survey_url,
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
                _user_inject_pending = True
                _record_user_correction(injected)
            # Snapshot `here` for the next step's just_landed check. Done at
            # the END of the step so the comparison at the top of the next
            # step sees the previous URL, not the one we just set.
            _prev_here = here
            # No fixed settle delay here: measured at 0.5s against a ~9.3s
            # step it was ~5% of wall-clock for no benefit -- the next
            # thing the loop does is take a screenshot, and actions that
            # genuinely need settling time (navigation, clicks that load a
            # page) are handled by the model issuing an explicit `wait`.

        yield {"type": "done", "answer": f"max_rounds={max_rounds} reached without terminate",
               "messages": messages}
    finally:
        _live_sink = None
        transcript.close()
        write_run_summary(run_dir)
