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
import os
import signal
import json
import queue
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cancel
import pw_bridge
import agent_core
from agent_core import run_agent, docker_exec, get_current_url, KEY_MAP, DATA_DIR, CONTAINER

HERE = Path(__file__).parent
MAX_EVENTS = 500
PRESETS_FILE = Path(os.environ.get("FARA_PRESETS", HERE / "presets.json"))
presets_lock = threading.Lock()


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
        }
        if with_events:
            payload["events"] = [e for e in state["events"] if e.get("seq", 0) > since]
    payload["url"] = _cached_url() if fresh_url else _url_cache["v"]
    return payload


def _publish_state():
    hub.publish("state", _status_payload(with_events=False, fresh_url=False))


def _on_live(msg: dict):
    """agent_core.live_hook: forward what the agent is doing right now."""
    msg = dict(msg, ts=time.time())
    if msg.get("type") == "phase":
        with state_lock:
            state["phase"] = msg
    hub.publish("live", msg)


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
    hub.publish("ev", event)
    _publish_state()


def _pop_injected():
    with state_lock:
        text = state["inject_message"]
        state["inject_message"] = None
        return text


def run_one_cycle(task: str, url: str, max_rounds: int, stop_event, answer_queue,
                   messages: list = None, batch_fill: bool = False, wander_seconds: float = 0):
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
        return _drive_cycle(task, url, max_rounds, stop_event, answer_queue, messages, batch_fill, wander_seconds)
    except cancel.Cancelled:
        # Stop pressed while the agent was blocked on a model call or a
        # wait: cancel.cancel_all() cut the request, the generator is gone.
        _push_event({"type": "done", "answer": "STOPPED"})
        return False, messages


def _drive_cycle(task, url, max_rounds, stop_event, answer_queue, messages, batch_fill, wander_seconds=0):
    gen = run_agent(task, url, max_rounds, stop_event=stop_event, messages=messages,
                     batch_fill=batch_fill, wander_seconds=wander_seconds)
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


def agent_driver(task: str, url: str, max_rounds: int, loop: bool, poll_interval: float,
                  batch_fill: bool = False, wander_seconds: float = 0):
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
            ok, _ = run_one_cycle(task, url if cycle == 1 else "", max_rounds,
                                  stop_event, answer_queue,
                                  messages=None, batch_fill=batch_fill,
                                  wander_seconds=wander_seconds)
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
    finally:
        if inhibitor is not None:
            inhibitor.terminate()
        with state_lock:
            state["running"] = False
            state["stopping"] = False
            state["phase"] = None
        hub.publish("live", {"type": "phase", "phase": "idle", "ts": time.time()})
        _publish_state()


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
                _publish_state()
                threading.Thread(
                    target=agent_driver,
                    args=(task, url, max_rounds, loop, poll_interval, batch_fill, wander_seconds), daemon=True,
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"GUI on http://127.0.0.1:{args.port} (loopback only)")
    server.serve_forever()


if __name__ == "__main__":
    main()
