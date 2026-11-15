#!/usr/bin/env python3
"""Suite for the guardrails added after the first Prolific nights: speed
modes, the study timer's veto on `terminate`, the click fallbacks, the
study cache, audio/video screening, log pruning and the retry backoff.

Two halves:

  OFFLINE   pure logic -- no browser, no network. pw_bridge is replaced by a
            stub that raises, so a test that accidentally reaches for the
            browser fails loudly instead of touching the live session.
  DOM       needs a browser on FARA_CDP_URL. It navigates and clicks, so it
            REFUSES to run while a real agent run is in progress; point it
            at the isolated stack (fara-browser-test + port 8767) as the
            README describes, or pass --force if you know what you are doing.

    ../venv_pw/bin/python tests_resilience.py [--offline] [--force]
"""
import argparse
import gzip
import threading as _th_mod
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import agent_core as ac
import study_cache

results = []


def record(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name:52s} {detail}")


class _NoBrowser:
    """Stand-in for pw_bridge: any browser call during the offline half is a bug."""
    def __getattr__(self, _name):
        def boom(*_a, **_kw):
            raise AssertionError("offline test reached for the browser")
        return boom


# ======================= OFFLINE =======================================
def offline():
    real_bridge, ac.pw_bridge = ac.pw_bridge, _NoBrowser()
    try:
        # --- speed modes -------------------------------------------------
        ac.set_speed("normal")
        record("set_speed rejects an unknown mode", ac.set_speed("ludicrous") is False)
        record("unknown mode leaves the state alone", ac.speed_state()["mode"] == "normal")

        ac.set_speed("hurry")
        ac._auto_escalate("super_fast")
        st = ac.speed_state()
        record("timer can escalate above the user's choice",
               st["mode"] == "super_fast" and st["auto"] and st["user"] == "hurry", str(st))
        ac._auto_escalate("normal")
        record("escalation never downgrades", ac.speed_state()["mode"] == "super_fast")
        ac._restore_user_speed()
        st = ac.speed_state()
        record("end of study restores the user's choice",
               st["mode"] == "hurry" and not st["auto"], str(st))
        ac.set_speed("super_fast")
        ac._restore_user_speed()
        record("restore leaves an explicit choice untouched",
               ac.speed_state()["mode"] == "super_fast")

        # --- screenshot encoding per mode --------------------------------
        # What a faster mode has to buy is fewer image tokens, i.e. fewer
        # pixels; the byte size of the upload is not a sane invariant (PNG of a
        # flat UI gets *bigger* when downscaled, antialiasing being what it
        # compresses worst), so the sizes are printed, not asserted.
        import base64 as _b64, io as _io
        from PIL import Image, ImageDraw
        import fara_prompt as fp
        img = Image.new("RGB", (1440, 900), "#f4f5f7")
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 1440, 60], fill="#1b1f2a")
        for i in range(9):                              # rows of text-like runs
            y = 90 + i * 85
            d.rectangle([40, y, 1400, y + 70], fill="white", outline="#d8dbe0")
            for j in range(26):
                d.rectangle([60 + j * 18, y + 20, 71 + j * 18, y + 32], fill="#2b3040")
        px = {}
        for mode in ("normal", "hurry", "super_fast"):
            ac.set_speed(mode)
            scale = ac.HISTORY_SCALE[mode]
            rh, rw = fp.smart_resize(int(900 * scale), int(1440 * scale))
            url = ac.encode_for_history(img)
            px[mode] = rw * rh
            want_mime = ("data:image/png;base64," if scale == 1.0
                         else "data:image/jpeg;base64,")
            record(f"encode_for_history[{mode}] {rw}x{rh} {want_mime[5:-8]}",
                   url.startswith(want_mime), f"{len(url)//1024} KB")
        record("faster modes keep fewer pixels in history (= fewer image tokens)",
               px["super_fast"] < px["hurry"] < px["normal"],
               " ".join(f"{k}={v/1e6:.2f}MP" for k, v in px.items()))

        # The screenshot the model is about to click on is never degraded,
        # whatever the mode: only the older ones kept in context are. This
        # block used to read a SPEED_SCALE table that no longer exists, so the
        # whole offline suite died here -- and with it the dead-end and
        # study-cache checks further down, which is how a description could
        # start reading as a screen-out unnoticed.
        for mode in ("normal", "hurry", "super_fast"):
            ac.set_speed(mode)
            record(f"encode_for_model[{mode}] stays full-res PNG",
                   ac.encode_for_model(img).startswith("data:image/png;base64,"))
        ac.set_speed("normal")

        # --- study timer vetoes an early terminate -----------------------
        clock = [1000.0]
        t = ac.StudyTimer(clock=lambda: clock[0])
        record("idle timer never refuses terminate", ac.terminate_refusal(t) == "")
        t.start(600)                                    # 10-minute study
        record("terminate refused at the start", "TERMINATE REFUSED" in ac.terminate_refusal(t))
        clock[0] += 300
        record("still refused halfway", "TERMINATE REFUSED" in ac.terminate_refusal(t))
        record("status line tells the model to wait", "Do NOT submit yet" in (t.status_line() or ""))
        clock[0] += 275                                 # 25 s left: inside the grace window
        record("allowed inside the last 30 s", ac.terminate_refusal(t) == "",
               f"remaining={t.remaining():.0f}s")
        clock[0] += 60
        record("allowed past the minimum", ac.terminate_refusal(t) == "")
        t.reset()
        record("reset disarms the timer", not t.is_active() and ac.terminate_refusal(t) == "")

        # --- NOCODE guard: refuse a premature terminate on Prolific ----
        prol = "https://app.prolific.com/studies/abc123"
        host = "https://survey.example.com/page"
        # Non-NOCODE answers never trigger the guard.
        record("CODE answer never blocked",
               ac.guarded_nocode_refusal("CODE:XYZ", prol, False, False, 0) == "")
        # NOCODE off Prolific never blocked (model is still on the survey host).
        record("NOCODE off Prolific never blocked",
               ac.guarded_nocode_refusal("NOCODE", host, False, False, 0) == "")
        # The bug reproduction: on Prolific, never saw external, no cancel confirm.
        refusal = ac.guarded_nocode_refusal("NOCODE", prol, False, False, 0)
        record("NOCODE on Prolific without external/cancel is refused",
               "NOCODE REFUSED" in refusal and "Cancel participation" in refusal)
        # Legit case A: external survey was visited (completed, no code shown).
        record("NOCODE after external survey visited passes",
               ac.guarded_nocode_refusal("NOCODE", prol, True, False, 0) == "")
        # Legit case B: Prolific-side cancel confirmation seen.
        record("NOCODE after cancel confirmation passes",
               ac.guarded_nocode_refusal("NOCODE", prol, False, True, 0) == "")
        # Safety valve: 3rd attempt always passes so a stale label can't wedge the loop.
        record("safety valve lets 3rd NOCODE attempt through",
               ac.guarded_nocode_refusal("NOCODE", prol, False, False, 2) == "")
        record("2nd NOCODE attempt still refused",
               "NOCODE REFUSED" in ac.guarded_nocode_refusal("NOCODE", prol, False, False, 1))
        # Confirmation detector: multilingual phrases.
        for text, want in [
            ("You have cancelled your participation in this study.", True),
            ("La tua partecipazione è stata annullata.", True),
            ("Your submission has been received", True),
            ("Thanks for taking part!", True),
            ("Study submitted without a completion code", True),
            ("I'm finished", False),
            ("Take part in this study", False),
            ("", False),
        ]:
            got = ac.saw_prolific_submission_confirmation(text)
            record(f"submission confirmation: {text[:34]!r}", got == want, f"got={got!r}")

        # --- dead-end / audio-video detection ----------------------------
        for text, expect in [
            ("Sorry, you are not eligible to take part in this study.", "non idoneo"),
            ("This study is no longer available.", "non più disponibile"),
            ("All places have been taken", "non più disponibile"),
            ("You have already completed this study", "già svolto"),
            ("You were screened out of this study", "screened out"),
            ("Answer these 20 questions about your shopping habits.", ""),
            ("Are you eligible for a pension? Yes/No", ""),
        ]:
            got = ac._detect_dead_end(text)
            record(f"dead end: {text[:34]!r}", got == expect, f"got={got!r}")

        for text, expect_hit in [
            ("You will need headphones for this task.", True),
            ("Please watch the short video and answer.", True),
            ("Listen to the audio recording carefully.", True),
            ("We will record your face via webcam.", True),
            ("Guarda il video e rispondi alle domande.", True),
            ("How many hours of video do you watch per week?", False),
            ("Rate the following statements about audio equipment brands.", False),
        ]:
            got = ac._detect_av(text, check_text=True)
            record(f"audio/video: {text[:34]!r}", bool(got) == expect_hit, f"got={got!r}")
        record("media check alone needs the DOM (no false positive from text)",
               ac._detect_av("you will need headphones", check_text=False) == "")

        # --- study cache --------------------------------------------------
        with tempfile.TemporaryDirectory() as td:
            study_cache.CACHE_FILE = Path(td) / "study_cache.json"
            sid = "a" * 24
            record("mark returns True the first time",
                   study_cache.mark(sid, "non idoneo", title="Coffee habits") is True)
            record("mark returns False on a repeat",
                   study_cache.mark(sid, "non idoneo", title="Coffee habits") is False)
            record("the study is remembered", study_cache.is_marked(sid))
            record("a repeat bumps the hit counter",
                   study_cache.entries()[sid]["hits"] == 2)
            study_cache.mark("b" * 24, "audio/video: cuffie richieste", title="Audio task")
            line = study_cache.prompt_line()
            record("prompt line names both studies and reasons",
                   "Coffee habits" in line and "Audio task" in line and "cuffie" in line)
            record("titles are listed once each", study_cache.titles() == ["Audio task", "Coffee habits"]
                   or sorted(study_cache.titles()) == ["Audio task", "Coffee habits"],
                   str(study_cache.titles()))
            study_cache.mark("c" * 24, "scartato")      # no title: nothing to tell the model
            record("an untitled entry stays out of the prompt",
                   "c" * 24 not in study_cache.prompt_line())
            record("forget removes one entry", study_cache.forget(sid) and not study_cache.is_marked(sid))
            record("forget on a missing key is a no-op", study_cache.forget("zzz") is False)

            # expiry: rewrite one entry as older than the TTL
            data = json.loads(study_cache.CACHE_FILE.read_text())
            key = next(iter(data))
            data[key]["ts"] = time.time() - study_cache.TTL_SECONDS - 10
            study_cache.CACHE_FILE.write_text(json.dumps(data))
            record("expired entries disappear", key not in study_cache.entries())
            record("clear empties the file", study_cache.clear() >= 0 and study_cache.entries() == {})

            study_cache.CACHE_FILE.write_text("{ not json")
            record("a corrupt cache file never breaks a run", study_cache.entries() == {})

        # --- log pruning keeps the transcripts ----------------------------
        import gui_server as gs
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real_root, ac.LOG_ROOT = ac.LOG_ROOT, root
            try:
                for i in range(1, 5):
                    d = root / f"run_2026101{i}_120000"
                    d.mkdir()
                    (d / "transcript.jsonl").write_text(json.dumps({"step": 1, "run": i}) + "\n")
                    (d / "screenshot_01.png").write_bytes(b"\x89PNG fake")
                ac._prune_old_runs(keep=2)
                old, new = root / "run_20261011_120000", root / "run_20261014_120000"
                record("old screenshots are deleted", not list(old.glob("screenshot_*.png")))
                record("old transcript is kept, gzipped",
                       (old / "transcript.jsonl.gz").exists() and not (old / "transcript.jsonl").exists())
                record("recent run is left alone",
                       (new / "transcript.jsonl").exists() and (new / "screenshot_01.png").exists())
                with gzip.open(old / "transcript.jsonl.gz", "rt") as f:
                    record("gzipped transcript is still readable", json.loads(f.read())["run"] == 1)
                files = gs._transcript_files()
                record("the dashboard still sees every run", len(files) == 4, f"{len(files)} file")
                record("archived transcripts read back",
                       all(gs._read_transcript(f).strip() for f in files))
                ac._prune_old_runs(keep=2)
                record("pruning twice is harmless", (old / "transcript.jsonl.gz").exists())
            finally:
                ac.LOG_ROOT = real_root

        # --- recovery from a wedged tab ------------------------------------
        import pw_bridge as pb                      # importing never connects
        record("an unreadable URL has its own error type",
               issubclass(ac.BrowserStateError, RuntimeError))
        record("drop_connection is safe before the bridge exists",
               pb.drop_connection() is None and pb._bridge is None)
        real_cdp, pb.CDP_URL = pb.CDP_URL, "http://127.0.0.1:9"   # discard port
        try:
            record("recycle_tab reports failure instead of raising",
                   pb.recycle_tab("https://example.invalid/") is False)
        finally:
            pb.CDP_URL = real_cdp

        # --- retry backoff -------------------------------------------------
        seq = [gs._backoff(n) for n in range(1, 7)]
        record("backoff grows then flattens at 5 min", seq == [30, 60, 120, 300, 300, 300], str(seq))
        record("backoff is defined for a nonsense count", gs._backoff(0) == 30 and gs._backoff(-3) == 30)

        # --- completion code: Prolific redirect vs the study's own page ----
        for url, want in [
            ("https://app.prolific.com/submissions/complete?cc=C1A2B3", "C1A2B3"),
            ("https://app.prolific.co/submissions/complete/?cc=Cover_F8033DF8&x=1", "Cover_F8033DF8"),
            ("https://app.prolific.com/submissions/complete", ""),      # no code: no opinion
            ("https://app.prolific.com/submissions", ""),
            ("https://survey.example.com/end?cc=FAKE1", ""),          # only Prolific's redirect counts
            ("https://app.prolific.com/submissions/complete?cc=<x>", ""),
        ]:
            record(f"code from URL: {url[8:60]!r}", ac.completion_code(url) == want,
                   f"got={ac.completion_code(url)!r}")
        for text, want in [
            ("Thank you! Your completion code is: C1A2B3X9. Copy it into Prolific.", ["C1A2B3X9"]),
            ("Prolific code: Cover_F8033DF8", ["Cover_F8033DF8"]),
            ("Your survey completion code\n\nCX7Y2Z", ["CX7Y2Z"]),
            ("Your completion code will be shown on the next page.", []),
            ("Thank you for taking part in this study.", []),
        ]:
            record(f"code on the study's page: {text[:30]!r}", ac.codes_on_page(text) == want,
                   f"got={ac.codes_on_page(text)}")

        for url, want in [("https://app.booktest.io/thanks?code=PLKXLQ&lang=it", ["PLKXLQ"]),
                          ("https://x.example/end?completion_code=C9X", ["C9X"]),
                          ("https://x.example/?page=2&id=55", [])]:
            record(f"code in the study's URL: {url[8:45]!r}", ac.codes_in_url(url) == want)
        for ans, seen, want in [("CODE: PLKXLQ", ["PLKXLQ"], "match"),
                                ("CODE: PLKXLO", ["PLKXLQ"], "mismatch"),
                                ("NOCODE", ["PLKXLQ"], "missed"),
                                ("NOCODE", [], "not_found"),
                                ("CODE: X1", [], "not_found")]:
            record(f"code check {ans} vs {seen} -> {want}", ac.check_code(ans, seen) == want)

        # --- list watching: only a clear "empty" may skip the model -------
        L = "https://app.prolific.com/studies"
        for url, text, want in [
            (L, "Studies ... Waiting for studies ... Note: you do not need to refresh", "empty"),
            (L, "Studies  Shopping survey  £3.00  5 mins  Take part", "studies"),
            (L, "", "unknown"),                                   # page not readable
            ("https://app.prolific.com/studies/" + "a" * 24, "Waiting for studies", "unknown"),
            ("https://app.prolific.com/auth/login", "Log in", "unknown"),
        ]:
            record(f"list state {url[8:40]}.. -> {want}", ac.list_state_from(url, text) == want)

        # --- model speed watch --------------------------------------------
        alerts = []
        saved_n, saved_live = ac._notify_telegram, ac._live
        ac._notify_telegram, ac._live = (lambda m: alerts.append(m)), (lambda m: None)
        ac._gen_tps.clear(); ac._last_slow_alert = 0.0
        try:
            for _ in range(10):
                ac._record_timing({"predicted_n": 5, "predicted_per_second": 1.0})
            record("very short answers never count as slow", not alerts and not ac._gen_tps)
            for _ in range(ac.SLOW_WINDOW):
                ac._record_timing({"predicted_n": 100, "predicted_per_second": 33.0, "prompt_n": 1400})
            record("normal speed raises nothing", not alerts and ac.model_speed()["median_gen_tps"] == 33.0)
            for _ in range(ac.SLOW_WINDOW):
                ac._record_timing({"predicted_n": 100, "predicted_per_second": 2.4})
            record("a sustained collapse is reported on Telegram", len(alerts) == 1, str(alerts)[:60])
            for _ in range(ac.SLOW_WINDOW):
                ac._record_timing({"predicted_n": 100, "predicted_per_second": 2.4})
            record("...once per hour, not at every step", len(alerts) == 1)
        finally:
            ac._notify_telegram, ac._live = saved_n, saved_live
            ac._gen_tps.clear(); ac._last_slow_alert = 0.0

        # --- per-run summary ----------------------------------------------
        sid = "b" * 24
        lines = [json.dumps(x) for x in [
            {"type": "diag", "step": 1, "url": L, "guarded": True, "timer_active": False},
            {"type": "diag", "step": 2, "url": f"{L}/{sid}", "guarded": True, "timer_active": False},
            {"type": "diag", "step": 3, "url": "https://q.example.com/s", "guarded": False,
             "timer_active": True, "model": {"gen_tps": 30.0}},
            {"type": "diag", "step": 4, "url": "https://q.example.com/s", "guarded": False,
             "timer_active": True, "model": {"gen_tps": 32.0}},
            {"step": 5, "action_type": "terminate", "args": {"answer": "CODE: C1A2"},
             "code_source": "url"},
        ]]
        s = ac.summarize_transcript(lines)
        record("summary: outcome and code source", s["outcome"] == "CODE" and s["code_source"] == "url")
        record("summary: where the steps went",
               s["steps_where"] == {"list": 1, "study": 2, "elsewhere": 1}, str(s["steps_where"]))
        record("summary: the study id is kept", s["studies"] == [sid])
        record("summary: an unfinished run reads as INCOMPLETE",
               ac.summarize_transcript(lines[:3])["outcome"] == "INCOMPLETE")

        # --- Telegram presets ---------------------------------------------
        ps = [{"name": "Survey research (QA)"}, {"name": "Survey lunghe"}, {"name": "Test"}]
        record("/start with no name takes the first preset", gs._pick_preset(ps)[0] is ps[0])
        record("/start matches the exact name in any case", gs._pick_preset(ps, "test")[0] is ps[2])
        record("/start takes a unique partial match", gs._pick_preset(ps, "lungh")[0] is ps[1])
        p_, err = gs._pick_preset(ps, "survey")
        record("/start refuses an ambiguous name and lists the options",
               p_ is None and "Survey lunghe" in err)
        record("/start with no presets says so", gs._pick_preset([], "x")[0] is None)
        record("the daily summary builds", "Riepilogo" in gs._daily_summary_text())

        # --- the wait between cycles ---------------------------------------
        seen = iter(["empty", "empty", "studies"])
        saved = (gs.LIST_CHECK_MIN_SECONDS, gs._refresh_list, ac.studies_list_state, ac.capture_page)
        gs.LIST_CHECK_MIN_SECONDS, gs._refresh_list = 0, (lambda url: None)
        ac.studies_list_state, ac.capture_page = (lambda: next(seen)), (lambda kind: None)
        try:
            stop2 = _th_mod.Event()
            t0 = time.time()
            stopped = gs._wait_for_studies("u", 0, stop2)
            record("an empty list is re-checked without the model until studies appear",
                   stopped is False and next(seen, "done") == "done")
            stop2.set()
            ac.studies_list_state = lambda: "empty"
            record("Stop during the wait ends it at once",
                   gs._wait_for_studies("u", 0, stop2) is True)
        finally:
            (gs.LIST_CHECK_MIN_SECONDS, gs._refresh_list, ac.studies_list_state,
             ac.capture_page) = saved

        # --- the recovery path itself, end to end --------------------------
        # A cycle that raises must be reported and retried, not end the run.
        # The first version crashed right here (UnboundLocalError on
        # `traceback`, shadowed by an import further down the function) and
        # only the unit test of _backoff existed, so nothing noticed.
        import queue as _q, threading as _th

        class _Bridge:
            def reset_to(self, url): calls.append(("reset_to", url))
            def wait_for_content(self): pass
            def recycle_tab(self, url): calls.append(("recycle", url)); return True

        calls, cycles, notified = [], [], []
        stop = _th.Event()

        def fake_cycle(*a, **kw):
            cycles.append(1)
            if len(cycles) == 1:
                raise ValueError("No <tool_call> block in model output")
            stop.set()
            return True, None

        saved = (gs.run_one_cycle, gs._backoff, gs.get_current_url, gs.pw_bridge,
                 gs._inhibit_sleep, ac._notify_telegram)
        gs.run_one_cycle, gs._backoff = fake_cycle, lambda n: 0
        gs.get_current_url = lambda: "https://app.prolific.com/studies"
        gs.pw_bridge, gs._inhibit_sleep = _Bridge(), (lambda: None)
        ac._notify_telegram = lambda m: notified.append(m)
        with gs.state_lock:
            gs.state.update(stop_event=stop, answer_queue=_q.Queue(), running=True,
                            events=[], seq=0,
                            stats={"cycle": 0, "steps": 0, "batch_fills": 0, "errors": 0})
        try:
            gs.agent_driver("task", "", 5, True, 0.01)
        finally:
            (gs.run_one_cycle, gs._backoff, gs.get_current_url, gs.pw_bridge,
             gs._inhibit_sleep, ac._notify_telegram) = saved
        errs = [e.get("message", "") for e in gs.state["events"] if e.get("type") == "error"]
        record("a failed cycle is retried instead of ending the run", len(cycles) == 2,
               f"cicli={len(cycles)}")
        record("the failure is reported once, with the retry delay",
               len(errs) == 1 and "riprovo" in errs[0], str(errs)[:90])
        record("nothing crashes inside the recovery itself",
               not any("traceback" in e or "local variable" in e for e in errs))
        record("the browser is put back on a known page before retrying",
               calls and calls[0][0] == "reset_to", str(calls))
        record("Telegram gets the failed cycle and the retry delay",
               any("ValueError" in m and "Riprovo tra" in m for m in notified), str(notified)[:80])
    finally:
        ac.pw_bridge = real_bridge


# ========================== DOM ========================================
FIXTURES = {
    "test_disabled_submit.html": """<!doctype html><meta charset=utf-8><body style="font:16px sans-serif">
<h1>Checkout</h1>
<label><input type=checkbox id=agree> I agree</label>
<p><button id=dis style="position:absolute;left:100px;top:200px;width:160px;height:40px" disabled>Submit (disabled)</button>
<button id=en style="position:absolute;left:100px;top:300px;width:160px;height:40px">Submit (enabled)</button>
<a id=aria href="#" role=button aria-disabled="true" style="position:absolute;left:100px;top:400px">Next</a>
</body>""",
    "test_checkbox_far.html": """<!doctype html><meta charset=utf-8><body style="font:16px sans-serif">
<h1>Consent</h1>
<label style="position:absolute;left:200px;top:300px"><input type=checkbox id=cb> I consent</label>
</body>""",
    "test_two_cards.html": """<!doctype html><meta charset=utf-8><body style="font:16px sans-serif">
<div style="position:absolute;left:0;top:100px;width:600px;height:120px">
  <h3>Shopping survey</h3><span>Reward: 3.00</span><span> 5 minutes</span>
  <button id=a style="position:absolute;left:400px;top:60px">Open A</button></div>
<div style="position:absolute;left:0;top:300px;width:600px;height:120px">
  <h3>Memory task</h3><span>Reward: 9.00</span><span> 23 minutes</span>
  <button id=b style="position:absolute;left:400px;top:60px">Open B</button></div>
<button id=far style="position:absolute;left:20px;top:700px">Elsewhere</button>
</body>""",
    "test_media.html": """<!doctype html><meta charset=utf-8><body style="font:16px sans-serif">
<h1>Listening task</h1><video id=v src="data:video/mp4;base64,AAAA" controls width=320></video></body>""",
    "test_code_end.html": """<!doctype html><meta charset=utf-8><body style="font:16px sans-serif">
<h1>Thank you!</h1><label for=c>Your Prolific code</label> <input id=c readonly value="ZX81AB">
<iframe srcdoc="<p>Completion code: IFR4ME</p>" width=300 height=60></iframe></body>""",
    "test_nomedia.html": """<!doctype html><meta charset=utf-8><body style="font:16px sans-serif">
<h1>Plain questionnaire</h1><p>Twelve questions about your week.</p></body>""",
}


def _gui_busy(port: int):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/agent/status", timeout=3) as r:
            d = json.load(r)
        return bool(d.get("running")), d
    except Exception:
        return False, None


def dom(force: bool, port: int):
    busy, status = _gui_busy(port)
    if busy and not force:
        print(f"\nSKIP  i test DOM: una run è in corso su :{port} "
              f"(ciclo {(status or {}).get('stats', {}).get('cycle')}).\n"
              "      Usa lo stack isolato (fara-browser-test, porta 8767) oppure --force.")
        return
    import pw_bridge as pb
    for name, html in FIXTURES.items():
        (ac.DATA_DIR / name).write_text(html)

    def goto(name):
        pb.goto(f"file:///data/{name}")
        pb.wait_for_content()
        time.sleep(0.3)

    def box(sel):
        return pb.run(lambda b: b.active_page().evaluate(
            "s => { const r = document.querySelector(s).getBoundingClientRect();"
            "return [r.left + r.width/2, r.top + r.height/2]; }", sel))

    # 1. disabled button detection
    goto("test_disabled_submit.html")
    x, y = box("#dis")
    record("disabled button is detected", ac._check_disabled_near(x, y) is True)
    x, y = box("#en")
    record("enabled button is not flagged", ac._check_disabled_near(x, y) is False)
    x, y = box("#aria")
    record("aria-disabled is detected", ac._check_disabled_near(x, y) is True)

    # 2. checkbox click fallback
    goto("test_checkbox_far.html")
    x, y = box("#cb")
    before = ac._near_checkbox_state(x + 40, y + 10)      # a near miss, inside the 80px radius
    record("nearest checkbox state is read", before is False, f"state={before}")
    ac._js_click_near_checkbox(x + 40, y + 10)
    record("missed click is recovered via JS",
           ac._near_checkbox_state(x, y) is True)
    record("no checkbox nearby reads as None", ac._near_checkbox_state(5, 5) is None)

    # 3. study duration comes from the card that was clicked
    goto("test_two_cards.html")
    xa, ya = box("#a")
    xb, yb = box("#b")
    da, db = ac._extract_study_duration(xa, ya), ac._extract_study_duration(xb, yb)
    record("duration read from card A", da == 5.0, f"got={da}")
    record("duration read from card B", db == 23.0, f"got={db}")
    xf, yf = box("#far")
    record("click far from any card falls back to the page",
           ac._extract_study_duration(xf, yf) in (5.0, 23.0), f"got={ac._extract_study_duration(xf, yf)}")

    # 4. media elements on the page
    goto("test_media.html")
    record("a <video> element is detected", "video" in (ac._detect_av(check_text=False) or ""))
    record("page title is readable for the cache", ac._study_title() == "Listening task",
           ac._study_title())
    goto("test_nomedia.html")
    record("a plain page is not flagged", ac._detect_av(check_text=False) == "")

    # 4b. codes on a custom end page: in a copy box and inside an iframe
    goto("test_code_end.html")
    time.sleep(0.5)
    found = ac.codes_on_page(ac._end_page_text())
    record("code in a read-only field is found", "ZX81AB" in found, str(found))
    record("code inside an iframe is found", "IFR4ME" in found, str(found))

    # 5. extra tabs are closed after a study
    pb.run(lambda b: [b._active.context.new_page().goto("about:blank") for _ in range(2)])
    n_before = pb.run(lambda b: len(b._active.context.pages))
    ac._close_extra_tabs()
    n_after = pb.run(lambda b: len(b._active.context.pages))
    record("extra tabs are closed", n_after == 1, f"{n_before} -> {n_after}")

    for name in FIXTURES:
        try:
            (ac.DATA_DIR / name).unlink()
        except OSError:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="solo i test senza browser")
    ap.add_argument("--force", action="store_true", help="esegui i test DOM anche con una run in corso")
    ap.add_argument("--port", type=int, default=8766, help="porta del gui_server da interrogare")
    a = ap.parse_args()

    offline()
    if not a.offline:
        dom(a.force, a.port)

    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
