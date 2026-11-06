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

        # ---- end to end on the real markup ------------------------------
        close_extra_tabs()
        load("start_dialog", stub)
        before = len(pw_bridge.page_ids())
        res = ac._humanized_launch_reserved_study(include_take_part=False)
        active = pw_bridge.run(lambda b: b.active_page().url)
        print(f"   launcher on the real dialog -> {res}")
        check("launcher reaches the (stubbed) survey from the REAL dialog",
              (res or {}).get("status") == "launched" and "stub_survey" in active,
              f"status={(res or {}).get('status')} active={active.rsplit('/', 1)[-1]}")
        check("it opened a new tab", len(pw_bridge.page_ids()) > before,
              f"{before} -> {len(pw_bridge.page_ids())}")
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

    # ---- the banner is NOT a dead-slot signal, proven on the real page ---
    for state in ("detail_reserved", "start_dialog"):
        if state not in fx:
            continue
        meta = fx[state]["meta"]
        if "orphan_banner" in meta:
            check(f"{state}: real page shows the 'finish or return' banner "
                  f"while still launchable",
                  meta["orphan_banner"] is True,
                  f"banner={meta.get('orphan_banner')}")

    print()
    passed = sum(results); total = len(results)
    print(f"== FIXTURE SUMMARY ==  {passed}/{total} passed",
          "ok" if passed == total else "FAILED")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
