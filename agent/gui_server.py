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
import random
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
import pusher_listener
import local_agent
import sites
from agent_core import run_agent, docker_exec, get_current_url, KEY_MAP, DATA_DIR, CONTAINER

HERE = Path(__file__).parent
MAX_EVENTS = 500

# A second server (the isolated test stack on :8767, or a smoke test) must not
# run the side threads: both talk to things the live instance owns -- the same
# Telegram bot token, where two getUpdates pollers steal each other's
# messages, and the browser, where the submissions updater opens a tab.
TEST_INSTANCE = os.environ.get("FARA_TEST_INSTANCE", "") not in ("", "0")
PRESETS_FILE = Path(os.environ.get("FARA_PRESETS", HERE / "presets.json"))

# ── Model registry (swap-on-demand) ──────────────────────────────
MODELS = {
    "fara": {
        "container": "fara-test",
        "label": "Web Agent",
        "multimodal": True,
    },
    "qwen": {
        "container": "qwen-model",
        "label": "Local Agent",
        "multimodal": False,
    },
}
_active_model: str = ""
_model_lock = threading.Lock()


def _detect_active_model() -> str:
    """Check which model container is running right now."""
    for key, cfg in MODELS.items():
        try:
            out = subprocess.check_output(
                ["docker", "inspect", "-f", "{{.State.Running}}", cfg["container"]],
                stderr=subprocess.DEVNULL, text=True).strip()
            if out == "true":
                return key
        except Exception:
            continue
    return ""


def _model_healthy(key: str) -> bool:
    """Check if the model's llama-server /health endpoint responds OK."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8090/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def switch_model(target: str) -> dict:
    """Stop the current model container and start the target one."""
    global _active_model
    if target not in MODELS:
        return {"error": f"unknown model: {target}", "available": list(MODELS.keys())}

    with _model_lock:
        current = _active_model or _detect_active_model()
        if current == target:
            healthy = _model_healthy(target)
            return {"ok": True, "model": target, "already": True, "healthy": healthy}

        # Stop current
        if current and current in MODELS:
            subprocess.run(["docker", "stop", MODELS[current]["container"]],
                           capture_output=True, timeout=30)

        # Start target
        try:
            subprocess.run(["docker", "start", MODELS[target]["container"]],
                           capture_output=True, timeout=30, check=True)
        except subprocess.CalledProcessError as e:
            return {"error": f"failed to start {target}: {e.stderr or str(e)}"}

        _active_model = target

        # Wait for health (model loading takes time)
        import urllib.request
        for i in range(120):
            time.sleep(1)
            try:
                with urllib.request.urlopen("http://127.0.0.1:8090/health", timeout=2) as r:
                    if r.status == 200:
                        return {"ok": True, "model": target, "healthy": True,
                                "load_time_s": i + 1}
            except Exception:
                pass
        return {"ok": True, "model": target, "healthy": False,
                "warning": "model started but not healthy after 120s"}
presets_lock = threading.Lock()

# ── Local Agent state ────────────────────────────────────────────────
# Multiple named chats persisted on disk. _local_sessions maps a chat id to
# its message list; _local_active names the one currently receiving messages.
LOCAL_CHATS_FILE = Path(os.environ.get("FARA_LOCAL_CHATS",
                                       HERE / "local_chats.json"))
_local_sessions: dict = {}
_local_active: str = ""
_local_busy = False
_local_lock = threading.Lock()


def _local_load():
    """Load persisted chats + active id into module state. Caller holds lock."""
    global _local_sessions, _local_active
    try:
        data = json.loads(LOCAL_CHATS_FILE.read_text())
        _local_sessions = {k: list(v) for k, v in (data.get("sessions") or {}).items()}
        _local_active = str(data.get("active") or "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _local_sessions = {}
        _local_active = ""
    if not _local_sessions:
        cid = f"chat-{int(time.time())}"
        _local_sessions[cid] = []
        _local_active = cid
    elif _local_active not in _local_sessions:
        _local_active = next(iter(_local_sessions))


def _local_save():
    """Persist chats. Caller holds lock."""
    try:
        LOCAL_CHATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LOCAL_CHATS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"sessions": _local_sessions,
                                   "active": _local_active},
                                  ensure_ascii=False, indent=2))
        os.replace(tmp, LOCAL_CHATS_FILE)
    except Exception:
        pass


def _local_title(messages: list) -> str:
    """Derive a short title from the first user message, or empty."""
    for m in messages:
        if m.get("role") == "user":
            t = (m.get("content") or "").strip().splitlines()[0]
            return (t[:40] + "…") if len(t) > 40 else t
    return ""


def _local_current() -> list:
    """Return the active chat's message list. Caller holds lock."""
    if _local_active not in _local_sessions:
        _local_load()
    return _local_sessions[_local_active]


_local_load()

# "data": every submission known, newest first (see the local store below).
# "header": the platform's own SUMMARY figures, which are the account's real
# lifetime totals. "complete": whether the store matches that SUMMARY yet.
_submissions_cache: dict = {"data": [], "ts": 0.0, "header": {}, "complete": False}
_submissions_lock = threading.Lock()
_submissions_trigger = threading.Event()   # set on each "done" event


def _last_run_flag() -> str:
    """The "your submission failed our checks" wording from the run that just
    ended, or "". Read from the newest summary.json, which run_agent writes
    before the cycle returns, so the per-cycle ping can carry the verdict:
    the study still gave a completion code, so without this line the ping
    says nothing but a cheerful tick.
    """
    try:
        tfs = _transcript_files()
        if not tfs:
            return ""
        s = json.loads((tfs[-1].parent / "summary.json").read_text())
        return (s.get("flagged") or "")[:200]
    except Exception:
        return ""


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
    """Append a debug line to submissions_api_debug.txt.

    It used to overwrite the file, so only the very last line survived and a
    multi-page crawl left no trace of which page went wrong. Capped at 200 KB.
    """
    try:
        from agent_core import LOG_ROOT
        Path(LOG_ROOT).mkdir(parents=True, exist_ok=True)
        f = Path(LOG_ROOT) / "submissions_api_debug.txt"
        if f.exists() and f.stat().st_size > 200_000:
            f.write_text(f.read_text(errors="replace")[-50_000:])
        with f.open("a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {str(msg)[:4000]}\n")
    except Exception:
        pass

# -- the account's submission history ----------------------------------------
# What a page of the history looks like (its rows, badges, SUMMARY and pager)
# belongs to the site profile; the crawl and the local store below are generic.
# A profile without a history (history_url "") switches all of it off.

def _parse_submission_rows(raw_rows: list[str]) -> list[dict]:
    return sites.current.parse_history_rows(raw_rows)


def _parse_submission_header(rows: list[str]) -> dict:
    return sites.current.parse_history_header(rows)


def _parse_last_page(rows: list[str]) -> int:
    return sites.current.parse_history_last_page(rows)


def _money(line: str):
    return sites.current.money(line)


def _fetch_submissions(page: int = 1) -> dict:
    """Open a temporary tab, read one page of the submissions list, close it.

    Returns {"rows", "header", "last_page"}; "rows" is empty when the page
    could not be read, and the caller keeps whatever it already had.

    Runs only during the inter-cycle idle window (see
    _submissions_auto_updater), so the agent never notices the pause. Saves
    and restores the active tab atomically inside the bridge worker thread.
    """
    base = sites.current.history_url
    url = base if page <= 1 else f"{base}?page={page}"
    JS = """() => (document.body.innerText || '').split('\\n')
                  .map(s => s.trim()).filter(s => s.length > 0)"""
    # The list is rendered client-side, so wait for it to be there (the
    # profile's history_ready_js) instead of for a fixed five seconds.
    READY = sites.current.history_ready_js

    def _do(b):
        prev     = b._active
        new_page = prev.context.new_page()
        b._known.append(new_page)
        b._active = new_page
        try:
            new_page.goto(url, wait_until="domcontentloaded", timeout=25000)
            try:
                if not READY:
                    raise ValueError("no readiness test")
                new_page.wait_for_function(READY, timeout=12000)
            except Exception:
                new_page.wait_for_timeout(4000)   # read whatever is there
            new_page.wait_for_timeout(700)        # let the last rows paint
            return new_page.evaluate(JS) or []
        finally:
            b._active = prev
            b._known  = [p for p in b._known if p is not new_page]
            try:
                new_page.close()
            except Exception:
                pass

    try:
        raw_rows = pw_bridge.run(_do, timeout=60)
    except Exception as e:
        _dbg(f"tab fetch error (page {page}): {e}")
        return {"rows": [], "header": {}, "last_page": 1}

    rows = _parse_submission_rows(raw_rows)
    header = _parse_submission_header(raw_rows)
    last_page = _parse_last_page(raw_rows)
    # Every page of the list shows one status badge per submission, so that
    # count is what the page holds and len(rows) is what we got out of it.
    # The store settling 9 short of the SUMMARY, with every page yielding its
    # 20 rows and no key ever clashing, has to be one of these two: a row the
    # parser drops (no title found within the backward window, so nothing is
    # emitted) or two rows of one page landing on a single key. Both are
    # invisible unless measured here, so measure both and keep the page.
    badges = sum(1 for r in raw_rows if sites.current.history_status_re.match(r))
    keys = [_subs_key(r) for r in rows]
    dupes = len(keys) - len(set(keys))
    _dbg(f"page {page}/{last_page}: {len(raw_rows)} lines, {badges} badges, "
         f"{len(rows)} rows" + (f", {dupes} duplicate keys" if dupes else ""))
    if dupes or len(rows) != badges or (badges < sites.current.history_per_page and page < last_page):
        try:
            (Path(agent_core.LOG_ROOT) / f"submissions_odd_p{page}.txt"
             ).write_text("\n".join(raw_rows))
            _dbg(f"page {page} does not add up ({badges} badges, {len(rows)} rows, "
                 f"{dupes} duplicate keys): lines kept")
        except Exception:
            pass
    return {"rows": rows, "header": header, "last_page": last_page}


# ------------------------------------------------------------ the local store
# Reading only the first page meant /earnings and /status spoke for 20 of 816
# submissions. Every page read is merged into one file instead, so a crawl can
# stop whenever the agent needs the browser back and resume later, and so a
# row whose status changes weeks after submission (awaiting review -> approved)
# is updated in place rather than counted twice.
SUBS_STORE = Path(agent_core.LOG_ROOT) / "submissions.json"
_SUBS_STORE_V = 1
# How long to leave the history alone after a sweep that ended without
# matching the platform's SUMMARY. Six hours: long enough that an unmatchable
# count cannot turn into a permanent crawl, short enough that a status that
# flips deep in the history is picked up the same day.
_SUBS_SWEEP_COOLDOWN = 6 * 3600


def _subs_key(row: dict) -> str:
    """A submission's identity. The timestamp is to the minute and the title is
    the study's, so the pair is unique and, unlike a row's position, survives
    newer submissions pushing it onto a later page."""
    return f"{row.get('date_full', '')}|{row.get('title', '')}"


def _subs_ts(row: dict) -> float:
    """The submission's own date as an epoch, or 0.0 when unparseable."""
    from datetime import datetime as _dt
    for fmt in ("%d %b %Y, %H:%M", "%d %b %Y"):
        try:
            return _dt.strptime(row.get("date_full", ""), fmt).timestamp()
        except ValueError:
            pass
    return 0.0


def _subs_store_load() -> dict:
    try:
        d = json.loads(SUBS_STORE.read_text())
        if d.get("v") == _SUBS_STORE_V and isinstance(d.get("rows"), dict):
            # Drop what an older parser could not read. The signature is a
            # title holding an amount, which is what the bonus line used to
            # become. NOT the absence of a date: the oldest submissions
            # really are listed without one --
            #     Runners wanted: help improve a running app
            #     By maze.design
            #     Reward: $4,33
            #     RETURNED
            # -- and requiring a date threw away 8 real rows, which left the
            # store permanently 8 short of the SUMMARY and re-swept all 41
            # pages every six hours looking for them.
            d["rows"] = {k: r for k, r in d["rows"].items()
                         if (r.get("title") or " ")[:1] not in "£$€"}
            return d
    except Exception:
        pass
    return {"v": _SUBS_STORE_V, "rows": {}, "header": {}, "cursor": 2, "ts": 0.0}


def _subs_store_save(store: dict) -> None:
    try:
        SUBS_STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SUBS_STORE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store, separators=(",", ":")))
        tmp.replace(SUBS_STORE)
    except Exception as e:
        _dbg(f"store save failed: {e}")


def _subs_merge(store: dict, rows: list) -> int:
    """Add or update rows in the store; returns how many actually changed.

    Two rows of the same page landing on one key would be a submission lost
    without a trace, and the store then never reaches the SUMMARY's count: a
    full 41-page read lands 807 of 816 while every single page yields its 20
    rows. Whenever a key arrives over a row that differs in more than its
    status, say so -- the line shows whether the platform listed two payments of
    one study separately (so the key needs a third part) or simply added an
    adjustment to a row already read (so it does not).
    """
    changed = 0
    seen: dict = {}
    for r in rows:
        k = _subs_key(r)
        # One page can genuinely show the same study twice at the same minute
        # for the same money: "AI Videos - Evaluation", 13 Nov 2025 18:44,
        # $5.00, once approved and once returned. Date and title alone are
        # then not an identity, and the second row used to overwrite the
        # first. Numbering the repeats within the page keeps both, and keeps
        # doing so on every later read, because they always arrive together
        # and in the same order.
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > 1:
            k = f"{k}#{seen[k]}"
        old = store["rows"].get(k)
        if old is not None and (old.get("reward") != r.get("reward")
                                or old.get("currency") != r.get("currency")):
            _dbg(f"key clash {k!r}: {old.get('currency')}{old.get('reward')}"
                 f" ({old.get('status')}) <- {r.get('currency')}{r.get('reward')}"
                 f" ({r.get('status')})")
        if old != r:
            store["rows"][k] = r
            changed += 1
    return changed


def _subs_rows(store: dict) -> list:
    """Every submission known, newest first."""
    return sorted(store["rows"].values(), key=_subs_ts, reverse=True)


def _subs_complete(store: dict) -> bool:
    """True when the store holds as much as the platform's own SUMMARY claims.

    The page states the account's totals, so they are the checksum that says
    whether the crawl still has pages to read -- no guessing from page counts,
    and a status that flipped deep in the history shows up as a shortfall in
    the approved count.
    """
    h = store.get("header") or {}
    if not h.get("total"):
        return False
    rows = store["rows"].values()
    paid = sum(1 for r in rows if (r.get("status") or "").lower() in sites.current.history_paid_statuses)
    return len(store["rows"]) >= h["total"] and paid >= h.get("approved", 0)


def _subs_agent_idle() -> bool:
    """Whether the browser can be borrowed: the agent is stopped, or between
    cycles. Checked again before every page of a crawl, because pw_bridge
    serialises calls and a long crawl would otherwise queue the next cycle
    behind it."""
    with state_lock:
        return not state["running"] or state.get("phase") is None


def _submissions_refresh(max_pages: int = 6) -> bool:
    """Read page 1, then as much of the history as is still missing.

    Page 1 every time (that is where today's work lands). After it, the deep
    sweep walks pages 2..last_page across refreshes, keeping its place in the
    store, and only while the store disagrees with the page's own SUMMARY.
    Once they agree it costs exactly one page load a refresh. Returns True if
    anything was read.
    """
    store = _subs_store_load()
    if not store["rows"]:
        # First fill: there is nothing to report from until the history is in,
        # so take a bigger bite. Still one page at a time, still giving the
        # browser back the moment the agent wants it.
        max_pages = max(max_pages, 20)
    first = _fetch_submissions(1)
    if not first["rows"]:
        return False
    _subs_merge(store, first["rows"])
    if first["header"]:
        store["header"] = first["header"]
    last_page = max(1, first["last_page"])

    pages_read = 1
    page = max(2, int(store.get("cursor") or 2))
    # A sweep in progress (cursor past page 2) always continues. A new one
    # starts only after the cooldown: if the SUMMARY can never be matched --
    # a row the platform no longer lists, a title we truncate differently -- the
    # crawl must not then borrow the browser on every single cycle forever.
    # Page numbers only mean something while the list stands still. Every new
    # submission pushes every older one down, so a row can cross a boundary
    # the sweep has already passed and never be read: that is how the store
    # settled 8 short of a list a single uninterrupted pass reads whole. If
    # the account's own total moved since this sweep began, the sweep starts
    # over rather than carrying on with stale page numbers.
    total_now = (store.get("header") or {}).get("total")
    if page > 2 and store.get("sweep_total") not in (None, total_now):
        _dbg(f"sweep restarted: the list grew from {store.get('sweep_total')} "
             f"to {total_now} while it was being read")
        page = 2
    sweeping = not _subs_complete(store) and (
        page > 2 or time.time() - (store.get("swept") or 0) > _SUBS_SWEEP_COOLDOWN)
    if sweeping and page == 2:
        store["sweep_total"] = total_now
    while (sweeping and pages_read < max_pages and not _subs_complete(store)
           and page <= last_page and _subs_agent_idle()):
        got = _fetch_submissions(page)
        pages_read += 1
        if got["rows"]:
            _subs_merge(store, got["rows"])
            if got["header"]:
                store["header"] = got["header"]
            last_page = max(last_page, got["last_page"])
        page += 1
        # Save after every page, not at the end of the crawl: a restart in the
        # middle of a 41-page sweep would otherwise throw away everything it
        # had read and start again from page 2.
        store["cursor"] = page if page <= last_page else 2
        _subs_store_save(store)
        if page > last_page:
            # The sweep reached the end. Newer submissions shift rows onto
            # later pages, so one pass can still miss one: park the cursor
            # back at the start and let the cooldown decide when to retry.
            page = 2
            store["swept"] = time.time()
            break
        time.sleep(random.uniform(0.8, 2.0))
    store["cursor"] = page
    store["ts"] = time.time()
    _subs_store_save(store)

    rows = _subs_rows(store)
    with _submissions_lock:
        _submissions_cache["data"] = rows
        _submissions_cache["header"] = store.get("header") or {}
        _submissions_cache["complete"] = _subs_complete(store)
        _submissions_cache["ts"] = time.time()
    _dbg(f"refresh: {pages_read} page(s), store {len(rows)}/"
         f"{(store.get('header') or {}).get('total', '?')}, cursor {page}")
    return True


def _agent_submissions() -> list[dict]:
    """Submissions from the last 24h, newest first, or the newest few when the
    day was quiet. Reads the in-memory store; never blocks and never fetches."""
    with _submissions_lock:
        all_subs = list(_submissions_cache["data"])

    # Submissions from the last 24h (what the agent did today), newest first;
    # the newest eight when the window is empty.
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
    return recent if recent else all_subs[:8]


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
    payload["model"] = agent_core.model_speed()
    payload["site"] = sites.current.name
    payload["sites"] = sites.names()
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
        return False, messages, "STOPPED"


def _drive_cycle(task, url, max_rounds, stop_event, answer_queue, messages, batch_fill,
                  wander_seconds=0, skip_av=False):
    gen = run_agent(task, url, max_rounds, stop_event=stop_event, messages=messages,
                     batch_fill=batch_fill, wander_seconds=wander_seconds, skip_av=skip_av)
    try:
        event = next(gen)
    except StopIteration:
        return True, messages, ""

    while True:
        latest_messages = event.get("messages", messages)
        # "messages" carries the full conversation incl. base64 screenshots --
        # fine to thread internally, but far too heavy to store/poll in the
        # GUI's event feed, so it's stripped before pushing.
        _push_event({k: v for k, v in event.items() if k != "messages"})
        if event["type"] == "done":
            return True, latest_messages, event.get("answer", "")
        if event["type"] == "question":
            # Unattended loop: never block on a human. Auto-steer the model to
            # proceed on its own and keep the 24/7 loop moving. A "this study
            # won't open, what should I do?" question would otherwise stall the
            # whole run until someone replied. The user still sees it (feed +
            # non-blocking Telegram) and can redirect with /inject anytime.
            if state.get("loop"):
                _push_event({"type": "auto_answer", "step": event.get("step"),
                             "question": event["question"],
                             "answer": "(unattended: proceeding autonomously)"})
                try:
                    agent_core._notify_telegram(
                        "ℹ️ L'agente avrebbe chiesto (procedo in autonomia, nessun blocco):\n"
                        + str(event["question"])[:400]
                        + "\n\nPer intervenire: /inject <testo>.")
                except Exception:
                    pass
                event = gen.send(sites.current.unattended_answer())
                continue
            with state_lock:
                state["pending_question"] = event["question"]
            _publish_state()
            # Ping Telegram so the user doesn't have to open the GUI to
            # realise the agent is blocked. Any plain-text reply (or
            # /inject <answer>) from Telegram unblocks it via answer_queue.
            try:
                agent_core._notify_telegram(
                    "❓ L'agente chiede:\n" + str(event["question"])[:500]
                    + "\n\nRispondi qui con un messaggio (o /inject <testo>).")
            except Exception:
                pass
            answer = answer_queue.get()
            with state_lock:
                state["pending_question"] = None
            _publish_state()
            if answer is None:
                gen.close()
                _push_event({"type": "done", "answer": "STOPPED"})
                return False, latest_messages, "STOPPED"
            event = gen.send(answer)
        else:
            injected = _pop_injected()
            if injected:
                _push_event({"type": "inject", "text": injected})
            try:
                event = gen.send(injected) if injected else next(gen)
            except StopIteration:
                return True, latest_messages, ""


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
    """The page each loop cycle returns to: always the platform studies list
    (see agent_core.loop_home_for). A study detail, submission/completion or
    external survey page is never a valid home -- returning there would make
    the loop re-open a finished study -- so only an actual studies list is
    kept as-is. Robust to the loop being started from any page."""
    return agent_core.loop_home_for(url)


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
             "--who=WebDolly agent", "--why=Agent run in progress", "--mode=block",
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

# Between cycles the list is watched from the DOM instead of with the model:
# on 13 Oct 196 cycles out of 211 found it empty, ~7 model calls each. The
# check keeps the cadence the loop already had (a 10 s pause plus a ~38 s
# empty cycle, about 45 s) -- it saves the GPU, it does not make the agent
# arrive any sooner. A full cycle still runs every FULL_CYCLE_EVERY seconds
# in case the DOM reading is wrong.
LIST_CHECK_MIN_SECONDS = 2
# Safety-net cycle. Only runs when Pusher is NOT delivering events: with a
# healthy sniffer connection every new study triggers us within ~1 s and a
# periodic blind cycle is pure GPU waste. When the listener disconnects we
# fall back to this interval so a silent push outage can't make the agent
# miss studies all night. 0 = never fire a blind cycle.
FULL_CYCLE_EVERY_PUSHER_DOWN = int(os.environ.get("FARA_SAFETY_CYCLE_S", "900"))
# Max consecutive INCOMPLETE cycles before giving up on a stuck survey and
# resetting back to the studies list. Each INCOMPLETE cycle burns max_rounds
# steps on the same survey -- with max_rounds=30 that's 30 steps per cycle,
# so 3 attempts = ~90 steps on one survey, enough for a difficult form but
# not enough to loop forever.
MAX_INCOMPLETE_STREAK = 3


def _backoff(consecutive_errors: int) -> int:
    """Seconds to wait before retrying, by how many cycles failed in a row."""
    i = max(1, int(consecutive_errors)) - 1
    return ERROR_BACKOFF[min(i, len(ERROR_BACKOFF) - 1)]


def _refresh_list(home_url: str):
    """Back to the starting page, freshly loaded. A plain reload of the
    CURRENT page wasn't enough: after a finished survey the current page is
    the "submission received" screen, so every later cycle reloaded that,
    saw "already done" and terminated -- the loop never got back to the
    studies list. Navigating also refreshes SPA dashboards."""
    try:
        if home_url:
            pw_bridge.reset_to(home_url)
        else:
            pw_bridge.reload()
        pw_bridge.wait_for_content()
    except Exception:
        pass


def _ensure_pusher(stop_event):
    """Start the Pusher CDP sniffer if not already running."""
    if TEST_INSTANCE:
        return
    pl = pusher_listener.get_listener()
    if pl.connected:
        return
    pl.start()


def _wait_for_studies(home_url: str, poll_interval: float, stop_event) -> bool:
    """Refresh and read the list from the DOM until there is something for the
    model to look at (or it is time for a safety cycle). True if Stop was
    pressed while waiting.

    When the Pusher listener is connected, react instantly to push events
    instead of sleeping the full poll_interval between DOM checks."""
    every = max(float(poll_interval or 0), LIST_CHECK_MIN_SECONDS)
    if not sites.current.home_url():
        # A site with no task list: there is no list to watch, so the next
        # cycle simply starts after the pause, from the start page again.
        if stop_event.wait(every):
            return True
        _refresh_list(home_url)
        return False
    since = time.time()
    checks = 0
    unknowns = 0
    pl = pusher_listener.get_listener()
    while True:
        if stop_event.is_set():
            return True
        # Wait for stop OR Pusher event OR poll timeout, whichever first.
        # Playwright dispatches WS frame callbacks only while its event loop
        # is pumped (during pw_bridge calls).  Interleave short pump bursts
        # with event checks so a Pusher push is detected within ~1 s.
        pushed = False
        if pl.connected:
            deadline = time.monotonic() + every
            while time.monotonic() < deadline:
                if stop_event.is_set():
                    return True
                try:
                    pw_bridge.run(
                        lambda b: b.active_page().wait_for_timeout(200),
                        timeout=3)
                except Exception:
                    pass
                if pl.wait(min(0.8, deadline - time.monotonic())):
                    pushed = True
                    break
        else:
            if stop_event.wait(every):
                return True
        if pushed:
            _push_event({"type": "notice",
                         "text": "📡 Pusher: evento ricevuto, controllo lista..."})
        _refresh_list(home_url)
        checks += 1
        try:
            seen = agent_core.studies_list_state()
        except Exception:
            seen = "unknown"
        if seen == "studies":
            agent_core.capture_page("studies_list")
            unknowns = 0
        elif seen == "unknown":
            unknowns += 1
        else:
            unknowns = 0
        # "studies" → cycle immediately.
        # "unknown" → tolerate a few times (page loading, SPA glitch) before
        #   falling back to a model cycle; avoids wasting 5–7 model steps on
        #   a page that just hasn't rendered yet.
        # "empty" → keep waiting.
        # Safety cycle: only fire when Pusher is NOT giving us events, and
        #   even then only after FULL_CYCLE_EVERY_PUSHER_DOWN seconds. With a
        #   connected sniffer we trust the push stream and don't burn the GPU
        #   on blind polls.
        safety_due = (
            FULL_CYCLE_EVERY_PUSHER_DOWN > 0
            and not pl.connected
            and time.time() - since > FULL_CYCLE_EVERY_PUSHER_DOWN
        )
        should_cycle = (
            seen == "studies"
            or (seen == "unknown" and unknowns >= 3)
            or safety_due
        )
        if should_cycle:
            if checks > 1:
                reason = ("ora c'è qualcosa." if seen == "studies" else
                          "pusher offline, ciclo di controllo." if safety_due else
                          "pagina non riconosciuta, passo al modello.")
                _push_event({"type": "notice",
                             "text": f"Elenco controllato {checks} volte senza modello: " + reason})
            return False
        hub.publish("live", {"type": "phase", "phase": "watch", "ts": time.time(),
                             "checks": checks,
                             "pusher": pl.connected})


def _choose_site(site: str, url: str) -> str:
    """Activate the site profile for a run (see sites.resolve) and say which.
    The browser's current page is only looked at when the run has no start
    URL of its own."""
    current_url = ""
    if not url and not site and not sites.configured():
        try:
            current_url = get_current_url()
        except Exception:
            current_url = ""
    name = sites.resolve(site, url, current_url)
    sites.use(name, remember=True)
    with state_lock:
        state["site"] = name
    _push_event({"type": "notice", "text": f"Profilo sito: {name}"})
    return name


def agent_driver(task: str, url: str, max_rounds: int, loop: bool, poll_interval: float,
                  batch_fill: bool = False, wander_seconds: float = 0, skip_av: bool = False,
                  site: str = ""):
    stop_event = state["stop_event"]
    answer_queue = state["answer_queue"]
    try:
        _choose_site(site, url)
    except ValueError as e:
        _push_event({"type": "error", "message": str(e)})
        with state_lock:
            state["running"] = False
            state["stopping"] = False
        _publish_state()
        return
    # Each cycle starts a FRESH conversation, with no memory of earlier
    # cycles. Threading history made the model anchor on old conclusions:
    # with 3 studies on screen it answered "still shows exactly one
    # available survey" (what an earlier cycle had seen). Measured on the
    # same screenshot: with prior history or even with a few lines of text
    # notes about earlier cycles it repeated the wrong count 3/3 times;
    # with no prior context it saw all three every time. Nothing is lost:
    # the platform drops completed studies from the list, and eligibility is
    # cheap to re-judge each cycle.
    home_url = url  # where each new cycle starts from; captured below if not given
    inhibitor = _inhibit_sleep()
    if loop:
        _ensure_pusher(stop_event)
    try:
        cycle = 0
        errors_in_a_row = 0
        incomplete_streak = 0
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
                ok, _, cycle_answer = run_one_cycle(task, url if cycle == 1 else "", max_rounds,
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
            # Any outcome beyond "nothing on the list" deserves a Telegram
            # ping -- previously only errors and successful CODE answers were
            # reported, so a timed-out study ended silently and the user had
            # no way to know from the phone.
            _ca = (cycle_answer or "").strip()
            if _ca and _ca not in ("NONE", "STOPPED") and "max_rounds=" not in _ca:
                head = _ca.split("\n", 1)[0][:300]
                if agent_core.completion_code_of(_ca):
                    emoji = "✅"
                elif _ca == "NOCODE" or _ca.upper().startswith("CODE:"):
                    emoji = "⚠️"; head = "Completato senza codice"
                else:
                    emoji = "ℹ️"
                _flag = _last_run_flag()
                if _flag:
                    emoji = "🚩"
                    head = f"{head}\n⚠️ {_flag}"
                agent_core._notify_telegram(f"{emoji} Ciclo {cycle}: {head}")
            # Inter-cycle idle: a real user doesn't start the next study 2s
            # after submitting the last. 8-25s normally; ~5% of the time a
            # longer "coffee break" of 25-70s. Only after a real completion
            # (CODE/NOCODE/other terminate), not after NONE (nothing on the
            # list) or STOPPED (user interrupt) where the dashboard poll
            # already handles pacing.
            if (loop and not stop_event.is_set() and _ca
                    and _ca not in ("NONE", "STOPPED")
                    and "max_rounds=" not in _ca):
                if random.random() < 0.05:
                    idle = random.uniform(25, 70)
                else:
                    idle = random.uniform(8, 25)
                if stop_event.wait(idle):
                    return
            if not ok or not loop or stop_event.is_set():
                break
            # INCOMPLETE (max_rounds reached) means the model was still mid-work:
            # likely on an external survey tab. Calling _wait_for_studies here
            # would reset_to(home_url) and destroy the survey tab (closes every
            # other tab, navigates the active one). Skip the inter-cycle check
            # and jump straight to the next cycle so the model keeps working on
            # the survey. Cap consecutive INCOMPLETE cycles so a truly stuck
            # survey doesn't block the loop forever.
            if "max_rounds=" in cycle_answer:
                incomplete_streak += 1
                if incomplete_streak >= MAX_INCOMPLETE_STREAK:
                    _push_event({"type": "notice",
                                 "text": f"⚠️ Studio bloccato dopo {incomplete_streak} "
                                         f"cicli INCOMPLETE: torno alla lista."})
                    incomplete_streak = 0
                else:
                    _push_event({"type": "notice",
                                 "text": f"Ciclo INCOMPLETE ({incomplete_streak}): "
                                         f"continuo dallo stesso punto senza resettare il tab."})
                    continue
            else:
                incomplete_streak = 0
            if _wait_for_studies(home_url, poll_interval, stop_event):
                return
    except Exception as e:
        _push_event({"type": "error", "message": str(e)})
        tb = traceback.format_exc()[-300:]
        agent_core._notify_telegram(f"❌ Errore agent!\n{type(e).__name__}: {str(e)[:150]}\n\n{tb}")
    finally:
        pusher_listener.stop()
        if inhibitor is not None:
            inhibitor.terminate()
        with state_lock:
            state["running"] = False
            state["stopping"] = False
            state["phase"] = None
        hub.publish("live", {"type": "phase", "phase": "idle", "ts": time.time()})
        _publish_state()


def _reclassify(s: dict) -> dict:
    """Bring a summary written by an older classifier up to date.

    The verdict that moved in SUMMARY_V 2 depends only on the terminate
    answer, which every summary already stores, so the statistics correct
    themselves on read instead of re-parsing a few thousand transcripts.
    Nothing is rewritten: the file keeps its old verdict and is reclassified
    again next time, which costs one string test per run.
    """
    if s.get("v") == agent_core.SUMMARY_V:
        return s
    answer = s.get("answer") or ""
    code = agent_core.completion_code_of(answer)
    if code:
        s["outcome"], s["code"] = "CODE", code
    elif answer.upper().startswith("CODE:") or answer == "NOCODE":
        s["outcome"], s["code"] = "NOCODE", ""
    else:
        s["code"] = ""
    s["v"] = agent_core.SUMMARY_V
    return s


def _mark_repeats(records: list) -> list:
    """Re-label the cycles that reported a completion already banked.

    A cycle that begins while the browser still sits on the previous study's
    submissions/complete?cc= URL -- or that walks back onto it with
    history_back -- reads that code again and ends in a handful of steps. The
    submission was made once, so counting it again overstates the studies
    done: run_20261109_183807 re-banked CODE: CBBD09819 nineteen seconds
    after run_20261109_175856 earned it.

    A study can be taken only once on the platform, so the same study id twice is
    always the same submission. A repeated code alone is not: one
    researcher's recurring series reuses a single code across different study
    ids (C3YV0TJP came back under four of them), so a code counts as a repeat
    only for a cycle that never reached a study page of its own.

    `records` must be in chronological order; the first cycle to report a
    completion keeps it and the later ones are flagged. The flag is written to
    a key of its own rather than over `outcome`, so running this again on the
    same (cached) records recomputes the same answer instead of building on
    its own previous verdict.
    """
    seen_studies, seen_codes = set(), set()
    for r in records:
        r["repeat"] = False
        if r.get("outcome") != "CODE":
            continue
        # The last study page the cycle was on, not the first: a cycle that
        # looks at one study, skips it and completes another must be judged on
        # the one it finished. A study only ever enters the set below from a
        # cycle that banked a code, so a page merely looked at and skipped
        # cannot make a later completion read as a repeat.
        sid = (r.get("studies") or [None])[-1]
        code = r.get("code") or ""
        if (sid in seen_studies) if sid else (code in seen_codes):
            r["repeat"] = True
            continue
        if sid:
            seen_studies.add(sid)
        if code:
            seen_codes.add(code)
    return records


def _outcome_of(record: dict) -> str:
    """The run's outcome as every surface should report it: REPEAT for a cycle
    that re-banked a completion, TERMINATE for a user stop (STOPPED is an
    internal name the page never showed)."""
    if record.get("repeat"):
        return "REPEAT"
    o = record.get("outcome")
    return "TERMINATE" if o == "STOPPED" else o


def _run_record(tf) -> dict | None:
    """One run for the dashboard, from its summary.json when there is one.

    Runs written before summaries existed are summarized once and the
    result saved next to them, so the dashboard stops re-parsing every
    transcript on every request. The run in progress is never cached: its
    summary is written by run_agent when it ends.
    """
    from datetime import datetime
    run_dir = tf.parent
    try:
        started = datetime.strptime(run_dir.name.replace("run_", ""), "%Y%m%d_%H%M%S")
    except Exception:
        return None
    summ_file = run_dir / "summary.json"
    s = None
    if summ_file.exists():
        try:
            s = json.loads(summ_file.read_text())
        except Exception:
            s = None
    if s is not None:
        s = _reclassify(s)
    if s is None:
        screens = list(run_dir.glob("screenshot_*.png"))
        ended = (datetime.fromtimestamp(max(x.stat().st_mtime for x in screens))
                 if screens else started)
        s = agent_core.summarize_transcript(_read_transcript(tf).splitlines(), started, ended)
        finished = s["outcome"] != "INCOMPLETE" or time.time() - ended.timestamp() > 3600
        if finished:
            try:
                summ_file.write_text(json.dumps(s, indent=1))
            except Exception:
                pass
    ended = datetime.fromisoformat(s["ended"]) if s.get("ended") else started
    outcome = "TERMINATE" if s["outcome"] == "STOPPED" else s["outcome"]
    return {"started": started, "ended": ended, "duration_s": s.get("duration_s", 0),
            "steps": s.get("steps", 0), "outcome": outcome, "answer": s.get("answer", ""),
            "code": s.get("code") or "", "studies": s.get("studies") or [],
            "code_source": s.get("code_source"), "where": s.get("steps_where") or {},
            "gen_tps": s.get("gen_tps_median"), "flagged": s.get("flagged") or "",
            "site": s.get("site") or _legacy_site(s)}


def _is_platform(name: str) -> bool:
    """Whether a profile runs the task-platform rules (task list, timer,
    completion codes): the statistics about tasks only apply to those."""
    try:
        return any(getattr(c, "name", "") == "task_platform"
                   for c in sites.get(name).behaviors())
    except Exception:
        return False


def _legacy_site(s: dict) -> str:
    """The profile of a run recorded before runs named theirs. One that saw a
    task list, a task or a completion code ran on the platform profile. Any
    other (a cycle that failed early, a short plain run) carries no reliable
    sign either way, and is given the profile last used."""
    w = s.get("steps_where") or {}
    if (w.get("list") or w.get("study") or s.get("code_source")
            or s.get("outcome") in ("CODE", "NOCODE", "NONE")):
        return next((n for n in sites.names() if _is_platform(n)), "generic")
    return sites.last_used() or "generic"


def _profiles_info() -> dict:
    """What the statistics page needs to know about each profile."""
    out = {}
    for n in sites.names():
        try:
            prof = sites.get(n)
            out[n] = {"title": getattr(prof, "title", n),
                      "label": "Generico" if n == "generic" else getattr(prof, "title", n),
                      "platform": _is_platform(n),
                      "history": bool(getattr(prof, "history_url", ""))}
        except Exception:
            pass
    return out


_record_cache: dict = {}


def _run_record_cached(tf) -> dict | None:
    """_run_record, remembered per run until its files change.

    A finished run never changes, so the statistics page reads each
    summary.json once per server life instead of on every refresh -- with
    a few hundred runs a week that is the difference between a constant
    and a growing cost per request.
    """
    run_dir = tf.parent
    try:
        summ = run_dir / "summary.json"
        key = (summ.stat().st_mtime if summ.exists() else None, tf.stat().st_mtime)
    except OSError:
        key = None
    hit = _record_cache.get(run_dir.name)
    if key is not None and hit and hit[0] == key:
        return hit[1]
    rec = _run_record(tf)
    _record_cache[run_dir.name] = (key, rec)
    return rec


def _dashboard_data() -> dict:
    """Everything the statistics page shows, as plain JSON.

    The page does the aggregation itself, so switching period is instant and
    the server only has to list runs. One row per run:
    [start, end, seconds, steps, outcome, answer, code_source,
     steps_on_list, steps_in_study, steps_elsewhere, model_tokens_per_s,
     qc_flag, site]
    """
    from datetime import datetime as _dt
    runs = []
    records = [r for r in (_run_record_cached(tf) for tf in _transcript_files()) if r]
    # _transcript_files() is sorted by run directory, i.e. by start time, which
    # is the order _mark_repeats needs.
    for r in _mark_repeats(records):
        w = r.get("where") or {}
        runs.append([int(r["started"].timestamp()), int(r["ended"].timestamp()),
                     int(r.get("duration_s") or 0), int(r.get("steps") or 0), _outcome_of(r),
                     (r.get("answer") or "")[:80], r.get("code_source"),
                     int(w.get("list", 0)), int(w.get("study", 0)), int(w.get("elsewhere", 0)),
                     r.get("gen_tps"), r.get("flagged") or "", r.get("site") or "generic"])
    subs = []
    for s in list(_submissions_cache.get("data") or []):
        ts = None
        for fmt in ("%d %b %Y, %H:%M", "%d %b %Y"):
            try:
                ts = int(_dt.strptime(s.get("date_full", ""), fmt).timestamp())
                break
            except ValueError:
                pass
        try:
            reward = float(s.get("reward") or 0)
        except ValueError:
            reward = 0.0
        subs.append({"title": s.get("title", ""), "status": s.get("status", ""), "ts": ts,
                     "reward": reward, "currency": s.get("currency", "£")})
    try:
        skipped = [{"title": v.get("title", ""), "reason": v.get("reason", ""),
                    "ts": v.get("ts"), "hits": v.get("hits", 1)}
                   for v in study_cache.entries().values()]
    except Exception:
        skipped = []
    return {"generated": time.time(), "runs": runs, "submissions": subs,
            "profiles": _profiles_info(),
            # the platform's own SUMMARY block: the account's real lifetime totals,
            # and whether the local history is still being read in.
            "subs_header": dict(_submissions_cache.get("header") or {}),
            "subs_complete": bool(_submissions_cache.get("complete")),
            "submissions_ts": _submissions_cache.get("ts") or None, "skipped": skipped,
            "agent": _status_payload(with_events=False, fresh_url=False),
            "slow_tps": agent_core.SLOW_TPS}


def _build_dashboard() -> str:
    """The statistics page: a static template read on every request (like
    gui_index.html, so its design can change without restarting the server)
    with the data inlined, so the first paint needs no second round trip.
    The page then keeps itself fresh from /dashboard.json and the SSE feed."""
    tpl = (HERE / "dashboard.html").read_text()
    data = json.dumps(_dashboard_data(), separators=(",", ":")).replace("</", "<\\/")
    return tpl.replace("/*__DATA__*/null", data, 1)


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
        elif self.path == "/dashboard.json":
            self._send_json(_dashboard_data())
        elif self.path == "/study_cache":
            self._send_json({"entries": study_cache.entries(),
                             "titles": study_cache.titles()})
        elif self.path == "/model/status":
            global _active_model
            current = _active_model or _detect_active_model()
            _active_model = current
            cfg = MODELS.get(current, {})
            self._send_json({
                "active": current,
                "label": cfg.get("label", "unknown"),
                "multimodal": cfg.get("multimodal", False),
                "healthy": _model_healthy(current) if current else False,
                "models": {k: {"label": v["label"], "multimodal": v["multimodal"]}
                           for k, v in MODELS.items()},
            })
        elif self.path == "/local":
            html = (HERE / "local_index.html").read_bytes()
            self._send_bytes(html, "text/html; charset=utf-8")
        elif self.path == "/local/history":
            with _local_lock:
                hist = _local_current()
                visible = [m for m in hist if m["role"] != "system"]
            self._send_json({"messages": visible, "chat_id": _local_active})
        elif self.path == "/local/chats":
            with _local_lock:
                items = []
                for cid, msgs in _local_sessions.items():
                    items.append({"id": cid,
                                  "title": _local_title(msgs) or "(empty)",
                                  "turns": sum(1 for m in msgs if m.get("role") == "user")})
                items.sort(key=lambda x: x["id"], reverse=True)
                self._send_json({"chats": items, "active": _local_active})
        elif self.path == "/local/thinking":
            self._send_json({
                "enabled": local_agent.ENABLE_THINKING,
                "effort": local_agent.REASONING_EFFORT,
            })
        elif self.path.startswith("/local/scratch/"):
            # Serve files that write_file dropped into the Qwen scratchpad so
            # the user can preview them (HTML renders, SVG/PNG inline, …).
            rel = self.path[len("/local/scratch/"):].split("?", 1)[0]
            try:
                base = local_agent.SCRATCH_DIR.resolve()
                target = (base / rel).resolve()
                if not str(target).startswith(str(base)) or not target.is_file():
                    self.send_response(404); self.end_headers(); return
                import mimetypes
                ctype, _ = mimetypes.guess_type(str(target))
                self._send_bytes(target.read_bytes(),
                                 ctype or "application/octet-stream")
            except Exception:
                self.send_response(404); self.end_headers()
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
                context = str(body.get("context", "")).strip()
                task = str(body.get("task", ""))
                if context:
                    task = context + "\n\n" + task
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
                    # Remember the request so a watchdog restart of this server
                    # (pw_bridge._restart_server) can start the same run again.
                    try:
                        _lr = HERE.parent / "var" / "last_run.json"
                        _lr.parent.mkdir(parents=True, exist_ok=True)
                        _lr.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
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
                          skip_av),
                    kwargs={"site": str(body.get("site", "") or "")}, daemon=True,
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
                    # "" = automatic (see sites.resolve)
                    "site": str(body.get("site", "") or ""),
                }
                save_preset(preset)
                return self._send_json({"ok": True})

            if self.path == "/model/switch":
                if self._agent_running():
                    return self._send_json({"error": "agent is running — stop it first"}, status=409)
                body = self._read_json()
                target = str(body.get("model", "")).strip()
                result = switch_model(target)
                status = 200 if "ok" in result else 400
                return self._send_json(result, status=status)

            if self.path == "/presets/delete":
                body = self._read_json()
                delete_preset(str(body.get("name", "")))
                return self._send_json({"ok": True})

            if self.path in ("/local/chat", "/local/chat/stream"):
                global _local_busy
                stream = (self.path == "/local/chat/stream")
                with _local_lock:
                    if _local_busy:
                        return self._send_json({"error": "busy"}, status=409)
                    _local_busy = True
                cancel.reset()
                body = self._read_json()
                msg = str(body.get("message", "")).strip()
                if not msg:
                    with _local_lock:
                        _local_busy = False
                    return self._send_json({"error": "empty message"}, status=400)

                if not stream:
                    try:
                        events = []
                        response = ""
                        hist = _local_current()
                        for ev in local_agent.run_chat(msg, hist):
                            events.append(ev)
                            if ev.get("type") == "response":
                                response = ev.get("content", "")
                        with _local_lock:
                            _local_save()
                        self._send_json({"ok": True, "response": response, "events": events})
                    except Exception as e:
                        self._send_json({"error": str(e)}, status=500)
                    finally:
                        with _local_lock:
                            _local_busy = False
                    return

                # Streaming NDJSON: one JSON event per line, flushed as soon as
                # local_agent yields it so the UI can show thinking → tokens →
                # tools → final response in real time. A background thread
                # drives run_chat so the on_delta token callback and the
                # generator yields both feed the same queue the writer drains.
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                def _emit(ev: dict):
                    try:
                        self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode())
                        self.wfile.flush()
                    except Exception:
                        pass
                evq: queue.Queue = queue.Queue()
                _SENTINEL = object()
                hist = _local_current()
                def _runner():
                    try:
                        for ev in local_agent.run_chat(
                                msg, hist,
                                on_event=lambda e: evq.put(e)):
                            evq.put(ev)
                    except Exception as e:
                        evq.put({"type": "error", "message": str(e)})
                    finally:
                        evq.put(_SENTINEL)
                threading.Thread(target=_runner, daemon=True).start()
                try:
                    while True:
                        ev = evq.get()
                        if ev is _SENTINEL:
                            break
                        _emit(ev)
                    with _local_lock:
                        _local_save()
                    _emit({"type": "end"})
                except Exception as e:
                    _emit({"type": "error", "message": str(e)})
                finally:
                    with _local_lock:
                        _local_busy = False
                return

            if self.path == "/local/chat/cancel":
                cancel.cancel_all()
                return self._send_json({"ok": True})

            if self.path == "/local/clear":
                with _local_lock:
                    _local_current().clear()
                    _local_save()
                return self._send_json({"ok": True})

            if self.path == "/local/chats/new":
                global _local_active
                with _local_lock:
                    cid = f"chat-{int(time.time()*1000)}"
                    _local_sessions[cid] = []
                    _local_active = cid
                    _local_save()
                return self._send_json({"ok": True, "chat_id": cid})

            if self.path == "/local/chats/switch":
                body = self._read_json()
                cid = str(body.get("chat_id", ""))
                with _local_lock:
                    if cid not in _local_sessions:
                        return self._send_json({"error": "no such chat"}, status=404)
                    _local_active = cid
                    _local_save()
                return self._send_json({"ok": True, "chat_id": cid})

            if self.path == "/local/chats/delete":
                body = self._read_json()
                cid = str(body.get("chat_id", ""))
                with _local_lock:
                    if cid not in _local_sessions:
                        return self._send_json({"error": "no such chat"}, status=404)
                    del _local_sessions[cid]
                    if _local_active == cid:
                        if _local_sessions:
                            _local_active = next(iter(_local_sessions))
                        else:
                            new_id = f"chat-{int(time.time()*1000)}"
                            _local_sessions[new_id] = []
                            _local_active = new_id
                    _local_save()
                return self._send_json({"ok": True, "active": _local_active})

            if self.path == "/local/thinking":
                body = self._read_json()
                if "enabled" in body:
                    local_agent.ENABLE_THINKING = bool(body["enabled"])
                if "effort" in body:
                    eff = str(body["effort"]).lower()
                    # This Qwen build rejects "high"; keep the UI label but
                    # send what the model's chat template accepts.
                    eff = {"high": "xhigh"}.get(eff, eff)
                    if eff in ("low", "medium", "xhigh"):
                        local_agent.REASONING_EFFORT = eff
                return self._send_json({
                    "ok": True,
                    "enabled": local_agent.ENABLE_THINKING,
                    "effort": local_agent.REASONING_EFFORT,
                })

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


def _pick_preset(presets: list, name: str = ""):
    """(preset, None) or (None, message). Empty name -> the first preset;
    otherwise an exact name (any case), else a unique partial match."""
    if not presets:
        return None, "❌ Nessun preset salvato. Avvia manualmente dalla dashboard."
    name = (name or "").strip().lower()
    if not name:
        return presets[0], None
    exact = [p for p in presets if p.get("name", "").lower() == name]
    if exact:
        return exact[0], None
    partial = [p for p in presets if name in p.get("name", "").lower()]
    if len(partial) == 1:
        return partial[0], None
    names = "\n".join(f"• {p.get('name')}" for p in (partial or presets))
    why = "Più preset corrispondono" if partial else "Nessun preset corrisponde"
    return None, f"❓ {why} a «{name}»:\n{names}"


def _daily_summary_text(day=None) -> str:
    """What happened on `day` (default today), from the run transcripts,
    the skipped-studies memory and the submissions cache."""
    from datetime import date, datetime as _dt
    day = day or date.today()
    tag = day.strftime("%Y%m%d")
    # Repeats can only be told apart from the whole history (a cycle re-banks
    # a code earned on an earlier day), so every run is read and the day is
    # selected afterwards. The records are cached per run, so this costs no
    # transcript parsing after the first call.
    allrecs = _mark_repeats([r for r in (_run_record_cached(tf)
                                         for tf in _transcript_files()) if r])
    runs = [r for r in allrecs if r["started"].date() == day]
    done, none, other, repeat = [], 0, 0, 0
    for r in runs:
        outcome = _outcome_of(r)
        if outcome == "CODE":
            src = r.get("code_source")
            done.append(f"CODE: {r['code']}" + (" (da URL)" if src == "url" else ""))
        elif outcome == "NONE":
            none += 1
        elif outcome == "REPEAT":
            repeat += 1
        else:
            other += 1
    skipped = [v for v in study_cache.entries().values()
               if _dt.fromtimestamp(v.get("ts", 0)).date() == day]
    lines = [f"📊 Riepilogo {day.strftime('%d/%m')}",
             f"🔄 Cicli: {len(runs)} (elenco vuoto {none}, interrotti {other}"
             + (f", codice già registrato {repeat}" if repeat else "") + ")",
             f"✅ Studi completati: {len(done)}"]
    lines += [f"   • {d}" for d in done]
    # A flagged submission still earns a code, so it is counted above as
    # completed; it is listed again here because the researcher said the
    # work failed their checks, and that is the only notice there will be.
    flagged = [r for r in runs if r.get("flagged")]
    if flagged:
        lines.append(f"🚩 Segnalati dal ricercatore: {len(flagged)}")
        lines += [f"   • {r['flagged'][:120]}" for r in flagged[:4]]
    lines.append(f"⏭ Studi scartati: {len(skipped)}")
    lines += [f"   • {v.get('title') or 'senza titolo'} — {v.get('reason')}" for v in skipped[:8]]
    with state_lock:
        errs = (state.get("stats") or {}).get("errors", 0)
    if errs:
        lines.append(f"⚠️ Errori nella run in corso: {errs}")
    speed = agent_core.model_speed()
    if speed:
        lines.append(f"🧠 Modello: {speed['median_gen_tps']:.1f} token/s (mediana recente)")
    # Keep currencies separate: the platform pays some studies in £ and others
    # in $/€, so summing them into one scalar silently misrepresents the day.
    by_cur: dict[str, float] = {}
    for s in list(_submissions_cache.get("data") or []):
        try:
            when = _dt.strptime(s.get("date_full", ""), "%d %b %Y, %H:%M").date()
        except ValueError:
            continue
        if when == day and s.get("reward") and s.get("status", "").lower() in ("approved", "awaiting review"):
            try:
                amt = float(s["reward"])
            except (ValueError, TypeError):
                continue
            by_cur[s.get("currency", "£")] = by_cur.get(s.get("currency", "£"), 0.0) + amt
    if by_cur:
        lines.append("💰 Approvati o in revisione oggi: " + _fmt_money_by_cur(by_cur))
    return "\n".join(lines)


def _fmt_money_by_cur(by_cur: dict[str, float]) -> str:
    """Join non-zero currency buckets with " + " so £ and $ stay separate.
    Keeps the display honest when the platform mixes GBP/USD studies in one day.
    """
    parts = [f"{sym}{amt:.2f}" for sym, amt in by_cur.items() if amt > 0]
    return " + ".join(parts) if parts else "£0.00"


_IT_DAYS = ("lun", "mar", "mer", "gio", "ven", "sab", "dom")
_IT_MONTHS = ("gen", "feb", "mar", "apr", "mag", "giu",
              "lug", "ago", "set", "ott", "nov", "dic")


def _earnings_text() -> str:
    """Earnings by the date the study was taken, plus the account's own totals.

    The periods are summed from the local submission store; the all-time
    figures come from the platform's SUMMARY block, which states them itself.
    Summing the rows for an all-time total is what made this report read
    "£0.70 + $27.39 over 8 studies" while the page said £590.94 + $939.95
    over 559: only the first of 41 pages was ever read.
    Per currency throughout, because the platform pays some studies in £ and
    others in $ and the two are never added together.
    """
    from datetime import date, datetime as _dt, timedelta
    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    month_start = today.replace(day=1)
    # The window each line covers, spelled out. The statistics page offers
    # rolling windows ("ultimi 7 giorni", "ultimi 30 giorni") whose figures
    # are much larger than these calendar ones, and two numbers under two
    # similar words with no window stated is a discrepancy nobody can resolve.
    PERIODS = (("today", f"Oggi (da mezzanotte)", today),
               ("week", f"Settimana (da {_IT_DAYS[week_start.weekday()]} {week_start.day})",
                week_start),
               ("month", f"Mese (dal 1 {_IT_MONTHS[month_start.month - 1]})", month_start))

    ok: dict[str, dict[str, float]] = {k: {} for k, _, _ in PERIODS}
    wait: dict[str, dict[str, float]] = {k: {} for k, _, _ in PERIODS}
    n_ok = {k: 0 for k, _, _ in PERIODS}
    n_wait = {k: 0 for k, _, _ in PERIODS}

    with _submissions_lock:
        subs = list(_submissions_cache.get("data") or [])
        header = dict(_submissions_cache.get("header") or {})
        complete = bool(_submissions_cache.get("complete"))

    for s in subs:
        status = (s.get("status") or "").lower()
        if status not in sites.current.history_paid_statuses and status != "awaiting review":
            continue                   # returned / timed out / rejected pay nothing
        try:
            amt = float(s.get("reward") or 0)
        except (ValueError, TypeError):
            continue
        if not amt:
            continue
        cur = s.get("currency", "£")
        df = s.get("date_full", "")
        d = None
        for fmt in ("%d %b %Y, %H:%M", "%d %b %Y"):
            try:
                d = _dt.strptime(df, fmt).date()
                break
            except ValueError:
                pass
        if d is None:
            continue
        for key, _label, since in PERIODS:
            if d >= since:
                bucket, count = ((ok, n_ok) if status in sites.current.history_paid_statuses
                                 else (wait, n_wait))
                bucket[key][cur] = bucket[key].get(cur, 0.0) + amt
                count[key] += 1

    lines = ["💰 Guadagni (per data di partecipazione)",
             f"   incassati = approvati + screening fee, come li conta {sites.current.title}"]
    for key, label, _since in PERIODS:
        money = _fmt_money_by_cur(ok[key]) if ok[key] else "—"
        if n_ok[key] or n_wait[key]:
            tail = f"{n_ok[key]} incassati"
            if n_wait[key]:
                tail += (f" · {n_wait[key]} in revisione "
                         f"{_fmt_money_by_cur(wait[key])}")
        else:
            tail = "nessuno studio"
        lines.append(f"   {label}: {money}  ·  {tail}")

    lines.append("")
    if header.get("earned"):
        lines.append(f"🏦 Totale account: {_fmt_money_by_cur(header['earned'])}"
                     + (f" su {header['approved']} studi approvati"
                        if header.get("approved") else ""))
    if header.get("balance"):
        # the platform's wallet converts everything to one currency: its "Pending"
        # £7.74 is the same money as the £2.85 + $6.71 awaiting review above,
        # at its own rate. Labelled as the wallet, not as a second "in
        # revisione", so two different-looking figures do not sit under one
        # word.
        bal = f"   Portafoglio: {_fmt_money_by_cur(header['balance'])} disponibili"
        if header.get("pending"):
            bal += (f" · {_fmt_money_by_cur(header['pending'])} in attesa "
                    f"({sites.current.title} converte tutto in £)")
        lines.append(bal)
    if header.get("total"):
        read = len(subs)
        if not complete:
            lines.append(f"   ⏳ Storico letto: {read} di {header['total']} "
                         "submission (le altre pagine arrivano a cicli fermi)")
        else:
            lines.append(f"   Storico completo: {read} submission")
    elif not subs:
        lines.append("Nessuna submission letta ancora.")
    return "\n".join(lines)


SUMMARY_HOUR = int(os.environ.get("FARA_SUMMARY_HOUR", "21"))


def _daily_summary_loop():
    """Send the day's summary on Telegram once a day at SUMMARY_HOUR."""
    if TEST_INSTANCE:
        return
    from datetime import datetime as _dt, timedelta
    while True:
        now = _dt.now()
        at = now.replace(hour=SUMMARY_HOUR, minute=0, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        time.sleep((at - now).total_seconds())
        try:
            agent_core._notify_telegram(_daily_summary_text())
        except Exception:
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
            {"command": "status",  "description": "Stato + screenshot + studi recenti"},
            {"command": "start",   "description": "Avvia l'agente"},
            {"command": "stop",    "description": "Ferma l'agente"},
            {"command": "restart", "description": "Ferma, torna a studies, riavvia"},
            {"command": "normal",  "description": "Modalità normale (qualità massima)"},
            {"command": "hurry",   "description": "Modalità veloce (immagini ridotte)"},
            {"command": "superfast","description": "Modalità super-veloce (massima velocità)"},
            {"command": "inject",  "description": "Manda un'istruzione extra all'agente"},
            {"command": "presets", "description": "Elenco dei preset (/start <nome>)"},
            {"command": "earnings", "description": "Guadagni: oggi, settimana, mese, totale"},
            {"command": "riepilogo", "description": "Riepilogo della giornata"},
            {"command": "help",    "description": "Lista comandi"},
            {"command": "hardrestart", "description": "⚠️ Riavvia TUTTO il server (distruttivo)"},
        ]}).encode()
        req = _ur.Request(f"{base}/setMyCommands", data=cmds,
                          headers={"Content-Type": "application/json"})
        _ur.urlopen(req, timeout=8)
    except Exception:
        pass

    def _do_start(name: str = "") -> str:
        p, err = _pick_preset(load_presets(), name)
        if err:
            return err
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
        context = str(p.get("context", "")).strip()
        task = str(p.get("task", ""))
        if context:
            task = context + "\n\n" + task
        threading.Thread(
            target=agent_driver,
            args=(
                task,
                "",   # no forced navigation — start from wherever the browser is
                int(p.get("max_rounds", 20)),
                bool(p.get("loop", False)),
                float(p.get("poll_interval", 60)),
                bool(p.get("batch_fill", False)),
                max(0.0, float(p.get("wander_seconds", 90))),
                bool(p.get("skip_av", False)),
            ),
            kwargs={"site": str(p.get("site", "") or "")},
            daemon=True,
        ).start()
        return f"▶️ Agente avviato — preset: {p.get('name', '?')}"

    def _do_restart(name: str = "") -> str:
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
            home = agent_core.studies_url()
            if home:
                pw_bridge.reset_to(home)
                pw_bridge.wait_for_content()
        except Exception:
            pass
        return _do_start(name)

    def _do_hardrestart() -> str:
        """Kill THIS gui_server process and relaunch it via scripts/restart.sh,
        then navigate the browser to the platform's task list. The server
        comes back EMPTY (no agent running). Done through a detached shell so
        the script survives this very process being killed by its pkill step."""
        script = str(HERE.parent / "scripts" / "restart.sh")
        if not os.path.exists(script):
            return f"❌ Script non trovato: {script}"
        port = str(_PORT)
        # Build a one-liner that: (1) sleeps so our ACK flushes to Telegram,
        # (2) runs restart.sh (which kills us), (3) waits for the new server
        # to respond, (4) navigates to the studies list. Each step tolerates
        # transient failures -- we don't want the browser-nav step to abort
        # the whole restart on a 1-second race.
        studies_url = agent_core.studies_url()
        oneliner = (
            f"sleep 1 && "
            f"bash {shlex.quote(script)} > /tmp/fara-hardrestart.log 2>&1; "
            f"for i in $(seq 1 30); do "
            f"  curl -sf -o /dev/null --max-time 2 http://127.0.0.1:{port}/agent/status && break; "
            f"  sleep 1; "
            f"done"
        )
        if studies_url:
            oneliner += (
                f"; curl -s -X POST http://127.0.0.1:{port}/navigate "
                f"  -H 'Content-Type: application/json' "
                f"  -d {shlex.quote(json.dumps({'url': studies_url}))} > /dev/null 2>&1 || true"
            )
        try:
            subprocess.Popen(
                ["bash", "-c", oneliner],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
            )
        except Exception as e:
            return f"❌ Non riesco a lanciare il restart: {e}"
        where = studies_url.split("://", 1)[-1] if studies_url else "la pagina corrente"
        return f"🔄 Hard restart avviato: il server si ucciderà, ripartirà vuoto e il browser tornerà a {where}."

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
        pl = pusher_listener.get_listener()
        lines.append("📡 Pusher: " + ("connesso" if pl.connected else "disconnesso")
                      + (f" ({pl.stats['events']} eventi)" if pl.stats["events"] else ""))
        try:
            n_skipped = len(study_cache.entries())
            if n_skipped:
                lines.append(f"⏭ Studi scartati in memoria: {n_skipped}")
        except Exception:
            pass

        # Submissions from the platform (opens a tab briefly when agent is idle)
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

                total_24h_by_cur: dict[str, float] = {}
                n_24h = 0
                # Sum over EVERY submission in the window, not only the eight
                # that fit on screen: the total used to be computed inside the
                # display loop, so a ninth study of the day was money the
                # report simply left out.
                for s in subs:
                    if (s.get("reward") and s["status"].lower() in COUNTS_AS_EARNED):
                        cur = s.get("currency", "£")
                        try:
                            try:
                                d = datetime.strptime(s.get("date_full", ""), "%d %b %Y, %H:%M")
                            except ValueError:
                                d = datetime.strptime(s.get("date_full", ""), "%d %b %Y")
                        except Exception:
                            continue
                        if d >= cutoff_24h:
                            total_24h_by_cur[cur] = (total_24h_by_cur.get(cur, 0.0)
                                                     + float(s["reward"]))
                            n_24h += 1

                lines.append("")
                lines.append("📋 Studi recenti:")
                for s in subs[:8]:
                    emoji = STATUS_EMOJI.get(s["status"].lower(), "❔")
                    date_disp = f" ({s['date']})" if s.get("date") else ""
                    rwd = s.get("reward", "")
                    cur = s.get("currency", "£")
                    reward_disp = f" {cur}{rwd}" if rwd else ""
                    lines.append(f"{emoji} {s['title']}{date_disp}{reward_disp}")
                if len(subs) > 8:
                    lines.append(f"   … e altri {len(subs) - 8}")

                lines.append(f"💰 Approvati nelle ultime 24h: "
                             f"{_fmt_money_by_cur(total_24h_by_cur)} ({n_24h} studi)")
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
                tg_send(_do_start(arg))
            elif text in ("/presets", "presets", "/preset", "preset"):
                ps = load_presets()
                tg_send("📋 Preset salvati:\n" + "\n".join(f"• {p.get('name')}" for p in ps)
                        + "\n\n/start <nome> per avviarne uno (basta una parte del nome)"
                        if ps else "Nessun preset salvato.")
            elif text in ("/riepilogo", "riepilogo", "/summary", "summary"):
                tg_send(_daily_summary_text())
            elif text in ("/earnings", "earnings", "/guadagni", "guadagni"):
                tg_send(_earnings_text())
            elif text in ("/status", "status", "/stato", "stato"):
                tg_send_photo(_do_status())
            elif text in ("/restart", "restart", "/riavvia", "riavvia"):
                tg_send("🔄 Riavvio in corso...")
                tg_send(_do_restart(arg))
            elif text in ("/hardrestart", "hardrestart", "/riavvia_tutto", "riavvia_tutto"):
                # Hard restart: kills THIS gui_server, relaunches via
                # scripts/restart.sh, navigates browser to studies list,
                # leaves the server empty (no agent).
                tg_send("🧨 Hard restart: uccido il server e lo rilancio.")
                tg_send(_do_hardrestart())
            elif text in ("/inject", "inject", "/istruzione", "istruzione", "/msg", "msg"):
                if not arg:
                    tg_send("Scrivi anche il testo:\n/inject <istruzione per l'agente>")
                else:
                    with state_lock:
                        running = state["running"]
                        pending_q = state.get("pending_question")
                        aq = state.get("answer_queue") if pending_q else None
                        if running and not pending_q:
                            state["inject_message"] = arg
                    if not running:
                        tg_send("ℹ️ L'agente non è in esecuzione.")
                    elif pending_q and aq is not None:
                        # The driver is blocked on answer_queue.get() — any
                        # inject_message would sit unread until the user also
                        # answered from the GUI. Route the text to the queue
                        # so Telegram can unstick a pending question too.
                        aq.put(arg)
                        tg_send(f"✉️ Risposta inviata all'agente:\n{arg}")
                    else:
                        tg_send(f"📨 Istruzione in coda, la riceve al prossimo passo:\n{arg}")
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
            elif not text.startswith("/"):
                # Plain text with no command. If the agent is waiting for an
                # answer, treat the message as the answer -- otherwise the
                # question could only be answered from the GUI.
                with state_lock:
                    pending_q = state.get("pending_question")
                    aq = state.get("answer_queue") if pending_q else None
                if pending_q and aq is not None:
                    aq.put(raw)
                    tg_send(f"✉️ Risposta inviata all'agente:\n{raw}")
                else:
                    tg_send("Non riconosco il comando. /help per la lista.")
            elif text in ("/help", "help", "/aiuto", "aiuto"):
                tg_send(
                    "🤖 Comandi disponibili:\n"
                    "/status — Stato + screenshot + studi recenti\n"
                    "/start [preset] — Avvia l'agente (preset indicato o il primo)\n"
                    "/stop — Ferma l'agente\n"
                    "/restart — Ferma, torna a studies, riavvia (soft)\n"
                    "/inject <testo> — Istruzione extra per la run in corso\n"
                    "/presets — Elenco dei preset\n"
                    "/earnings — Guadagni: oggi, settimana, mese, totale\n"
                    "/riepilogo — Riepilogo di oggi (arriva da solo alle 21)\n"
                    "\n⚙️ Velocità:\n"
                    "/normal — Qualità massima (default)\n"
                    "/hurry — Veloce (immagini 75%, token ridotti)\n"
                    "/superfast — Super-veloce (img 50%, JPEG, contesto ridotto)\n"
                    "\n⚠️ /hardrestart — Riavvia TUTTO il server (distruttivo)\n"
                    "\n/help — Questa lista"
                )


def _submissions_auto_updater():
    """Background thread: refreshes the submissions cache right after each
    agent cycle ends (triggered by the 'done' event) or on a 5-minute
    fallback timer. Only runs the tab fetch during the inter-cycle idle
    window (phase=None) so the agent is never interrupted mid-cycle."""
    if TEST_INSTANCE:
        return
    # Serve the stored history at once: a restart used to leave /status and
    # /earnings on "nessuna submission" until the first idle window.
    try:
        _store = _subs_store_load()
        if _store["rows"]:
            with _submissions_lock:
                _submissions_cache["data"] = _subs_rows(_store)
                _submissions_cache["header"] = _store.get("header") or {}
                _submissions_cache["complete"] = _subs_complete(_store)
                _submissions_cache["ts"] = _store.get("ts") or 0.0
    except Exception:
        pass
    time.sleep(20)  # let the browser settle after server start
    _submissions_trigger.set()   # fetch once on startup if agent is idle
    while True:
        # Wait for a "done" signal, or wake on a timer: every minute while the
        # history is still being read in, every five once it is complete.
        _submissions_trigger.wait(
            timeout=300 if _submissions_cache.get("complete") else 60)
        _submissions_trigger.clear()
        try:
            # Skip if the cache is fresh -- unless history is still missing,
            # in which case every idle window is wanted until the store
            # matches the platform's own SUMMARY.
            if (time.time() - _submissions_cache["ts"] < 60
                    and _submissions_cache.get("complete")):
                continue

            with state_lock:
                running = state["running"]
                phase   = state.get("phase")

            # Safe: agent stopped, OR between cycles (phase=None after "done")
            if not running or (running and phase is None):
                # _submissions_refresh takes the cache lock itself, once it
                # has the data: holding it across a multi-page crawl would
                # block /status and the dashboard for the whole crawl.
                _submissions_refresh()
        except Exception:
            pass


_PORT = int(os.environ.get("FARA_PORT", "8766"))


def main():
    global _PORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=_PORT)
    args = parser.parse_args()
    _PORT = args.port
    threading.Thread(target=_telegram_bot_loop, daemon=True, name="tg-bot").start()
    threading.Thread(target=_submissions_auto_updater, daemon=True, name="subs-updater").start()
    threading.Thread(target=_daily_summary_loop, daemon=True, name="daily-summary").start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"GUI on http://127.0.0.1:{args.port} (loopback only)")
    server.serve_forever()


if __name__ == "__main__":
    main()
