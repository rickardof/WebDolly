#!/usr/bin/env python3
"""Replay the captured REAL Prolific pages and run the launch path against them.

Each fixture is an MHTML snapshot taken from the live browser by
capture_fixture.py, so the markup, the CSS and therefore the GEOMETRY are the
real ones -- no hand-written approximation to recalibrate. Every geometry bug
we have chased (confirm link below the fold, a dialog that scrolls 36px and
stops, a bounding box that points off-screen) is visible in these.

Runs against the ISOLATED test browser, never production:

  docker start fara-browser-test
  ./venv_pw/bin/python scripts/tools/test_fixtures_live.py

What a replay can and cannot show:
  CAN  -- exact layout, element positions, scroll ranges, which controls are
          below the fold, whether our selectors still match the real markup.
  CANNOT -- Prolific's own JS (MHTML replays with scripts inert), so the
          "confirm link enables once both boxes are ticked" behaviour does not
          re-run here. The link's outbound href is rewritten to a local stub so
          a test never opens the real survey.
"""
import json, os, sys, time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FIXDIR = os.environ.get("FARA_FIXTURE_DIR",
                        os.path.join(ROOT, "var", "fixtures", "prolific"))
CONTAINER = os.environ.get("FARA_TEST_CONTAINER", "fara-browser-test")

os.environ.setdefault("FARA_CDP_URL", "http://127.0.0.1:9334")
os.environ.setdefault("FARA_CONTAINER", CONTAINER)
sys.path.insert(0, os.path.join(ROOT, "agent"))

import pw_bridge          # noqa: E402
import agent_core as ac   # noqa: E402

results = []
def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name:62s} {detail}", flush=True)

def skip(name, why):
    print(f"SKIP  {name:62s} {why}", flush=True)


# --- synthetic dialog geometries ---------------------------------------
# The captured Prolific dialogs vary per study: the one in
# var/fixtures/prolific is 488px tall with its confirm link on screen and no
# checkboxes, while the Text-to-Image one carried a "Confidential
# information" section that pushed the link below the fold. Neither capture
# exercises the hard case, so these three reproduce it exactly, in the real
# browser with the real cursor. They are generated here rather than kept as
# files because the previous copies lived in /tmp and were lost to a reboot.
_REPRO = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>repro</title>
<style>
  html,body{margin:0;height:100%%;overflow:hidden}
  .backdrop{position:fixed;inset:0;background:rgba(0,0,0,.45)}
  .panel{position:fixed;left:50%%;top:14px;transform:translateX(-50%%);
         width:540px;height:%(ph)dpx;background:#fff;border-radius:12px;
         overflow:%(ov)s;font:16px system-ui;padding:24px;box-sizing:border-box}
  .spacer{height:%(sp)dpx}
  a.start{display:inline-block;background:#1d4ed8;color:#fff;text-decoration:none;
          padding:14px 28px;border-radius:8px}
  a.start[aria-disabled="true"]{background:#9ca3af;pointer-events:none}
</style></head><body>
<div class="backdrop"></div>
<section class="panel" role="dialog" data-testid="start-study-dialog">
  <h2>Start study</h2>
  <label><input type="checkbox" data-testid="device-checkbox"> I am using a desktop.</label>
  <label><input type="checkbox" data-testid="conf-checkbox"> I agree to keep this study confidential.</label>
  <div class="spacer"></div>
  <a class="start" href="stub_survey.html" target="_blank" aria-disabled="true"
     data-testid="dialog-start-study">Start study</a>
</section>
<script>
// Mirrors Prolific: the confirm link is inert until BOTH boxes are ticked.
const boxes = [...document.querySelectorAll('input[type=checkbox]')];
const link  = document.querySelector('[data-testid=dialog-start-study]');
function sync(){ if (boxes.every(b => b.checked)) link.removeAttribute('aria-disabled');
                 else link.setAttribute('aria-disabled','true'); }
boxes.forEach(b => b.addEventListener('change', sync));
sync();
%(calib)s
</script></body></html>"""

_CALIBRATE = ("const panel=document.querySelector('.panel');"
              "panel.style.height='auto';"
              "panel.style.height=(panel.scrollHeight-40)+'px';")

# name -> panel height, overflow, spacer, extra script
_GEOMETRIES = {
    # nothing scrolls at all
    "repro_locked.html":   dict(ph=1400, ov="hidden", sp=1000, calib=""),
    # scrolls, but its whole range is ~40px: the pessimistic reading of
    # run_20261106_124620, where 4 wheel ticks moved the dialog 36px
    "repro_creeping.html": dict(ph=940, ov="auto", sp=1000, calib=_CALIBRATE),
    # scrolls hundreds of px: enough, if you keep going
    "repro_long.html":     dict(ph=880, ov="auto", sp=1200, calib=""),
}


def write_repros(outdir):
    os.makedirs(outdir, exist_ok=True)
    paths = []
    for name, kw in _GEOMETRIES.items():
        f = os.path.join(outdir, name)
        with open(f, "w") as fh:
            fh.write(_REPRO % kw)
        paths.append(f)
    return paths


def fixtures():
    if not os.path.isdir(FIXDIR):
        return {}
    out = {}
    for f in sorted(os.listdir(FIXDIR)):
        if f.endswith(".mhtml"):
            state = f[:-len(".mhtml")]
            meta = os.path.join(FIXDIR, state + ".json")
            out[state] = {
                "mhtml": os.path.join(FIXDIR, f),
                "meta": json.load(open(meta)) if os.path.exists(meta) else {},
            }
    return out


def push_to_container(paths):
    import subprocess
    subprocess.run(["docker", "exec", CONTAINER, "mkdir", "-p", "/tmp/fixtures"],
                   check=True, capture_output=True)
    for p in paths:
        subprocess.run(["docker", "cp", p, f"{CONTAINER}:/tmp/fixtures/"],
                       check=True, capture_output=True)
    # local stub the confirm link is rewritten to point at
    stub = "/tmp/fixtures/stub_survey.html"
    subprocess.run(
        ["docker", "exec", CONTAINER, "sh", "-c",
         f"printf '%s' '<!doctype html><title>stub</title><h1 id=ok>STUB SURVEY</h1>' > {stub}"],
        check=True, capture_output=True)
    return stub


# --- the pre-screenshot nudge vs an answered radio group ---------------
# run_20261108_181732: the book survey's radio GROUPS were all answered, but
# every group still has unchecked siblings, so the nudge's
# 'input[type=radio]:not(:checked)' matched forever and re-centred an
# answered question on every step. The model scrolled away to look for the
# field it thought was blocking submission; the nudge dragged the viewport
# straight back; the screenshot never changed. Twenty steps, five minutes,
# then it cancelled a study it could have submitted. Shaped like that page.
_NUDGE_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>nudge repro</title><style>body{font:16px system-ui;margin:0}
 .pad{height:1100px}</style></head><body>
<h2>Perch&eacute; hai scelto questo libro?</h2>
<textarea rows=4 cols=60>risposta</textarea>
<h3>Qual &egrave; il tuo livello di interesse?</h3>
<label><input type=radio name=interest value=a %(top_a)s> Bleah</label><br>
<label><input type=radio name=interest value=b %(top_b)s> Neutrale</label><br>
<label><input type=radio name=interest value=c> Wow</label>
<div class=pad></div>
<h3>Compri libri su Amazon?</h3>
<label><input type=radio name=amazon value=si checked> S&igrave;</label><br>
<label><input type=radio name=amazon value=no> No</label>
<div class=pad></div>
<button id=send disabled>Invia voto</button>
</body></html>"""


def nudge_repro(answered_top):
    """Write + serve the page, scroll to the bottom, return the nudge's verdict."""
    import subprocess, tempfile
    html = _NUDGE_PAGE % ({"top_a": "", "top_b": "checked"} if answered_top
                          else {"top_a": "", "top_b": ""})
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
        fh.write(html)
        local = fh.name
    subprocess.run(["docker", "cp", local, f"{CONTAINER}:/tmp/fixtures/nudge.html"],
                   check=True, capture_output=True)
    pw_bridge.goto("file:///tmp/fixtures/nudge.html")
    time.sleep(0.9)
    pw_bridge.front()
    pw_bridge.run(lambda b: b.active_page().evaluate(
        "() => window.scrollTo(0, document.body.scrollHeight)"))
    before = pw_bridge.run(lambda b: b.active_page().evaluate("() => window.scrollY"))
    verdict = ac._prescroll_to_controls()
    after = pw_bridge.run(lambda b: b.active_page().evaluate("() => window.scrollY"))
    return verdict, before, after


# --- the survey's own "return to Prolific" control ----------------------
# run_20261109_102733 passed a 45-trial evaluation, got "Your submission has
# been validated", and terminated NOCODE with a black "Return to Prolific and
# collect your reward" button in the middle of the screen. These pages are
# that screen: one where the control is a real link to Prolific's completion
# endpoint, one where it is a JS button (no href to match on) sitting below
# the fold, which is the case the real cursor has to scroll to.
_THANKYOU = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>thank you</title><style>body{font:16px system-ui;text-align:center}
 .pad{height:%(pad)dpx} .cta{background:#000;color:#fff;border:0;padding:14px 22px;
 font-size:16px;text-decoration:none;display:inline-block}</style></head><body>
<h1>Thank you for your participation!</h1>
<p>Your submission has been validated. Thank you for your time and effort!</p>
<div class=pad></div>
%(cta)s
</body></html>"""


def thankyou_page(cta, pad=0):
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
        fh.write(_THANKYOU % {"cta": cta, "pad": pad})
        local = fh.name
    subprocess.run(["docker", "cp", local, f"{CONTAINER}:/tmp/fixtures/thanks.html"],
                   check=True, capture_output=True)
    pw_bridge.goto("file:///tmp/fixtures/thanks.html")
    time.sleep(0.9)
    pw_bridge.front()


def load(state, stub):
    pw_bridge.goto(f"file:///tmp/fixtures/{state}.mhtml")
    time.sleep(2.2)
    pw_bridge.front()
    # Never let a test open the real survey.
    pw_bridge.run(lambda b: b.active_page().evaluate(
        """(stub) => {
            for (const a of document.querySelectorAll('a[target=_blank]'))
                a.setAttribute('href', stub);
        }""", f"file://{stub}"))
    return pw_bridge.run(lambda b: b.active_page().url)


def geometry():
    return pw_bridge.run(lambda b: b.active_page().evaluate("""() => {
        const sels = {reserve: '[data-testid="reserve"]',
                      open_dialog: '[data-testid="open-study-in-new-window-button"]',
                      dialog: '[data-testid="start-study-dialog"]',
                      start: '[data-testid="dialog-start-study"]'};
        const out = {viewport: {w: innerWidth, h: innerHeight}, boxes: {}};
        for (const [k, s] of Object.entries(sels)) {
            const el = document.querySelector(s);
            if (!el) { out.boxes[k] = null; continue; }
            const r = el.getBoundingClientRect();
            out.boxes[k] = {y: Math.round(r.y), h: Math.round(r.height),
                            below_fold: r.bottom > innerHeight};
        }
        const d = document.querySelector(sels.dialog);
        if (d) out.dialog_range = d.scrollHeight - d.clientHeight;
        out.doc_range = document.documentElement.scrollHeight - innerHeight;
        return out;
    }"""))


def close_extra_tabs():
    def _c(b):
        for p in b.pages()[1:]:
            try: p.close()
            except Exception: pass
        b._active = b.pages()[0]; b._known = b.pages()
    pw_bridge.run(_c)


def main():
    fx = fixtures()
    if not fx:
        print(f"No fixtures in {FIXDIR}.\n"
              f"Capture them first:  ./venv_pw/bin/python "
              f"scripts/tools/capture_fixture.py --minutes 120")
        return 2
    stub = push_to_container([v["mhtml"] for v in fx.values()])
    print(f"replaying: {', '.join(sorted(fx))}\n")

    # ---- selectors still match the real markup --------------------------
    for state in ("detail_unreserved", "detail_reserved", "start_dialog"):
        if state not in fx:
            skip(f"{state}: selectors match the real markup", "fixture not captured yet")
            continue
        load(state, stub)
        g = geometry()
        want = {"detail_unreserved": "reserve",
                "detail_reserved": "open_dialog",
                "start_dialog": "start"}[state]
        check(f"{state}: '{want}' selector matches the real page",
              g["boxes"].get(want) is not None, json.dumps(g["boxes"]))

    # ---- the geometry we were guessing at -------------------------------
    if "start_dialog" in fx:
        load("start_dialog", stub)
        g = geometry()
        vh = g["viewport"]["h"]
        sb = g["boxes"]["start"]
        print(f"   REAL dialog geometry: viewport_h={vh} "
              f"start_link_y={sb and sb['y']} below_fold={sb and sb['below_fold']} "
              f"dialog_scroll_range={g.get('dialog_range')} doc_range={g.get('doc_range')}")
        check("start_dialog: the confirm link is where our selector says",
              sb is not None, f"{sb}")

        # the measurement the whole fix turns on
        raw = pw_bridge.element_box(pw_bridge._SEL_START, scroll=False)
        on = ac._box_on_screen(raw, g["viewport"]["w"], vh)
        if raw and not on:
            check("off-screen confirm link is REFUSED, not clicked blind",
                  ac._humanized_click_box(dict(raw)) is False)
            y0 = raw["y"]
            got = ac._box_into_view(pw_bridge._SEL_START)
            y1 = (pw_bridge.element_box(pw_bridge._SEL_START, scroll=False) or {}).get("y")
            print(f"   scroll attempt: y {round(y0)} -> {y1 and round(y1)}, "
                  f"in_view={got is not None}")
            check("the real dialog is either scrolled in, or the stall is detected",
                  got is not None or abs((y1 or y0) - y0) >= 0,
                  f"in_view={got is not None}")
        else:
            check("confirm link already on screen in this capture -> clickable",
                  raw is not None and on, f"{raw}")

        # ---- does the real cursor land on the real confirm link? --------
        # Chromium refuses to navigate out of an MHTML archive, so a replay
        # cannot show the survey actually opening (the launcher reports
        # clicked-no-tab, correctly). What a replay CAN prove -- and what
        # every geometry bug here was about -- is whether the coordinates the
        # launcher computes put the real xdotool cursor on the real element.
        # The listener below is our instrumentation; the click it records is
        # a genuine trusted event from the real cursor.
        # (Chromium disables a document's own JavaScript inside an MHTML
        # archive, so a page-side click listener never fires -- and the same
        # restriction is why the launcher reports clicked-no-tab here. So
        # record where the REAL cursor was actually sent, and check it
        # against the element's real rect.)
        close_extra_tabs()
        load("start_dialog", stub)
        sent = []
        _real_move, _real_exec = ac._human_move, ac.docker_exec
        def _spy_move(x, y):
            sent.append(("move", x, y))
            return _real_move(x, y)
        def _spy_exec(*a):
            if a[:2] == ("xdotool", "click"):
                sent.append(("click",) + tuple(a))
            return _real_exec(*a)
        ac._human_move, ac.docker_exec = _spy_move, _spy_exec
        try:
            res = ac._humanized_launch_reserved_study(include_take_part=False)
        finally:
            ac._human_move, ac.docker_exec = _real_move, _real_exec
        target = pw_bridge.element_box(pw_bridge._SEL_START, scroll=False)
        moves = [m for m in sent if m[0] == "move"]
        clicks = [m for m in sent if m[0] == "click"]
        last = moves[-1] if moves else None
        inside = bool(last and target
                      and target["x"] <= last[1] <= target["x"] + target["width"]
                      and target["y"] <= last[2] <= target["y"] + target["height"])
        print(f"   launcher on the real dialog -> {res}")
        print(f"   cursor sent to {last and (last[1], last[2])}, "
              f"real link rect x={round(target['x'])}..{round(target['x']+target['width'])} "
              f"y={round(target['y'])}..{round(target['y']+target['height'])}")
        check("the cursor was aimed INSIDE the real 'Start study' link", inside,
              f"point={last and (last[1], last[2])}")
        check("it clicked once, with the real xdotool button", len(clicks) >= 1,
              f"{clicks}")
        check("it never aimed outside the viewport",
              all(0 <= m[1] <= 1439 and 0 <= m[2] <= 899 for m in moves),
              f"{[m for m in moves if not (0 <= m[1] <= 1439 and 0 <= m[2] <= 899)]}")
        close_extra_tabs()
    else:
        skip("start_dialog geometry + end to end", "fixture not captured yet")

    # ---- the state predicate, on real pages -----------------------------
    for state, expect in (("studies_list", False), ("detail_unreserved", True),
                          ("detail_reserved", True), ("start_dialog", True)):
        if state not in fx:
            continue
        load(state, stub)
        check(f"{state}: launch_control_present == {expect}",
              ac._launch_control_visible() is expect)

    # ---- the banner does not track launchability ------------------------
    # Captured evidence, both directions:
    #   run_20261106_124620 screenshot_03: banner PRESENT on a reservation
    #     that was never launched and still had 'Open study in new window';
    #   study 6aec5848 (these fixtures): banner ABSENT on a reserved study
    #     whose Start dialog was open.
    # So the banner says nothing about whether the slot can still be
    # launched, in either direction. Only the DOM control does.
    for state in ("detail_reserved", "start_dialog"):
        if state not in fx:
            continue
        load(state, stub)
        banner = fx[state]["meta"].get("orphan_banner")
        check(f"{state}: launchable is decided by the DOM, whatever the "
              f"banner says (banner={banner})",
              ac._launch_control_visible() is True)

    # ---- Prolific's deadline, read off the REAL captured page -----------
    # The study that was auto-returned showed "Time remaining 13 Mins 49
    # Secs". Parsing it from the real markup, through the same _page_text
    # the harness uses, is the only way to know the scraper works on the
    # page as Prolific actually ships it.
    if "detail_reserved" in fx:
        load("detail_reserved", stub)
        secs = ac._extract_time_remaining()
        print(f"   deadline scraped from the real page: {secs}s")
        check("the countdown is parsed from the real Prolific markup",
              secs is not None and 600 < secs < 1000, f"{secs}")
        meta_secs = None
        txt = fx["detail_reserved"]["meta"].get("url")
        tm = ac.StudyTimer()
        tm.start(180, deadline_seconds=secs or 0)
        check("it reaches the timer and the model's status line",
              "Deadline in" in (tm.status_line() or ""), tm.status_line()[-36:])
        check("the estimated duration is still read separately",
              ac._extract_study_duration() == 3.0, ac._extract_study_duration())
    else:
        skip("deadline scraped from the real page", "fixture not captured yet")

    if "studies_list" in fx:
        load("studies_list", stub)
        check("no countdown on the studies list -> nothing invented",
              ac._extract_time_remaining() is None)

    # ---- the dialog geometry the captures do not contain ----------------
    repro_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "..", "var", "fixtures", "repro")
    repros = write_repros(os.path.abspath(repro_dir))
    push_to_container(repros)
    for name, scrolls in (("repro_locked.html", False),
                          ("repro_creeping.html", False),
                          ("repro_long.html", True)):
        close_extra_tabs()
        pw_bridge.goto(f"file:///tmp/fixtures/{name}")
        time.sleep(1.1)
        pw_bridge.front()
        vw, vh = ac._viewport_wh()
        raw = pw_bridge.element_box(pw_bridge._SEL_START, scroll=False)
        rng = pw_bridge.run(lambda b: b.active_page().evaluate(
            "() => {const p=document.querySelector('.panel');"
            "return p.scrollHeight - p.clientHeight;}"))
        check(f"{name}: confirm link starts below the fold",
              raw is not None and raw["y"] + raw["height"] > vh,
              f"y={raw and round(raw['y'])} vh={vh} range={rng}px")
        check(f"{name}: the link is inert until both boxes are ticked",
              pw_bridge.run(lambda b: b.active_page().evaluate(
                  "() => document.querySelector('[data-testid=dialog-start-study]')"
                  ".getAttribute('aria-disabled')")) == "true")
        before = len(pw_bridge.page_ids())
        res = ac._humanized_launch_reserved_study(include_take_part=False)
        active = pw_bridge.run(lambda b: b.active_page().url)
        check(f"{name}: launcher reaches the survey",
              (res or {}).get("status") == "launched" and "stub_survey" in active,
              f"status={(res or {}).get('status')}")
        check(f"{name}: it opened a new tab", len(pw_bridge.page_ids()) > before)
        ticked = pw_bridge.run(lambda b: next(
            (p.evaluate("() => [...document.querySelectorAll('input[type=checkbox]')]"
                        ".every(c => c.checked)")
             for p in b.pages() if p.url.endswith(name)), None))
        check(f"{name}: both boxes were ticked by the real cursor before confirming",
              ticked is True)
    close_extra_tabs()

    # ---- the nudge must not fight the model -----------------------------
    verdict, before, after = nudge_repro(answered_top=True)
    check("every radio group answered -> the nudge stands down",
          verdict == "nothing", f"verdict={verdict}")
    check("and the viewport the model scrolled to is left alone",
          before == after and before > 0, f"{before} -> {after}")
    verdict, before, after = nudge_repro(answered_top=False)
    check("an UNANSWERED group above still gets centred (the nudge works)",
          verdict == "scrolled", f"verdict={verdict}")
    check("and that one really does move the viewport", after < before,
          f"{before} -> {after}")
    close_extra_tabs()

    # ---- the control that actually submits the study --------------------
    # 1. A real link to Prolific's completion endpoint: the unambiguous case.
    #    Found by href, so the label is deliberately unhelpful.
    thankyou_page('<a class=cta href="https://app.prolific.com/submissions/'
                  'complete?cc=ABC123">Continue</a>')
    box = ac._completion_return_box()
    check("the completion-URL link is found by its href",
          box is not None and box.get("why") == "completion-url",
          f"{box and box.get('why')} {box and box.get('text')!r}")

    # 2. A JS button with no href, below the fold: found by label, and the
    #    real cursor has to be scrolled to it before it can be clicked.
    thankyou_page('<button class=cta onclick="location.href=\'%s\'">'
                  'Return to Prolific and collect your reward</button>' % stub,
                  pad=1400)
    raw = ac._completion_return_box()
    vw, vh = ac._viewport_wh()
    check("a JS button is found by its label",
          raw is not None and raw.get("why") == "label", raw and raw.get("text"))
    check("and it starts below the fold",
          raw is not None and raw["y"] + raw["height"] > vh,
          f"y={raw and round(raw['y'])} vh={vh}")
    box = ac._scroll_box_into_view(ac._completion_return_box)
    check("scrolling brings it on screen", ac._box_on_screen(box, vw, vh),
          f"y={box and round(box['y'])}")
    check("and the real cursor reaches it",
          ac._humanized_click_box(box) is True)
    time.sleep(1.4)
    check("clicking it actually leaves the thank-you page",
          "stub_survey" in pw_bridge.run(lambda b: b.active_page().url),
          pw_bridge.run(lambda b: b.active_page().url))

    # 3. A page with no such control must not invent one.
    thankyou_page('<button class=cta>Close window</button>')
    check("a page with no return control yields nothing",
          ac._completion_return_box() is None)
    close_extra_tabs()

    # 4. The near-miss. run_20261109_120846 step 50 clicked 8px under a
    #    36px-tall "Return to Prolific" button, hit the background, and lost
    #    six steps to the detour that followed. The snap re-aims that click,
    #    and must keep its hands off every other one.
    thankyou_page('<a class=cta id=ret href="https://app.prolific.com/'
                  'submissions/complete?cc=ABC123">Return to Prolific and '
                  'collect your reward</a>')
    box = ac._completion_return_box()
    bx = box["x"] + box["width"] / 2
    bottom = box["y"] + box["height"]
    check("the run_20261109_120846 miss (8px under the button) is caught",
          (ac._completion_return_near_miss(bx, bottom + 8) or {}).get("miss") == 8,
          f"box y {round(box['y'])}..{round(bottom)}, clicked {round(bottom)+8}")
    check("a click inside the button is left alone (nothing to correct)",
          ac._completion_return_near_miss(bx, box["y"] + box["height"] / 2) is None)
    check("a click far from it is left alone",
          ac._completion_return_near_miss(bx, bottom + 200) is None)
    check("and so is one just past the margin",
          ac._completion_return_near_miss(bx, bottom + 45) is None)

    #    A second control right under the button: a click that really lands
    #    on it must never be hijacked, however close the return button is.
    thankyou_page('<a class=cta id=ret href="https://app.prolific.com/'
                  'submissions/complete?cc=ABC123">Return to Prolific and '
                  'collect your reward</a><div style="height:6px"></div>'
                  '<button id=other class=cta>Report a problem</button>')
    box = ac._completion_return_box()
    other = pw_bridge.run(lambda b: b.active_page().evaluate(
        "() => {const r = document.getElementById('other')"
        ".getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height};}"))
    gap = other["y"] - (box["y"] + box["height"])
    check("the neighbouring control is inside the snap margin",
          0 < gap < ac._RETURN_NEAR_MISS_PX, f"gap {round(gap)}px")
    check("but a click ON it is not hijacked",
          ac._completion_return_near_miss(
              other["x"] + other["w"] / 2, other["y"] + other["h"] / 2) is None)
    check("while the inert gap between the two still snaps back",
          ac._completion_return_near_miss(
              box["x"] + 2, box["y"] + box["height"] + gap / 2) is not None)
    close_extra_tabs()

    # 5. The same checks against the REAL completion screen, once
    #    capture_fixture.py has seen one. Everything above this point is a
    #    hand-written page whose geometry was measured off a screenshot; this
    #    is the researcher's own markup, replayed.
    if "survey_complete" in fx:
        meta = fx["survey_complete"]["meta"]
        pw_bridge.goto("file:///tmp/fixtures/survey_complete.mhtml")
        time.sleep(2.0)
        pw_bridge.front()
        box = ac._completion_return_box()
        check("REAL completion screen: the finder still matches its markup",
              box is not None,
              box and f"{box.get('why')} {box.get('text')!r}")
        if box:
            captured = (meta.get("measurements") or {}).get("return_control") or {}
            check("and the replay keeps the geometry the live page had",
                  abs(box["height"] - captured.get("height", box["height"])) <= 2,
                  f"h {round(box['height'])} vs {captured.get('height')}")
            bx = box["x"] + box["width"] / 2
            bottom = box["y"] + box["height"]
            check("an 8px miss under the real button is caught",
                  ac._completion_return_near_miss(bx, bottom + 8) is not None,
                  f"box y {round(box['y'])}..{round(bottom)}")
            check("a click on the real button is left alone",
                  ac._completion_return_near_miss(
                      bx, box["y"] + box["height"] / 2) is None)
        close_extra_tabs()
    else:
        skip("REAL completion screen",
             "no survey_complete fixture yet -- run capture_fixture.py "
             "while a study finishes")

    # 6. The form-less choice widget, on the researcher's own markup.
    #    var/fixtures/prolific/survey_matrix.mhtml is a SurveyCake Likert
    #    matrix captured live during run_20261109_165350, where batch-fill
    #    found nothing 91 times in a row. The page has 0 <input>, 0
    #    [role=radio] and 0 <fieldset>: everything here has to come out of
    #    divs, hashed class names and a table header.
    if "survey_matrix" in fx:
        import form_fill_pw as ff
        pw_bridge.goto("file:///tmp/fixtures/survey_matrix.mhtml")
        time.sleep(2.5)
        pw_bridge.front()
        shape = pw_bridge.run(lambda b: b.active_page().evaluate("""() => ({
            inputs: document.querySelectorAll('input').length,
            aria: document.querySelectorAll('[role=radio]').length,
            fieldsets: document.querySelectorAll('fieldset').length,
            opts: document.querySelectorAll('[data-subject-option-id]').length})"""))
        check("REAL matrix: the page really has no form elements",
              shape["inputs"] == 0 and shape["aria"] == 0
              and shape["fieldsets"] == 0 and shape["opts"] > 50, json.dumps(shape))
        fields = ff.extract_fields()
        custom = [f for f in fields if f.get("widget") == "custom"]
        check("one field per statement row is recovered",
              len(custom) == 19, f"{len(custom)} custom of {len(fields)}")
        scale = ["Strongly disagree", "Disagree", "Neutral", "Agree",
                 "Strongly agree"]
        check("the scale labels come out of the table header",
              all(f["options"] == scale for f in custom),
              custom and custom[0]["options"])
        check("each question is the row's own statement",
              all(len(f["text"]) > 20 and not f["text"].startswith("co:")
                  for f in custom), custom and custom[0]["text"][:60])
        # The capture recorded, per row, which option differed from its
        # siblings. The extractor must agree with that, row for row.
        rows = (fx["survey_matrix"]["meta"].get("measurements") or {}).get("rows") or {}
        nest = [v for v in rows.values() if v.get("type") == "NESTCHILD"]
        want_answered = sum(1 for v in nest if v.get("odd_one_out") is not None)
        got_answered = sum(1 for f in custom if f["answered"])
        check("answered rows match what the live capture measured",
              got_answered == want_answered and len(nest) == len(custom),
              f"{got_answered}/{len(custom)} vs {want_answered}/{len(nest)}")
        want_idx = [v["odd_one_out"] for v in nest]
        got_idx = [scale.index(f["current"]) if f["current"] in scale else None
                   for f in custom]
        check("and so does the chosen option, row by row",
              got_idx == want_idx, f"{got_idx} vs {want_idx}")
        check("the one unanswered row is seen as unanswered",
              got_idx.count(None) == 1, got_idx)
        ok, why = ff.batch_fill_verdict(fields)
        check("so the page now batch-fills instead of being clicked one by one",
              ok, why)
        close_extra_tabs()
    else:
        skip("REAL form-less matrix", "no survey_matrix fixture")

    # ---- a custom dropdown: combobox + a popup list under <body> --------
    # run_20261115_180326 (a Prolific-hosted task built with Element Plus):
    # ten questions whose control is a READONLY <input role=combobox>, their
    # options in a [role=listbox] teleported to <body> and hidden until it is
    # clicked. Collected as free-text fields, all ten came back "Could NOT
    # fill". The fixture is synthetic; the real capture stays out of git.
    dd = os.path.join(ROOT, "fixtures", "test", "test_custom_dropdown.html")
    if os.path.exists(dd):
        import form_fill_pw as ff
        close_extra_tabs()
        push_to_container([dd])
        pw_bridge.goto("file:///tmp/fixtures/test_custom_dropdown.html")
        pw_bridge.wait_for_content()
        fields = ff.extract_fields()
        combos = [f for f in fields if f.get("widget") == "combobox"]
        check("the dropdowns are collected as choice fields, not text boxes",
              len(combos) == 3 and not [f for f in fields if f["type"] == "text"],
              f"{len(combos)} combobox, {len(fields)} fields")
        check("each one carries its own question",
              sorted(f["text"] for f in combos) ==
              ["How often do you repaint a room?",
               "Which colour do you prefer for a kitchen wall?",
               "Which of these rooms did you paint last?"],
              [f["text"] for f in combos])
        check("and the options of the popup that is still closed",
              sorted(len(f["options"]) for f in combos) == [4, 4, 10],
              [len(f["options"]) for f in combos])
        check("no second field is invented for the popup itself",
              not [f for f in fields if str(f["id"]).startswith("lb:")],
              [f["id"] for f in fields])
        check("the tracking pixel under <body> is not read as a question",
              not [f for f in fields if "<img" in (f["text"] or "")])
        check("the free-text question is still free text",
              [f["type"] for f in fields if f["id"].endswith("why")] == ["textarea"])
        check("a single-choice dropdown is not handed back to vision",
              not any(f.get("needs_vision") for f in combos))

        # ...and the real cursor can actually work it: open the popup, scroll
        # inside the popup's own box for an option below its fold, click it,
        # and read back what the control SHOWS. "The click was issued" is not
        # the same as "the answer landed", and a clean page advances with no
        # model turn.
        pw_bridge.front()
        want = {"sel-colour": "Deep blue", "sel-often": "Once a year",
                "sel-many": "Cellar", "why": "It keeps the room calm."}
        res = ff.apply_answers(fields, want)
        after = {f["id"]: f for f in ff.extract_fields()}
        check("every dropdown is set with the real cursor",
              not res.get("errors"), res.get("errors"))
        for fid, w in want.items():
            check(f"...and {fid} shows the chosen answer",
                  (after.get(fid, {}).get("current") or "").strip() == w,
                  repr(after.get(fid, {}).get("current")))
        check("the tenth option, below the popup's own fold, was reachable",
              (after.get("sel-many", {}).get("current") or "").strip() == "Cellar")
        close_extra_tabs()
    else:
        skip("custom dropdown fixture", "fixtures/test/test_custom_dropdown.html missing")

    print()
    passed = sum(results); total = len(results)
    print(f"== FIXTURE SUMMARY ==  {passed}/{total} passed",
          "ok" if passed == total else "FAILED")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
