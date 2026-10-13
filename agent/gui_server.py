#!/usr/bin/env python3
"""Loopback-only control panel for the Fara browser agent.

Serves a single-page GUI (gui_index.html) that lets you:
  - watch the virtual display live (polled screenshot)
  - click/type/navigate manually (for login setup -- kiosk mode has no
    address bar by design, this replaces it)
  - start/stop an agent run and watch its steps as they happen
  - answer the agent's questions inline when it calls ask_user_question

Stdlib only. Binds to 127.0.0.1 -- never exposed beyond this machine.

Usage:
  python3 gui_server.py [--port 8765]
"""

import argparse
import select
import socket
import shlex
import ctypes
import gzip
import os
import signal
import json
import queue
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cancel
import pw_bridge
import agent_core
import study_cache
from agent_core import run_agent, docker_exec, get_current_url, KEY_MAP, DATA_DIR, CONTAINER

HERE = Path(__file__).parent
MAX_EVENTS = 500

# A second server (the isolated test stack on :8767, or a smoke test) must not
# run the side threads: both talk to things the live instance owns -- the same
# Telegram bot token, where two getUpdates pollers steal each other's
# messages, and the browser, where the submissions updater opens a tab.
TEST_INSTANCE = os.environ.get("FARA_TEST_INSTANCE", "") not in ("", "0")
PRESETS_FILE = Path(os.environ.get("FARA_PRESETS", HERE / "presets.json"))
presets_lock = threading.Lock()

_submissions_cache: dict = {"data": [], "ts": 0.0}
_submissions_lock = threading.Lock()
_submissions_trigger = threading.Event()   # set on each "done" event


def _transcript_files() -> list:
    """Every run transcript, newest last. Runs older than FARA_MAX_RUNS keep
    their transcript gzipped (see agent_core._prune_old_runs), so both forms
    have to be looked for or the stats silently lose their history."""
    root = Path(agent_core.LOG_ROOT)
    found: dict = {}
    try:
        for pat in ("run_*/transcript.jsonl", "run_*/transcript.jsonl.gz"):
            for f in root.glob(pat):
                found.setdefault(f.parent.name, f)
    except Exception:
        return []
    return [found[k] for k in sorted(found)]


def _read_transcript(path) -> str:
    try:
        if str(path).endswith(".gz"):
            with gzip.open(path, "rt", errors="replace") as f:
                return f.read()
        return Path(path).read_text(errors="replace")
    except Exception:
        return ""


def _dbg(msg: str):
    """Write a debug line to submissions_api_debug.txt."""
    try:
        from agent_core import LOG_ROOT
        Path(LOG_ROOT).mkdir(parents=True, exist_ok=True)
        (Path(LOG_ROOT) / "submissions_api_debug.txt").write_text(str(msg)[:4000])
    except Exception:
        pass

def _fetch_submissions() -> list[dict]:
    """Open a temporary tab, read the Prolific submissions page, close the tab.

    Runs only during the inter-cycle idle window (called by _submissions_auto_updater
    when state phase is None), so the agent never notices the ~5-second pause.
    Saves and restores the active tab atomically inside the bridge worker thread.
    """
    import re

    SUBMISSIONS_URL = "https://app.prolific.com/submissions"
    JS = """() => (document.body.innerText || '').split('\\n')
                  .map(s => s.trim()).filter(s => s.length > 0)"""

    def _do(b):
        prev     = b._active
        new_page = prev.context.new_page()
        b._known.append(new_page)
        b._active = new_page
        try:
            new_page.goto(SUBMISSIONS_URL, wait_until="domcontentloaded", timeout=25000)
            new_page.wait_for_timeout(5000)
            return new_page.evaluate(JS) or []
        finally:
            b._active = prev
            b._known  = [p for p in b._known if p is not new_page]
            try:
                new_page.close()
            except Exception:
                pass

    try:
        raw_rows = pw_bridge.run(_do, timeout=45)
    except Exception as e:
        _dbg(f"tab fetch error: {e}")
        return []

    _dbg(f"tab fetch: got {len(raw_rows)} rows")

    STATUS_RE   = re.compile(
        r'^(AWAITING REVIEW|APPROVED|RETURNED|REJECTED|TIMED OUT|ACTIVE|SCREENED OUT)$',
        re.IGNORECASE)
    MONEY_RE    = re.compile(r'^(screened out )?reward', re.IGNORECASE)
    REWARD_AMT  = re.compile(r'([£$€])(\d+\.?\d*)')
    BY_RE       = re.compile(r'^by\s+.+', re.IGNORECASE)
    DATELINE_RE = re.compile(r'^\d{1,2}\s+\w{3}\s+\d{4}', re.IGNORECASE)
    SKIP_LINE   = re.compile(
        r'^(approved by|read more|submissions?|summary|total|filters?|sort|search'
        r'|balance|pending|about you|messages|studies|refer|projects?'
        r'|date|status|reward|actions?|study name|\d+\s*submissions?'
        r'|skip to main|time taken).*$', re.IGNORECASE)

    parsed, i = [], 0
    while i < len(raw_rows):
        line = raw_rows[i]
        if STATUS_RE.match(line):
            status = line.title()
            title, date_full, reward_str, currency = "", "", "", "£"
            for j in range(i - 1, max(i - 10, -1), -1):
                prev = raw_rows[j]
                if DATELINE_RE.match(prev):
                    date_full = prev.strip()
                elif MONEY_RE.match(prev):
                    m = REWARD_AMT.search(prev)
                    if m:
                        currency, reward_str = m.group(1), m.group(2)
                elif BY_RE.match(prev) or SKIP_LINE.match(prev):
                    continue
                elif not title and len(prev) > 4:
                    title = prev[:80]
                    break
            if title:
                parsed.append({
                    "title": title, "status": status,
                    "date": date_full.split(",")[0].strip(),
                    "date_full": date_full,
                    "reward": reward_str, "currency": currency,
                })
        i += 1
    return parsed


def _agent_submissions() -> list[dict]:
    """Return cached submissions (updated by background thread). Never blocks."""
    from agent_core import LOG_ROOT
    import json as _json

    with _submissions_lock:
        all_subs = list(_submissions_cache["data"])

    # Count runs where the agent actually submitted something (CODE or NOCODE)
    completed = 0
    try:
        for p in list(reversed(_transcript_files()))[:200]:
            try:
                for line in _read_transcript(p).splitlines():
                    d = _json.loads(line)
                    if d.get("action_type") == "terminate":
                        ans = d.get("args", {}).get("answer", "")
                        if ans.startswith("CODE:") or ans == "NOCODE":
                            completed += 1
                            break
            except Exception:
                pass
    except Exception:
        pass

    # Show submissions from the last 24h (covers what the agent did today).
    # Fall back to the top max(completed,5) if nothing recent is found.
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(hours=24)
    recent = []
    for s in all_subs:
        df = s.get("date_full", "")
        if df:
            try:
                try:
                    d = datetime.strptime(df, "%d %b %Y, %H:%M")
                except ValueError:
                    d = datetime.strptime(df, "%d %b %Y")
                if d >= cutoff:
                    recent.append(s)
            except Exception:
                pass
    return recent if recent else all_subs[:max(completed, 5)]


def load_presets() -> list:
    with presets_lock:
        if not PRESETS_FILE.exists():
            return []
        return json.loads(PRESETS_FILE.read_text())


def save_preset(preset: dict):
    with presets_lock:
        presets = []
        if PRESETS_FILE.exists():
            presets = json.loads(PRESETS_FILE.read_text())
        presets = [p for p in presets if p["name"] != preset["name"]]
        presets.append(preset)
        PRESETS_FILE.write_text(json.dumps(presets, indent=2))


def delete_preset(name: str):
    with presets_lock:
        if not PRESETS_FILE.exists():
            return
        presets = json.loads(PRESETS_FILE.read_text())
        presets = [p for p in presets if p["name"] != name]
        PRESETS_FILE.write_text(json.dumps(presets, indent=2))


state_lock = threading.Lock()
state = {
    "running": False,
    "events": [],
    "pending_question": None,
    "stop_event": None,
    "answer_queue": None,
    "inject_message": None,
}



class _Hub:
    """Fan-out of what happens in the agent to every connected GUI (SSE).

    Three kinds of message: "ev" (a durable, numbered feed event), "state"
    (run/stop/stats/question/url snapshot) and "live" (ephemeral: the
    current phase, the model's tokens as they are generated, the point the
    agent is about to click). Each subscriber has its own bounded queue so
    one stalled browser tab can never slow the agent or the others."""

    def __init__(self):
        self.lock = threading.Lock()
        self.subs = set()

    def subscribe(self):
        q = queue.Queue(maxsize=3000)
        q.overflowed = False
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def count(self) -> int:
        with self.lock:
            return len(self.subs)

    def publish(self, kind: str, data: dict):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait((kind, data))
            except queue.Full:
                # A viewer that cannot keep up drops ephemeral tokens; if it
                # would have to drop something durable it is disconnected and
                # resyncs from lastSeq when its EventSource reconnects.
                if kind != "live":
                    q.overflowed = True


hub = _Hub()


class _FrameStreamer:
    """Live view as an MJPEG stream.

    One persistent `docker exec` runs a capture loop inside the browser
    container. The ~65 ms the old per-frame `docker exec scrot` took was
    almost all process start-up (scrot itself needs ~9 ms there), so a
    long-lived loop gives as many frames/s as wanted (default 5, FARA_STREAM_FPS) that follow the real X display
    -- including native popups a CDP screencast would miss. It runs only
    while somebody is watching and stops a few seconds after the last
    viewer leaves. Identical consecutive frames are not re-sent."""

    FPS = float(os.environ.get("FARA_STREAM_FPS", "5"))
    QUALITY = int(os.environ.get("FARA_STREAM_QUALITY", "60"))
    IDLE_STOP_S = 4.0

    def __init__(self):
        self.cond = threading.Condition()
        self.frame, self.seq, self.clients = None, 0, 0
        self.proc, self.alive = None, False
        self.no_clients_since = None

    def _cmd(self) -> str:
        f = "/dev/shm/_fara_live.jpg"
        # The pause between frames is a `read` on stdin: when the GUI server
        # goes away (even killed outright) docker closes stdin, read returns
        # EOF and the loop ends. Writes to stdout alone do NOT detect that --
        # measured: after the client died the loop kept capturing forever.
        return (f"f={f}; while :; do scrot -o -q {self.QUALITY} $f 2>/dev/null && "
                f"{{ printf 'F %s\\n' $(stat -c %s $f) || exit; cat $f || exit; }}; "
                f"read -t {1.0 / self.FPS:.3f} -N 1 _; [ $? -gt 128 ] || exit; done")

    def _ensure_running(self):
        with self.cond:
            if self.alive:
                return
            self.alive = True
            self.no_clients_since = None
        try:   # a loop orphaned by an earlier, killed server would double the capture load
            subprocess.run(["docker", "exec", CONTAINER, "pkill", "-f", "[_]fara_live"],
                           capture_output=True, timeout=5)
        except Exception:
            pass
        threading.Thread(target=self._reader, daemon=True, name="frame-streamer").start()

    def _reader(self):
        proc = None
        try:
            proc = subprocess.Popen(
                ["docker", "exec", "-i", CONTAINER, "bash", "-c", self._cmd()],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            with self.cond:
                self.proc = proc
            out = proc.stdout
            while True:
                head = out.readline()
                if not head.startswith(b"F "):
                    break
                n = int(head[2:])
                data = out.read(n)
                if len(data) != n:
                    break
                with self.cond:
                    if data != self.frame:
                        self.frame = data
                        self.seq += 1
                        self.cond.notify_all()
                    if self.clients == 0:
                        if self.no_clients_since is None:
                            self.no_clients_since = time.time()
                        elif time.time() - self.no_clients_since > self.IDLE_STOP_S:
                            break
                    else:
                        self.no_clients_since = None
        except Exception:
            pass
        finally:
            try:
                if proc is not None:
                    proc.kill()   # the in-container loop exits on its next write (|| exit)
                    proc.wait(timeout=2)
            except Exception:
                pass
            with self.cond:
                self.alive, self.proc = False, None
                self.cond.notify_all()

    def add_client(self):
        with self.cond:
            self.clients += 1
            self.no_clients_since = None
        self._ensure_running()

    def remove_client(self):
        with self.cond:
            self.clients = max(0, self.clients - 1)

    def wait_frame(self, last_seq: int, timeout: float):
        """Newest frame after last_seq, or (last_seq, None) on timeout."""
        self._ensure_running()
        deadline = time.time() + timeout
        died = False
        with self.cond:
            while True:
                if self.frame is not None and self.seq != last_seq:
                    return self.seq, self.frame
                left = deadline - time.time()
                if left <= 0:
                    return last_seq, None
                self.cond.wait(min(left, 0.5))
                if not self.alive:
                    died = True
                    break
        if died:   # capture process gone (container restarted?): retry gently
            time.sleep(0.5)
            self._ensure_running()
        return last_seq, None


streamer = _FrameStreamer()


def _status_payload(since: int = 0, with_events: bool = True, fresh_url: bool = True) -> dict:
    with state_lock:
        payload = {
            "running": state["running"],
            "stopping": state.get("stopping", False),
            "started_at": state.get("started_at"),
            "run_id": state.get("run_id", 0),
            "loop": state.get("loop", False),
            "last_seq": state.get("seq", 0),
            "stats": dict(state.get("stats", {})),
            "pending_question": state["pending_question"],
            "phase": state.get("phase"),
            "study_timer": state.get("study_timer"),
            "skip_av": state.get("skip_av", False),
        }
        if with_events:
            payload["events"] = [e for e in state["events"] if e.get("seq", 0) > since]
    payload["url"] = _cached_url() if fresh_url else _url_cache["v"]
    payload["speed"] = agent_core.speed_state()
    return payload


def _publish_state():
    hub.publish("state", _status_payload(with_events=False, fresh_url=False))


def _on_live(msg: dict):
    """agent_core.live_hook: forward what the agent is doing right now."""
    msg = dict(msg, ts=time.time())
    if msg.get("type") == "phase":
        with state_lock:
            state["phase"] = msg
    if msg.get("type") in ("timer", "timer_start"):
        with state_lock:
            state["study_timer"] = {
                "elapsed": msg.get("elapsed", 0),
                "remaining": msg.get("remaining", 0),
                "min_seconds": msg.get("min_seconds", 0),
            }
    hub.publish("live", msg)
    if msg.get("type") == "speed":
        # The mode can change from the GUI, from Telegram or on its own when a
        # study overruns: push the whole state so every client agrees on it.
        _publish_state()


agent_core.live_hook = _on_live

_ticker_started = False


def _url_ticker():
    """Publishes the page URL when it changes. Runs only while a GUI is
    connected, and reads through the same short-timeout cache the status
    endpoint always used."""
    last = None
    while True:
        time.sleep(1.2)
        if hub.count() == 0:
            continue
        u = _cached_url()
        if u != last:
            last = u
            _publish_state()


def _compact_batch_result(r: dict) -> dict:
    """What the GUI needs from a batch-fill result. The full result carries
    every answer twice (answers + verified_state), ~6 KB per event, and the
    status endpoint used to resend the whole event list every second."""
    out = {k: r.get(k) for k in ("field_count", "already_answered_count", "newly_answered_count",
                                 "undecided", "mismatches", "apply_errors", "skipped", "reason")
           if k in r}
    out["answers"] = {k: (v if not isinstance(v, str) or len(v) < 140 else v[:137] + "...")
                      for k, v in (r.get("answers") or {}).items()}
    adv = r.get("advance")
    if adv:
        out["advance"] = {k: adv.get(k) for k in ("label", "advanced", "attempted", "new_text", "reason")}
    return out


def _push_event(event: dict):
    with state_lock:
        st = state.setdefault("stats", {"cycle": 0, "steps": 0, "batch_fills": 0, "errors": 0})
        t = event.get("type")
        if t == "cycle": st["cycle"] = event.get("n", st["cycle"] + 1)
        elif t == "step": st["steps"] += 1
        elif t == "batch_fill": st["batch_fills"] += 1
        elif t == "error": st["errors"] += 1
        state["seq"] = state.get("seq", 0) + 1
        event["seq"] = state["seq"]
        event["ts"] = time.time()
        if t == "batch_fill" and isinstance(event.get("result"), dict):
            event["result"] = _compact_batch_result(event["result"])
        state["events"].append(event)
        if len(state["events"]) > MAX_EVENTS:
            state["events"][:] = state["events"][-MAX_EVENTS:]
        if t in ("step", "batch_fill", "done", "error", "question"):
            state["phase"] = None   # whatever was in flight is now a feed row
        if t == "done":
            state.pop("study_timer", None)
        if t == "done":
            _submissions_trigger.set()   # wake the submissions updater
    hub.publish("ev", event)
    _publish_state()


def _pop_injected():
    with state_lock:
        text = state["inject_message"]
        state["inject_message"] = None
        return text


def run_one_cycle(task: str, url: str, max_rounds: int, stop_event, answer_queue,
                   messages: list = None, batch_fill: bool = False, wander_seconds: float = 0,
                   skip_av: bool = False):
    """Drives a single agent_core.run_agent generator to completion.

    Returns (ok, messages): ok is False if the run was aborted via the stop
    sentinel (an unanswered question plus /agent/stop); messages is the
    live conversation, threaded into the next cycle so the model keeps
    memory of what it already did across loop cycles. agent_core verifies
    and trims it against the real tokenizer at the start of a continuing
    cycle and after every response, so an oversized carried-over history
    never has to be handled here.
    """
    try:
        return _drive_cycle(task, url, max_rounds, stop_event, answer_queue, messages, batch_fill,
                            wander_seconds, skip_av)
    except cancel.Cancelled:
        # Stop pressed while the agent was blocked on a model call or a
        # wait: cancel.cancel_all() cut the request, the generator is gone.
        _push_event({"type": "done", "answer": "STOPPED"})
        return False, messages


def _drive_cycle(task, url, max_rounds, stop_event, answer_queue, messages, batch_fill,
                  wander_seconds=0, skip_av=False):
    gen = run_agent(task, url, max_rounds, stop_event=stop_event, messages=messages,
                     batch_fill=batch_fill, wander_seconds=wander_seconds, skip_av=skip_av)
    try:
        event = next(gen)
    except StopIteration:
        return True, messages

    while True:
        latest_messages = event.get("messages", messages)
        # "messages" carries the full conversation incl. base64 screenshots --
        # fine to thread internally, but far too heavy to store/poll in the
        # GUI's event feed, so it's stripped before pushing.
        _push_event({k: v for k, v in event.items() if k != "messages"})
        if event["type"] == "done":
            return True, latest_messages
        if event["type"] == "question":
            with state_lock:
                state["pending_question"] = event["question"]
            _publish_state()
            answer = answer_queue.get()
            with state_lock:
                state["pending_question"] = None
            _publish_state()
            if answer is None:
                gen.close()
                _push_event({"type": "done", "answer": "STOPPED"})
                return False, latest_messages
            event = gen.send(answer)
        else:
            injected = _pop_injected()
            if injected:
                _push_event({"type": "inject", "text": injected})
            try:
                event = gen.send(injected) if injected else next(gen)
            except StopIteration:
                return True, latest_messages


_url_cache = {"t": 0.0, "v": None}


def _cached_url():
    """Current page URL for the GUI header, refreshed at most every 2s and
    never allowed to stall the status poll behind a busy browser bridge."""
    if time.time() - _url_cache["t"] > 2.0:
        _url_cache["t"] = time.time()
        try:
            _url_cache["v"] = pw_bridge.run(lambda b: b.active_page().url, timeout=0.4)
        except Exception:
            pass
    return _url_cache["v"]


def _dashboard_for(url: str) -> str:
    """Page each loop cycle should return to. If the run was started while
    a single study's detail page was open (e.g. one with a reserved place),
    returning to THAT every cycle is wrong -- it is not the list of
    studies. Map such pages to the site's study list."""
    import re
    from urllib.parse import urlparse
    pr = urlparse(url or "")
    if agent_core.is_forbidden_url(url):
        return agent_core.safe_page_for(url)
    if re.fullmatch(r"/studies/[0-9a-fA-F]{24}/?", pr.path or ""):
        return f"{pr.scheme}://{pr.netloc}/studies"
    # Submission-complete and waiting-room pages: the agent's job there is
    # done; returning to them on the next cycle would confuse the model.
    if re.match(r"^/(submissions|waitingroom|participant)", pr.path or ""):
        return f"{pr.scheme}://{pr.netloc}/studies"
    return url


def _inhibit_sleep():
    """Keep the laptop awake with the lid closed, but only while a run is
    in progress. A logind "block" inhibitor on lid-switch handling and
    sleep -- the same mechanism GNOME uses when an external monitor is
    attached -- held by a child process that lives exactly as long as the
    run. No system settings are changed; when the run ends (or the server
    dies) the inhibitor disappears and lid-close suspends as usual."""
    try:
        return subprocess.Popen(
            ["systemd-inhibit", "--what=handle-lid-switch:sleep:idle",
             "--who=Fara agent", "--why=Agent run in progress", "--mode=block",
             "sleep", "infinity"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            # Die with the server even on a hard kill/crash, so an orphaned
            # inhibitor can never keep the laptop from sleeping forever.
            preexec_fn=lambda: ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM))
    except Exception:
        return None


# How long to wait before retrying after a cycle blew up, by consecutive
# failure count. A model timeout clears in seconds, a dead browser container
# does not, so the wait grows instead of hammering the same error.
ERROR_BACKOFF = (30, 60, 120, 300)


def _backoff(consecutive_errors: int) -> int:
    """Seconds to wait before retrying, by how many cycles failed in a row."""
    i = max(1, int(consecutive_errors)) - 1
    return ERROR_BACKOFF[min(i, len(ERROR_BACKOFF) - 1)]


def agent_driver(task: str, url: str, max_rounds: int, loop: bool, poll_interval: float,
                  batch_fill: bool = False, wander_seconds: float = 0, skip_av: bool = False):
    stop_event = state["stop_event"]
    answer_queue = state["answer_queue"]
    # Each cycle starts a FRESH conversation, with no memory of earlier
    # cycles. Threading history made the model anchor on old conclusions:
    # with 3 studies on screen it answered "still shows exactly one
    # available survey" (what an earlier cycle had seen). Measured on the
    # same screenshot: with prior history or even with a few lines of text
    # notes about earlier cycles it repeated the wrong count 3/3 times;
    # with no prior context it saw all three every time. Nothing is lost:
    # Prolific drops completed studies from the list, and eligibility is
    # cheap to re-judge each cycle.
    home_url = url  # where each new cycle starts from; captured below if not given
    inhibitor = _inhibit_sleep()
    try:
        cycle = 0
        errors_in_a_row = 0
        while True:
            cycle += 1
            if loop:
                _push_event({"type": "cycle", "n": cycle})
            if cycle == 1 and not url:
                # Remember the page the user started us on (e.g. the
                # studies list, after a manual login) so every later cycle
                # can return to it.
                start_page_url = _dashboard_for(get_current_url())
            # Navigate (if requested at all) only on the first cycle; later
            # cycles continue from wherever the page is (refreshed below),
            # instead of re-navigating to the start URL every time.
            try:
                ok, _ = run_one_cycle(task, url if cycle == 1 else "", max_rounds,
                                      stop_event, answer_queue,
                                      messages=None, batch_fill=batch_fill,
                                      wander_seconds=wander_seconds, skip_av=skip_av)
                errors_in_a_row = 0
            except Exception as e:
                # One bad cycle must not end a 24/7 run. A model timeout, a CDP
                # hiccup or a browser that died gets reported, waited out and
                # retried from the studies list; only a single (non-loop) run
                # still fails outright, where there is no next cycle to save.
                if not loop or stop_event.is_set():
                    raise
                errors_in_a_row += 1
                delay = _backoff(errors_in_a_row)
                _push_event({"type": "error",
                             "message": f"Ciclo {cycle} interrotto — {type(e).__name__}: "
                                        f"{str(e)[:200]} · riprovo tra {delay}s "
                                        f"({errors_in_a_row}° errore di fila)"})
                agent_core._notify_telegram(
                    f"⚠️ Ciclo {cycle} fallito ({errors_in_a_row}° di fila)\n"
                    f"{type(e).__name__}: {str(e)[:150]}\n"
                    f"Riprovo tra {delay}s\n\n{traceback.format_exc()[-300:]}")
                if stop_event.wait(delay):
                    return
                # Back to a known page, whatever state the browser is in. If
                # the bridge itself can't do it (a wedged tab makes every
                # Playwright call hang), replace the tab over plain CDP HTTP.
                target = home_url or agent_core.studies_url()
                try:
                    pw_bridge.reset_to(target)
                    pw_bridge.wait_for_content()
                except Exception:
                    if pw_bridge.recycle_tab(target):
                        _push_event({"type": "notice",
                                     "text": "Scheda del browser bloccata: sostituita con una nuova."})
                continue
            if not home_url:
                home_url = start_page_url
            if not ok or not loop or stop_event.is_set():
                break
            if stop_event.wait(poll_interval):   # wakes the instant Stop is pressed
                return
            # Go back to the starting page (the dashboard) before the next
            # cycle. A plain reload of the CURRENT page wasn't enough: after
            # a finished survey the current page is the "submission
            # received" screen, so every later cycle reloaded that, saw
            # "already done" and terminated -- the loop never got back to
            # the studies list. Navigating also refreshes SPA dashboards.
            try:
                if home_url:
                    pw_bridge.reset_to(home_url)
                else:
                    pw_bridge.reload()
                pw_bridge.wait_for_content()
            except Exception:
                pass
    except Exception as e:
        _push_event({"type": "error", "message": str(e)})
        import traceback
        tb = traceback.format_exc()[-300:]
        agent_core._notify_telegram(f"❌ Errore agent!\n{type(e).__name__}: {str(e)[:150]}\n\n{tb}")
    finally:
        if inhibitor is not None:
            inhibitor.terminate()
        with state_lock:
            state["running"] = False
            state["stopping"] = False
            state["phase"] = None
        hub.publish("live", {"type": "phase", "phase": "idle", "ts": time.time()})
        _publish_state()


def _build_dashboard() -> str:
    """Read all run transcripts and return a self-contained HTML dashboard."""
    from collections import defaultdict
    from datetime import datetime

    runs = _transcript_files()
    records = []
    for tf in runs:
        run_name = tf.parent.name
        try:
            started = datetime.strptime(run_name.replace("run_", ""), "%Y%m%d_%H%M%S")
        except Exception:
            continue
        lines = _read_transcript(tf).splitlines()
        steps, outcome, answer = 0, "INCOMPLETE", ""
        for line in lines:
            try:
                d = json.loads(line)
            except Exception:
                continue
            s = d.get("step", 0)
            if isinstance(s, int):
                steps = max(steps, s)
            at = d.get("action_type", "")
            if at == "terminate":
                ans = d.get("args", {}).get("answer", "")
                answer = ans
                if ans.startswith("CODE:"):
                    outcome = "CODE"
                elif ans == "NOCODE":
                    outcome = "NOCODE"
                elif ans == "NONE":
                    outcome = "NONE"
                else:
                    outcome = "TERMINATE"
        screens = list(tf.parent.glob("screenshot_*.png"))
        ended = started
        if screens:
            ended = datetime.fromtimestamp(max(s.stat().st_mtime for s in screens))
        duration_s = max(0, round((ended - started).total_seconds()))
        records.append({"started": started, "ended": ended,
                        "duration_s": duration_s, "steps": steps,
                        "outcome": outcome, "answer": answer[:80]})

    total = len(records)
    completed = [r for r in records if r["outcome"] in ("CODE", "NOCODE")]
    none_runs = sum(1 for r in records if r["outcome"] == "NONE")
    incomplete_runs = sum(1 for r in records if r["outcome"] not in ("CODE", "NOCODE", "NONE"))
    avg_dur = round(sum(r["duration_s"] for r in completed) / max(1, len(completed)))

    # CPH: use actual elapsed time from first run start to last run end
    first_dt = records[0]["started"] if records else None
    last_dt  = records[-1]["ended"]  if records else None
    if first_dt and last_dt:
        elapsed_h = max(0.01, (last_dt - first_dt).total_seconds() / 3600)
        cph = f"{total / elapsed_h:.1f}"
        session_str = first_dt.strftime("%-d %b %H:%M") + " → " + last_dt.strftime("%H:%M")
    else:
        cph = "—"
        session_str = "—"

    # Hourly buckets keyed by "YYYY-MM-DDTHH"
    hourly: dict = defaultdict(lambda: {"runs": 0, "none": 0, "completed": 0, "incomplete": 0,
                                         "first_start": None})
    for r in records:
        h = r["started"].strftime("%Y-%m-%dT%H")
        hourly[h]["runs"] += 1
        if hourly[h]["first_start"] is None:
            hourly[h]["first_start"] = r["started"]
        if r["outcome"] == "NONE":
            hourly[h]["none"] += 1
        elif r["outcome"] in ("CODE", "NOCODE"):
            hourly[h]["completed"] += 1
        else:
            hourly[h]["incomplete"] += 1

    hours_sorted = sorted(hourly.keys())
    max_runs = max((hourly[h]["runs"] for h in hours_sorted), default=1)
    CHART_H = 160  # px for tallest bar

    def fmt_time(dt):
        return dt.strftime("%H:%M") if dt else "—"

    def fmt_dt(dt):
        return dt.strftime("%-d %b %H:%M") if dt else "—"

    def fmt_dur(s):
        if s < 60: return f"{s}s"
        return f"{s // 60}m {s % 60:02d}s"

    def pct(n, d):
        return f"{n/max(1,d)*100:.1f}%"

    rate = pct(len(completed), total)
    none_pct = pct(none_runs, total)
    now_str = datetime.now().strftime("%H:%M:%S")
    with state_lock:
        agent_running = state["running"]
    agent_badge = '<span class="badge live">● in esecuzione</span>' if agent_running \
                  else '<span class="badge idle">○ fermo</span>'
    stop_btn_html = '<button class="stop-btn" onclick="stopAgent()">⏹ Ferma</button>' \
                    if agent_running else ""

    # --- bar chart HTML (flex column, bottom-aligned, with count labels) ---
    bar_html = ""
    for h in hours_sorted:
        d = hourly[h]
        bar_h = max(4, round((d["runs"] / max_runs) * CHART_H))
        # within bar: NONE bottom, completed middle, incomplete top
        # use flex proportions for the fill
        n_none = d["none"] or 0
        n_comp = d["completed"] or 0
        n_inc  = d["incomplete"] or 0
        # label: show actual first run time for clarity
        first_start = d["first_start"]
        if first_start:
            label = first_start.strftime("%H:%M")
        else:
            label = h[11:13] + ":00"
        segs = ""
        if n_inc:
            segs += f'<div style="flex:{n_inc};background:#f5a623;min-height:3px"></div>'
        if n_comp:
            segs += f'<div style="flex:{n_comp};background:#34c77b;min-height:4px"></div>'
        if n_none:
            segs += f'<div style="flex:{n_none};background:#252a38"></div>'
        bar_html += f"""<div class="bw">
  <div class="bcount">{d['runs']}</div>
  <div class="bstack" style="height:{bar_h}px">{segs}</div>
  <div class="blabel">{label}</div>
</div>"""

    # --- completed studies table ---
    rows_html = ""
    for r in reversed(completed):
        chip_cls = "chip-CODE" if r["outcome"] == "CODE" else "chip-NOCODE"
        rows_html += f"""<tr>
  <td class="mono">{fmt_dt(r['started'])}</td>
  <td><span class="chip {chip_cls}">{r['outcome']}</span></td>
  <td>{fmt_dur(r['duration_s'])}</td>
  <td class="mono muted">{r['steps']} step</td>
  <td class="mono ans">{r['answer']}</td></tr>"""
    if not rows_html:
        rows_html = '<tr><td colspan="5" class="muted center pad20">Nessuno studio completato ancora</td></tr>'

    # --- outcome bar: split into meaningful segments ---
    # use sqrt scale to prevent NONE from eating 99% of the bar
    import math
    def sqrt_flex(n):
        return round(math.sqrt(max(0, n)) * 10)

    sf_none = sqrt_flex(none_runs)
    sf_comp = sqrt_flex(len(completed))
    sf_inc  = sqrt_flex(incomplete_runs)

    avg_dur_completed = fmt_dur(avg_dur) if completed else "—"

    return f"""<!doctype html><html lang="it"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Fara Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Inter:wght@400;500;600;700&display=swap">
<style>
:root {{
  --bg: #0d0f16;
  --surface: #13161f;
  --surface2: #1a1e2b;
  --border: #222636;
  --border2: #2d3248;
  --fg: #dde3f4;
  --muted: #6b7491;
  --accent: #5b8df8;
  --green: #2ecc71;
  --green-dim: #1a3d2b;
  --yellow: #f0a500;
  --yellow-dim: #2e2310;
  --red: #e05454;
  --mono: "IBM Plex Mono", monospace;
  --sans: "Inter", system-ui, sans-serif;
  color-scheme: dark;
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: var(--bg);
  color: var(--fg);
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.55;
  padding: 24px 28px;
  max-width: 1200px;
  margin: 0 auto;
}}

/* ── header ── */
.hdr {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 28px;
  gap: 12px;
  flex-wrap: wrap;
}}
.hdr-left {{ display: flex; flex-direction: column; gap: 2px; }}
.hdr h1 {{
  font-size: 20px; font-weight: 700; letter-spacing: -.03em;
  color: var(--fg);
}}
.hdr h1 em {{ color: var(--accent); font-style: normal; }}
.session-str {{ font-family: var(--mono); font-size: 11px; color: var(--muted); }}
.hdr-right {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
.badge {{
  font-family: var(--mono); font-size: 11px;
  background: var(--surface2); border: 1px solid var(--border2);
  border-radius: 6px; padding: 4px 10px; color: var(--muted);
}}
.badge.live {{ border-color: var(--green); color: var(--green); }}
.badge.idle {{ border-color: var(--border2); color: var(--muted); }}
.refresh-btn {{
  font-family: var(--mono); font-size: 11px; padding: 4px 10px;
  background: var(--surface2); border: 1px solid var(--border2);
  border-radius: 6px; color: var(--muted); cursor: pointer;
  text-decoration: none;
}}
.refresh-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
.stop-btn {{
  font-family: var(--mono); font-size: 11px; padding: 4px 12px;
  background: #2a1515; border: 1px solid #6b2020;
  border-radius: 6px; color: #e05454; cursor: pointer;
  transition: background .15s, border-color .15s;
}}
.stop-btn:hover {{ background: #3d1a1a; border-color: var(--red); }}

/* ── tiles ── */
.tiles {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}}
.tile {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}}
.tile-label {{
  font-size: 10px; text-transform: uppercase; letter-spacing: .08em;
  color: var(--muted); font-weight: 600;
}}
.tile-value {{
  font-family: var(--mono); font-size: 30px; font-weight: 600;
  line-height: 1; font-variant-numeric: tabular-nums;
}}
.tile-sub {{ font-size: 11px; color: var(--muted); margin-top: 2px; }}
.tile.accent .tile-value {{ color: var(--accent); }}
.tile.green  .tile-value {{ color: var(--green); }}
.tile.yellow .tile-value {{ color: var(--yellow); }}
.tile.green  {{ border-color: var(--green-dim); }}
.tile.yellow {{ border-color: var(--yellow-dim); }}

/* ── section ── */
.section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 20px 22px;
  margin-bottom: 16px;
}}
.section-title {{
  font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
  color: var(--muted); font-weight: 600; margin-bottom: 18px;
  display: flex; align-items: center; gap: 8px;
}}

/* ── outcome split bar ── */
.outcome-wrap {{ display: flex; flex-direction: column; gap: 12px; }}
.outcome-bar {{
  display: flex; height: 10px; border-radius: 5px; overflow: hidden;
  background: var(--border); gap: 2px;
}}
.ob-seg {{ border-radius: 3px; transition: flex .3s; }}
.ob-none     {{ background: #2d3248; }}
.ob-done     {{ background: var(--green); }}
.ob-inc      {{ background: var(--yellow); }}
.outcome-legend {{
  display: flex; gap: 20px; flex-wrap: wrap;
}}
.ol-item {{
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--muted);
}}
.ol-dot {{
  width: 9px; height: 9px; border-radius: 2px; flex-shrink: 0;
}}
.ol-item strong {{ color: var(--fg); font-weight: 600; }}

/* ── bar chart ── */
.chart-wrap {{
  display: flex;
  align-items: flex-end;
  gap: 6px;
  height: {CHART_H + 48}px;
  padding-bottom: 30px;
  position: relative;
}}
.bw {{
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  height: 100%;
  gap: 0;
  position: relative;
}}
.bcount {{
  font-family: var(--mono); font-size: 9px; color: var(--muted);
  margin-bottom: 3px; text-align: center;
  white-space: nowrap;
}}
.bstack {{
  width: 100%;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  border-radius: 4px 4px 0 0;
  overflow: hidden;
  min-height: 4px;
}}
.blabel {{
  position: absolute; bottom: -22px;
  font-family: var(--mono); font-size: 9px; color: var(--muted);
  white-space: nowrap; text-align: center;
}}
.chart-legend {{
  display: flex; gap: 18px; margin-top: 14px; flex-wrap: wrap;
}}
.cl-item {{
  display: flex; align-items: center; gap: 5px;
  font-size: 11px; color: var(--muted);
}}
.cl-dot {{ width: 9px; height: 9px; border-radius: 2px; flex-shrink: 0; }}

/* ── table ── */
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
thead th {{
  text-align: left; font-size: 10px; text-transform: uppercase;
  letter-spacing: .08em; color: var(--muted); padding: 0 10px 12px;
  font-weight: 600; border-bottom: 1px solid var(--border);
}}
tbody td {{
  padding: 11px 10px;
  border-bottom: 1px solid var(--border);
  font-variant-numeric: tabular-nums;
  vertical-align: middle;
}}
tbody tr:last-child td {{ border-bottom: none; }}
tbody tr:hover td {{ background: var(--surface2); }}
.chip {{
  display: inline-block; padding: 3px 8px; border-radius: 5px;
  font-size: 11px; font-weight: 700; font-family: var(--mono);
  letter-spacing: .03em;
}}
.chip-CODE   {{ background: var(--green-dim); color: var(--green); }}
.chip-NOCODE {{ background: #1c2a42; color: var(--accent); }}
.mono {{ font-family: var(--mono); font-size: 12px; }}
.muted {{ color: var(--muted); }}
.ans {{ color: var(--muted); max-width: 260px; overflow: hidden;
        text-overflow: ellipsis; white-space: nowrap; }}
.center {{ text-align: center; }}
.pad20 {{ padding: 20px !important; }}

/* ── footer ── */
.footer {{
  font-size: 11px; color: var(--muted); text-align: right;
  margin-top: 16px; display: flex; justify-content: space-between;
  flex-wrap: wrap; gap: 8px;
}}
.footer a {{ color: var(--accent); text-decoration: none; }}
.footer a:hover {{ text-decoration: underline; }}
</style>
</head><body>

<div class="hdr">
  <div class="hdr-left">
    <h1>Fara <em>Agent</em> Dashboard</h1>
    <span class="session-str">{session_str}</span>
  </div>
  <div class="hdr-right">
    {agent_badge}
    <span class="badge">aggiornato {now_str}</span>
    <a class="refresh-btn" href="/dashboard">↺ aggiorna</a>
    {stop_btn_html}
  </div>
</div>

<div class="tiles">
  <div class="tile accent">
    <div class="tile-label">Cicli totali</div>
    <div class="tile-value">{total}</div>
    <div class="tile-sub">{f"{elapsed_h:.1f}h attive" if records else "—"}</div>
  </div>
  <div class="tile green">
    <div class="tile-label">Completati</div>
    <div class="tile-value">{len(completed)}</div>
    <div class="tile-sub">{rate} dei cicli</div>
  </div>
  <div class="tile">
    <div class="tile-label">NONE</div>
    <div class="tile-value">{none_runs}</div>
    <div class="tile-sub">{none_pct} — nessuno studio</div>
  </div>
  <div class="tile">
    <div class="tile-label">Durata media</div>
    <div class="tile-value" style="font-size:22px;padding-top:4px">{avg_dur_completed}</div>
    <div class="tile-sub">per studio completato</div>
  </div>
  <div class="tile yellow">
    <div class="tile-label">Cicli / ora</div>
    <div class="tile-value">{cph}</div>
    <div class="tile-sub">sul tempo effettivo</div>
  </div>
  <div class="tile">
    <div class="tile-label">Incompleti</div>
    <div class="tile-value">{incomplete_runs}</div>
    <div class="tile-sub">cicli senza esito</div>
  </div>
</div>

<div class="section">
  <div class="section-title">Distribuzione esiti</div>
  <div class="outcome-wrap">
    <div class="outcome-bar">
      <div class="ob-seg ob-none" style="flex:{sf_none}"></div>
      <div class="ob-seg ob-done" style="flex:{sf_comp}"></div>
      <div class="ob-seg ob-inc"  style="flex:{sf_inc}"></div>
    </div>
    <div class="outcome-legend">
      <div class="ol-item">
        <div class="ol-dot" style="background:#2d3248"></div>
        NONE &nbsp;<strong>{none_runs}</strong>&nbsp;<span>({none_pct})</span>
      </div>
      <div class="ol-item">
        <div class="ol-dot" style="background:var(--green)"></div>
        Completati &nbsp;<strong>{len(completed)}</strong>&nbsp;<span>({rate})</span>
      </div>
      <div class="ol-item">
        <div class="ol-dot" style="background:var(--yellow)"></div>
        Incompleti &nbsp;<strong>{incomplete_runs}</strong>&nbsp;<span>({pct(incomplete_runs,total)})</span>
      </div>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-title">Attività per ora</div>
  <div class="chart-wrap">{bar_html}</div>
  <div class="chart-legend">
    <div class="cl-item"><div class="cl-dot" style="background:#2d3248"></div>NONE</div>
    <div class="cl-item"><div class="cl-dot" style="background:#2ecc71"></div>Completato</div>
    <div class="cl-item"><div class="cl-dot" style="background:#f0a500"></div>Incompleto</div>
  </div>
</div>

<div class="section">
  <div class="section-title">Studi completati ({len(completed)})</div>
  <table>
    <thead><tr>
      <th>Inizio</th><th>Esito</th><th>Durata</th><th>Steps</th><th>Codice / risposta</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>

<div class="footer">
  <span>Aggiornamento automatico ogni 60s</span>
  <a href="/">← torna al pannello</a>
</div>

<script>
function stopAgent() {{
  if (!confirm('Fermare l\\'agente?')) return;
  var btn = document.querySelector('.stop-btn');
  if (btn) {{ btn.disabled = true; btn.textContent = '⏳ fermando…'; }}
  fetch('/agent/stop', {{method:'POST'}})
    .then(function(r) {{ return r.json(); }})
    .then(function() {{ setTimeout(function(){{ location.reload(); }}, 1500); }})
    .catch(function(e) {{ alert('Errore: ' + e); }});
}}
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def _agent_running(self) -> bool:
        with state_lock:
            return state["running"]

    # -- GET --------------------------------------------------------
    def do_GET(self):
        if self.path == "/":
            html = (HERE / "gui_index.html").read_bytes()
            self._send_bytes(html, "text/html; charset=utf-8")
        elif self.path.startswith("/screen.png"):
            try:
                docker_exec("scrot", "-o", "/data/_gui_live.png")
                data = (DATA_DIR / "_gui_live.png").read_bytes()
                self._send_bytes(data, "image/png")
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
        elif self.path.startswith("/agent/status"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            since = int((q.get("since") or ["0"])[0] or 0)
            self._send_json(_status_payload(since))
        elif self.path.startswith("/agent/events"):
            self._serve_events()
        elif self.path.startswith("/stream.mjpg"):
            self._serve_mjpeg()
        elif self.path == "/presets":
            self._send_json(load_presets())
        elif self.path == "/dashboard":
            self._send_bytes(_build_dashboard().encode(), "text/html; charset=utf-8")
        elif self.path == "/study_cache":
            self._send_json({"entries": study_cache.entries(),
                             "titles": study_cache.titles()})
        else:
            self.send_response(404)
            self.end_headers()


    # -- streaming endpoints ----------------------------------------
    def _serve_events(self):
        """Server-Sent Events: replays what the client missed, then pushes
        every new event/state/live message the moment it happens."""
        global _ticker_started
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        since = int((qs.get("since") or ["0"])[0] or 0)
        run = int((qs.get("run") or ["-1"])[0] or -1)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        if not _ticker_started:
            _ticker_started = True
            threading.Thread(target=_url_ticker, daemon=True, name="url-ticker").start()

        def send(kind, data):
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        q = hub.subscribe()          # subscribe first: nothing can fall in the gap
        try:
            snap = _status_payload(since if run == state.get("run_id", 0) else 0)
            events = snap.pop("events")
            send("state", snap)
            for e in events:
                send("ev", e)
            if snap.get("phase"):
                send("live", snap["phase"])
            while True:
                try:
                    kind, data = q.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if q.overflowed:
                    break
                send(kind, data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(q)

    def _serve_mjpeg(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=fara")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b"--fara\r\nContent-Type: image/jpeg\r\n\r\n")
        streamer.add_client()
        last = 0
        try:
            while True:
                seq, data = streamer.wait_frame(last, 1.0)
                if data is None:
                    # Static screen: nothing new to show. Notice a viewer that
                    # went away within a second (a peek that returns b"" = FIN).
                    if select.select([self.connection], [], [], 0)[0]:
                        try:
                            if not self.connection.recv(1, socket.MSG_PEEK):
                                break
                        except OSError:
                            break
                    continue
                last = seq
                # Each frame is followed at once by the boundary AND the complete
                # header block of the next part (no Content-Length, the boundary
                # delimits). Measured in Chrome: it only shows a part once it has
                # parsed the next part's headers, so without this every frame
                # appeared one frame late, and on a static screen not at all.
                self.wfile.write(data + b"\r\n--fara\r\nContent-Type: image/jpeg\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            streamer.remove_client()

    # -- POST -------------------------------------------------------
    def do_POST(self):
        try:
            if self.path == "/click":
                if self._agent_running():
                    return self._send_json({"error": "agent is running"}, status=409)
                body = self._read_json()
                x, y = round(body["x"]), round(body["y"])
                docker_exec("xdotool", "mousemove", str(x), str(y), "click", "1")
                return self._send_json({"ok": True})

            if self.path == "/type":
                if self._agent_running():
                    return self._send_json({"error": "agent is running"}, status=409)
                body = self._read_json()
                text = str(body.get("text", ""))
                docker_exec("xdotool", "type", "--clearmodifiers", text)
                return self._send_json({"ok": True})

            if self.path == "/key":
                if self._agent_running():
                    return self._send_json({"error": "agent is running"}, status=409)
                body = self._read_json()
                key = str(body.get("key", ""))
                mapped = KEY_MAP.get(key, key)
                docker_exec("xdotool", "key", mapped)
                return self._send_json({"ok": True})

            if self.path == "/navigate":
                if self._agent_running():
                    return self._send_json({"error": "agent is running"}, status=409)
                body = self._read_json()
                url = str(body.get("url", ""))
                pw_bridge.goto(url)
                return self._send_json({"ok": True})

            if self.path == "/start_agent":
                body = self._read_json()
                task = str(body.get("task", ""))
                url = str(body.get("url", ""))
                max_rounds = int(body.get("max_rounds", 20))
                loop = bool(body.get("loop", False))
                poll_interval = float(body.get("poll_interval", 60))
                batch_fill = bool(body.get("batch_fill", False))
                wander_seconds = max(0.0, float(body.get("wander_seconds", 90)))
                skip_av = bool(body.get("skip_av", False))
                with state_lock:
                    if state["running"]:
                        return self._send_json({"error": "agent already running"}, status=409)
                    cancel.reset()
                    state["run_id"] = state.get("run_id", 0) + 1
                    state["seq"] = 0
                    state["running"] = True
                    state["stopping"] = False
                    state["started_at"] = time.time()
                    state["loop"] = loop
                    state["stats"] = {"cycle": 0, "steps": 0, "batch_fills": 0, "errors": 0}
                    state["events"] = []
                    state["pending_question"] = None
                    state["inject_message"] = None
                    state["stop_event"] = threading.Event()
                    state["answer_queue"] = queue.Queue()
                    state["phase"] = None
                    state["skip_av"] = skip_av
                _publish_state()
                threading.Thread(
                    target=agent_driver,
                    args=(task, url, max_rounds, loop, poll_interval, batch_fill, wander_seconds,
                          skip_av), daemon=True,
                ).start()
                return self._send_json({"ok": True})

            if self.path == "/agent/answer":
                body = self._read_json()
                text = str(body.get("text", ""))
                with state_lock:
                    if state["pending_question"] is None:
                        return self._send_json({"error": "no pending question"}, status=400)
                    aq = state["answer_queue"]
                aq.put(text)
                _push_event({"type": "answer", "text": text})
                return self._send_json({"ok": True})

            if self.path == "/agent/inject":
                body = self._read_json()
                text = str(body.get("text", "")).strip()
                if not text:
                    return self._send_json({"error": "empty message"}, status=400)
                with state_lock:
                    if not state["running"]:
                        return self._send_json({"error": "agent is not running"}, status=409)
                    state["inject_message"] = text
                return self._send_json({"ok": True})

            if self.path == "/agent/speed":
                body = self._read_json()
                mode = str(body.get("mode", "")).strip()
                if not agent_core.set_speed(mode):
                    return self._send_json(
                        {"error": f"unknown speed mode: {mode!r}",
                         "modes": list(agent_core.SPEED_MODES)}, status=400)
                return self._send_json({"ok": True, "speed": agent_core.speed_state()})

            if self.path == "/study_cache/clear":
                n = study_cache.clear()
                _publish_state()
                return self._send_json({"ok": True, "cleared": n})

            if self.path == "/study_cache/forget":
                body = self._read_json()
                key = str(body.get("key", ""))
                return self._send_json({"ok": study_cache.forget(key)})

            if self.path == "/agent/stop":
                with state_lock:
                    se, aq = state["stop_event"], state["answer_queue"]
                    if state["running"]:
                        state["stopping"] = True
                if se is not None:
                    se.set()
                if aq is not None:
                    aq.put(None)
                cancel.cancel_all()   # cut the in-flight model request / wait right now
                _publish_state()
                return self._send_json({"ok": True})

            if self.path == "/presets":
                body = self._read_json()
                name = str(body.get("name", "")).strip()
                if not name:
                    return self._send_json({"error": "preset needs a name"}, status=400)
                preset = {
                    "name": name,
                    "context": str(body.get("context", "")),
                    "task": str(body.get("task", "")),
                    "url": str(body.get("url", "")),
                    "max_rounds": int(body.get("max_rounds", 60)),
                    "loop": bool(body.get("loop", False)),
                    "poll_interval": float(body.get("poll_interval", 60)),
                    "batch_fill": bool(body.get("batch_fill", False)),
                    "wander_seconds": max(0.0, float(body.get("wander_seconds", 90))),
                }
                save_preset(preset)
                return self._send_json({"ok": True})

            if self.path == "/presets/delete":
                body = self._read_json()
                delete_preset(str(body.get("name", "")))
                return self._send_json({"ok": True})

            self.send_response(404)
            self.end_headers()
        except subprocess.CalledProcessError as e:
            self._send_json({"error": e.stderr or str(e)}, status=500)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def log_message(self, fmt, *args):
        # Default logging only prints method+path+status, never bodies --
        # but keep it quiet regardless since /type and /key bodies must
        # never be surfaced anywhere, including indirectly.
        pass


def _telegram_bot_loop():
    """Background thread: polls Telegram for commands."""
    import urllib.request as _ur
    if TEST_INSTANCE:
        return
    try:
        import telegram_config as _tg
    except ImportError:
        return  # no config, skip silently

    token   = _tg.TELEGRAM_TOKEN
    chat_id = str(_tg.TELEGRAM_CHAT_ID)
    base    = f"https://api.telegram.org/bot{token}"
    offset  = 0

    def tg_send(text: str):
        try:
            payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
            req = _ur.Request(f"{base}/sendMessage", data=payload,
                              headers={"Content-Type": "application/json"})
            _ur.urlopen(req, timeout=8)
        except Exception:
            pass

    def tg_send_photo(caption: str):
        """Take a live screenshot and send it as a Telegram photo."""
        try:
            import email.mime.multipart, email.mime.base, email.generator
            import io as _io
            docker_exec("scrot", "-o", "/data/_gui_live.png")
            img_bytes = (DATA_DIR / "_gui_live.png").read_bytes()
            boundary = b"----TGBoundary"
            body = (
                b"--" + boundary + b"\r\n"
                b'Content-Disposition: form-data; name="chat_id"\r\n\r\n' +
                chat_id.encode() + b"\r\n"
                b"--" + boundary + b"\r\n"
                b'Content-Disposition: form-data; name="caption"\r\n\r\n' +
                caption.encode() + b"\r\n"
                b"--" + boundary + b"\r\n"
                b'Content-Disposition: form-data; name="photo"; filename="screen.png"\r\n'
                b"Content-Type: image/png\r\n\r\n" +
                img_bytes + b"\r\n"
                b"--" + boundary + b"--\r\n"
            )
            req = _ur.Request(
                f"{base}/sendPhoto", data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"},
            )
            _ur.urlopen(req, timeout=15)
        except Exception:
            tg_send(caption)  # fall back to text-only if screenshot fails

    def tg_get(offset: int):
        try:
            url = f"{base}/getUpdates?timeout=20&offset={offset}&allowed_updates=[\"message\"]"
            with _ur.urlopen(url, timeout=30) as r:
                return json.load(r).get("result", [])
        except Exception:
            return []

    # Register bot commands in Telegram's menu
    try:
        cmds = json.dumps({"commands": [
            {"command": "start",   "description": "Avvia l'agente"},
            {"command": "stop",    "description": "Ferma l'agente"},
            {"command": "restart", "description": "Ferma, torna a studies, riavvia"},
            {"command": "status",  "description": "Stato + screenshot + studi recenti"},
            {"command": "inject",  "description": "Manda un'istruzione extra all'agente"},
            {"command": "normal",  "description": "Modalità normale (qualità massima)"},
            {"command": "hurry",   "description": "Modalità veloce (immagini ridotte)"},
            {"command": "superfast","description": "Modalità super-veloce (massima velocità)"},
            {"command": "help",    "description": "Lista comandi"},
        ]}).encode()
        req = _ur.Request(f"{base}/setMyCommands", data=cmds,
                          headers={"Content-Type": "application/json"})
        _ur.urlopen(req, timeout=8)
    except Exception:
        pass

    def _do_start() -> str:
        presets = load_presets()
        if not presets:
            return "❌ Nessun preset salvato. Avvia manualmente dalla dashboard."
        p = presets[0]
        with state_lock:
            if state["running"]:
                return "ℹ️ Agente già in esecuzione."
            cancel.reset()
            state["run_id"]    = state.get("run_id", 0) + 1
            state["seq"]       = 0
            state["running"]   = True
            state["stopping"]  = False
            state["started_at"] = time.time()
            state["loop"]      = bool(p.get("loop", False))
            state["stats"]     = {"cycle": 0, "steps": 0, "batch_fills": 0, "errors": 0}
            state["events"]    = []
            state["pending_question"] = None
            state["inject_message"]   = None
            state["stop_event"]  = threading.Event()
            state["answer_queue"] = queue.Queue()
            state["phase"]     = None
            state["skip_av"]   = bool(p.get("skip_av", False))
        _publish_state()
        threading.Thread(
            target=agent_driver,
            args=(
                str(p.get("task", "")),
                "",   # no forced navigation — start from wherever the browser is
                int(p.get("max_rounds", 20)),
                bool(p.get("loop", False)),
                float(p.get("poll_interval", 60)),
                bool(p.get("batch_fill", False)),
                max(0.0, float(p.get("wander_seconds", 90))),
                bool(p.get("skip_av", False)),
            ),
            daemon=True,
        ).start()
        return f"▶️ Agente avviato — preset: {p.get('name', '?')}"

    def _do_restart() -> str:
        with state_lock:
            was_running = state["running"]
        if was_running:
            cancel.cancel_all()
            for _ in range(50):
                time.sleep(0.1)
                with state_lock:
                    if not state["running"]:
                        break
        try:
            pw_bridge.reset_to("https://app.prolific.com/studies")
            pw_bridge.wait_for_content()
        except Exception:
            pass
        return _do_start()

    def _do_status() -> str:
        with state_lock:
            running   = state["running"]
            phase     = state.get("phase") or None
            stats     = dict(state.get("stats") or {})
            started   = state.get("started_at")
            pending_q = state.get("pending_question")
            loop      = state.get("loop", False)
            stimer    = state.get("study_timer")

        lines = []
        lines.append("🟢 In esecuzione" if running else "⚪ Fermo")

        if running and started:
            elapsed = int(time.time() - started)
            h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
            lines.append(f"⏱ Attivo da {h:02d}:{m:02d}:{s:02d}")

        if stats:
            cycle  = stats.get("cycle", 0)
            steps  = stats.get("steps", 0)
            fills  = stats.get("batch_fills", 0)
            errors = stats.get("errors", 0)
            lines.append(f"🔄 Ciclo {cycle} | {steps} step | {fills} form | {errors} err")

        if phase:
            phase_str = phase.get("phase", str(phase)) if isinstance(phase, dict) else str(phase)
            phase_step = phase.get("step", "") if isinstance(phase, dict) else ""
            lines.append(f"📍 Fase: {phase_str}" + (f" (step {phase_step})" if phase_step else ""))

        if pending_q:
            lines.append(f"❓ In attesa risposta: {pending_q[:80]}")

        try:
            current_url = get_current_url()
            from urllib.parse import urlparse as _up
            host = _up(current_url).netloc or current_url[:40]
            lines.append(f"🌐 {host}")
        except Exception:
            pass

        if stimer and stimer.get("min_seconds", 0) > 0:
            el = int(stimer.get("elapsed", 0))
            rem = int(stimer.get("remaining", 0))
            tot = int(stimer.get("min_seconds", 0))
            el_m, el_s = el // 60, el % 60
            rem_m, rem_s = rem // 60, rem % 60
            tot_m = tot // 60
            if rem > 0:
                lines.append(f"⏳ Studio: {el_m}:{el_s:02d} / {tot_m} min (mancano {rem_m}:{rem_s:02d})")
            else:
                lines.append(f"✅ Studio: {el_m}:{el_s:02d} / {tot_m} min (tempo ok)")

        if loop:
            lines.append("🔁 Modalità loop attiva")
        if agent_core._speed_mode == "super_fast":
            lines.append("🚀 Modalità SUPER-FAST" + (" (automatica)" if agent_core._speed_auto else ""))
        elif agent_core._speed_mode == "hurry":
            lines.append("⚡ Modalità hurry" + (" (automatica)" if agent_core._speed_auto else ""))
        try:
            n_skipped = len(study_cache.entries())
            if n_skipped:
                lines.append(f"⏭ Studi scartati in memoria: {n_skipped}")
        except Exception:
            pass

        # Submissions from Prolific (opens a tab briefly when agent is idle)
        try:
            subs = _agent_submissions()
            if subs:
                from datetime import datetime, timedelta
                cutoff_24h = datetime.now() - timedelta(hours=24)
                STATUS_EMOJI = {
                    "approved": "✅", "awaiting review": "⏳",
                    "returned": "↩️", "rejected": "❌", "timed out": "⌛",
                    "active": "🔵", "screened out": "🚫",
                }
                COUNTS_AS_EARNED = {"approved"}

                total_24h = 0.0
                currency_sym = "£"
                lines.append("")
                lines.append("📋 Studi recenti:")
                for s in subs[:8]:
                    emoji = STATUS_EMOJI.get(s["status"].lower(), "❔")
                    date_disp = f" ({s['date']})" if s.get("date") else ""
                    rwd = s.get("reward", "")
                    cur = s.get("currency", "£")
                    reward_disp = f" {cur}{rwd}" if rwd else ""
                    lines.append(f"{emoji} {s['title']}{date_disp}{reward_disp}")

                    # Sum earnings for last 24h (approved/awaiting only)
                    if rwd and s["status"].lower() in COUNTS_AS_EARNED:
                        date_full = s.get("date_full", "")
                        try:
                            try:
                                d = datetime.strptime(date_full, "%d %b %Y, %H:%M")
                            except ValueError:
                                d = datetime.strptime(date_full, "%d %b %Y")
                            if d >= cutoff_24h:
                                total_24h += float(rwd)
                                currency_sym = cur
                        except Exception:
                            pass

                lines.append(f"💰 Guadagni ultime 24h: {currency_sym}{total_24h:.2f}")
            else:
                if _submissions_cache["ts"] == 0.0:
                    lines.append("📋 Studi: caricamento al termine del ciclo...")
                else:
                    lines.append("📋 Nessuna submission trovata")
        except Exception:
            pass

        return "\n".join(lines)

    print("Telegram bot polling started")
    while True:
        updates = tg_get(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message", {})
            # only accept messages from the authorized chat
            if str(msg.get("chat", {}).get("id", "")) != chat_id:
                continue
            raw = (msg.get("text") or "").strip()
            if not raw:
                continue      # photo, sticker, service message: nothing to parse
            parts = raw.split(maxsplit=1)
            text = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            if text in ("/stop", "stop", "/ferma", "ferma"):
                with state_lock:
                    running = state["running"]
                if running:
                    cancel.cancel_all()
                    tg_send("⏹ Agente fermato.")
                else:
                    tg_send("ℹ️ Agente già fermo.")
            elif text in ("/start", "start", "/avvia", "avvia"):
                tg_send(_do_start())
            elif text in ("/status", "status", "/stato", "stato"):
                tg_send_photo(_do_status())
            elif text in ("/restart", "restart", "/riavvia", "riavvia"):
                tg_send("🔄 Riavvio in corso...")
                tg_send(_do_restart())
            elif text in ("/inject", "inject", "/istruzione", "istruzione", "/msg", "msg"):
                if not arg:
                    tg_send("Scrivi anche il testo:\n/inject <istruzione per l'agente>")
                else:
                    with state_lock:
                        running = state["running"]
                        if running:
                            state["inject_message"] = arg
                    tg_send(f"📨 Istruzione in coda, la riceve al prossimo passo:\n{arg}"
                            if running else "ℹ️ L'agente non è in esecuzione.")
            elif text in ("/normal", "normal", "/normale", "normale"):
                agent_core.set_speed("normal")
                tg_send("🐢 Modalità normale attiva")
            elif text in ("/hurry", "hurry", "/veloce", "veloce"):
                agent_core.set_speed("hurry")
                tg_send("⚡ Modalità hurry attiva")
            elif text in ("/superfast", "superfast", "/super-fast", "super-fast",
                          "/velocissimo", "velocissimo"):
                agent_core.set_speed("super_fast")
                tg_send("🚀 Modalità SUPER-FAST attiva — qualità ridotta, massima velocità")
            elif text in ("/help", "help", "/aiuto", "aiuto"):
                tg_send(
                    "🤖 Comandi disponibili:\n"
                    "/start — Avvia l'agente\n"
                    "/stop — Ferma l'agente\n"
                    "/restart — Ferma, torna a studies, riavvia\n"
                    "/status — Stato + screenshot + studi recenti\n"
                    "/inject <testo> — Istruzione extra per la run in corso\n"
                    "\n⚙️ Velocità:\n"
                    "/normal — Qualità massima (default)\n"
                    "/hurry — Veloce (immagini 75%, token ridotti)\n"
                    "/superfast — Super-veloce (img 50%, JPEG, contesto ridotto)\n"
                    "\n/help — Questa lista"
                )


def _submissions_auto_updater():
    """Background thread: refreshes the submissions cache right after each
    agent cycle ends (triggered by the 'done' event) or on a 5-minute
    fallback timer. Only runs the tab fetch during the inter-cycle idle
    window (phase=None) so the agent is never interrupted mid-cycle."""
    if TEST_INSTANCE:
        return
    time.sleep(20)  # let the browser settle after server start
    _submissions_trigger.set()   # fetch once on startup if agent is idle
    while True:
        # Wait for a "done" signal or 5-minute timeout
        _submissions_trigger.wait(timeout=300)
        _submissions_trigger.clear()
        try:
            # Skip if cache is fresh (< 60s old)
            if time.time() - _submissions_cache["ts"] < 60:
                continue

            with state_lock:
                running = state["running"]
                phase   = state.get("phase")

            # Safe: agent stopped, OR between cycles (phase=None after "done")
            if not running or (running and phase is None):
                with _submissions_lock:
                    if time.time() - _submissions_cache["ts"] < 60:
                        continue
                    data = _fetch_submissions()
                    if data:  # keep stale cache on transient errors
                        _submissions_cache["data"] = data
                        _submissions_cache["ts"] = time.time()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    threading.Thread(target=_telegram_bot_loop, daemon=True, name="tg-bot").start()
    threading.Thread(target=_submissions_auto_updater, daemon=True, name="subs-updater").start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"GUI on http://127.0.0.1:{args.port} (loopback only)")
    server.serve_forever()


if __name__ == "__main__":
    main()
