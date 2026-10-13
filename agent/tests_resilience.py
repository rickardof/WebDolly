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
            scale = ac.SPEED_SCALE[mode]
            rh, rw = fp.smart_resize(int(900 * scale), int(1440 * scale))
            url = ac.encode_for_model(img)
            px[mode] = rw * rh
            want_mime = "data:image/jpeg;base64," if mode == "super_fast" else "data:image/png;base64,"
            record(f"encode_for_model[{mode}] {rw}x{rh} {want_mime[5:-8]}",
                   url.startswith(want_mime), f"{len(url)//1024} KB")
        record("faster modes send fewer pixels (= fewer image tokens)",
               px["super_fast"] < px["hurry"] < px["normal"],
               " ".join(f"{k}={v/1e6:.2f}MP" for k, v in px.items()))

        # Resampled once, from the full-res screenshot. Scaling down and then
        # letting smart_resize scale part of the way back up blurs the text for
        # nothing and cost 411 KB against 376 KB on a real screenshot.
        ac.set_speed("hurry")
        rh, rw = fp.smart_resize(int(900 * 0.75), int(1440 * 0.75))
        buf = _io.BytesIO()
        img.resize((rw, rh), Image.LANCZOS).save(buf, format="PNG")
        want = "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()
        record("hurry resamples once, straight from the full-res shot",
               ac.encode_for_model(img) == want)
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
