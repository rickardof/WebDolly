"""E2E for the live GUI features (SSE feed, MJPEG view, live card, click markers).
Runs against a TEST gui_server instance (default :8767, test browser container, stub or real model)
-- never point it at the port of a live run. Usage: gui_live_e2e.py [base_url] [quick]"""
import statistics as st, subprocess, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8767"
assert "8766" not in BASE, "refusing to run against the production GUI"
OUT = str(Path(__file__).resolve().parent.parent / "var" / "gui_shots_live")
Path(OUT).mkdir(parents=True, exist_ok=True)
CONTAINER = "fara-browser-test"
res = []
def check(name, ok, detail=""):
    res.append(bool(ok)); print(f"{'PASS' if ok else 'FAIL'}  {name:58s} {detail}", flush=True)
def loops():
    r = subprocess.run(["docker", "exec", CONTAINER, "bash", "-c", "ps aux | grep -c '[_]fara_live'"], capture_output=True, text=True)
    return int(r.stdout.strip() or 0)
def settled_loops(want, wait=1.5):
    """A loop being replaced overlaps its successor for <100 ms (it notices the closed stdin on its next read)."""
    t = time.time(); n = loops()
    while n != want and time.time() - t < wait: time.sleep(0.05); n = loops()
    return n
def post(pg, path, body=None):
    return pg.evaluate("([p,b]) => postJSON(p,b)", [path, body or {}])

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1500, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE + "/"); time.sleep(2)

    # ---- A. MJPEG stream is up
    nat = pg.evaluate("[screenImg.naturalWidth, screenImg.naturalHeight, mode]")
    check("A1 live view is an MJPEG stream (1440x900)", nat == [1440, 900, "stream"], str(nat))
    check("A2 LIVE badge on", "LIVE" in pg.inner_text("#liveBadge") and pg.evaluate("sseOpen"), pg.inner_text("#liveBadge"))
    check("A3 capture loop running while watched", settled_loops(1) == 1, f"loops={loops()}")

    # ---- B. change -> visible latency, stream vs old polling (measured inside the page, same colours)
    JS = """async (n) => {
      const cv = document.createElement('canvas'); cv.width = 40; cv.height = 40; const cx = cv.getContext('2d', {willReadFrequently: true});
      const px = () => { cx.drawImage(screenImg, 700, 440, 40, 40, 0, 0, 40, 40); return Array.from(cx.getImageData(20,20,1,1).data.slice(0,3)); };
      const cols = [[220,20,20],[20,20,220],[20,160,20]]; const lat = [];
      for (let i = 0; i < n; i++) {
        const c = cols[i % 3];
        const url = `data:text/html,<body style="margin:0;background:rgb(${c})">`;
        const t0 = performance.now(); await fetch('/navigate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url})});
        let ok = false;
        while (performance.now() - t0 < 4000) {
          const q = px(); if (Math.abs(q[0]-c[0]) < 30 && Math.abs(q[1]-c[1]) < 30 && Math.abs(q[2]-c[2]) < 30) { ok = true; break; }
          await new Promise(r => requestAnimationFrame(r));
        }
        lat.push(ok ? performance.now() - t0 : null);
        await new Promise(r => setTimeout(r, 300 + Math.random() * 600));   // random phase vs the 900 ms poll timer
      }
      return lat; }"""
    lat_stream = [x for x in pg.evaluate(JS, 9) if x]
    pg.evaluate("mode = 'poll'; screenImg.removeAttribute('src')"); time.sleep(1.5)
    lat_poll = [x for x in pg.evaluate(JS, 9) if x]
    pg.evaluate("useStream()"); time.sleep(1.2)
    print(f"      navigate->visible  stream: median {st.median(lat_stream):.0f} ms (max {max(lat_stream):.0f})   polling(old): median {st.median(lat_poll):.0f} ms (max {max(lat_poll):.0f})")
    check("B1 stream shows a page change faster than polling", st.median(lat_stream) < st.median(lat_poll) * 0.7,
          f"{st.median(lat_stream):.0f} vs {st.median(lat_poll):.0f} ms")
    check("B2 stream worst case under 600 ms", max(lat_stream) < 600, f"{max(lat_stream):.0f} ms")
    post(pg, "/navigate", {"url": "file:///data/test.html"}); time.sleep(1)

    # ---- C. agent run: live card, tokens, markers, ordering, event latency
    pg.evaluate("""() => { window.__lat = {ev: [], live: []}; window.__evs = []; window.__marks = [];
      const oe = applyEvent; applyEvent = function(ev) { window.__lat.ev.push(Date.now()/1000 - ev.ts); window.__evs.push(ev); return oe(ev); };
      const ol = applyLive; applyLive = function(m) { if (m.type === 'token' || m.type === 'phase') window.__lat.live.push(Date.now()/1000 - m.ts); if (m.type === 'target') window.__marks.push(m); return ol(m); };
      // events arrive through the EventSource listeners created before; rebind them to the wrappers
      connectEvents(); }""")
    time.sleep(1)
    pg.click("text=Compito e opzioni"); pg.fill("#taskBox", "Do the scripted steps."); pg.click("#runBtn")
    t_start = time.time()
    while time.time() - t_start < 5 and not pg.evaluate("running"): time.sleep(0.02)
    check("C0 pill/button switch to running within 300 ms of the click", time.time() - t_start < 0.3, f"{(time.time()-t_start)*1000:.0f} ms")
    seen_len, seen_phase, marker_ok, marker_detail, shot_done = [], set(), None, "", False
    while time.time() - t_start < 40 and pg.evaluate("running"):
        info = pg.evaluate("""() => ({phase: live.phase, len: live.text.length, card: document.getElementById('liveCard').className,
            chip: document.getElementById('phaseChip').className, markers: document.querySelectorAll('#overlay .marker i').length})""")
        if info["phase"]: seen_phase.add(info["phase"])
        if info["phase"] == "think": seen_len.append(info["len"])
        if info["markers"] and marker_ok is None:
            pos = pg.evaluate("""() => { const i = document.querySelector('#overlay .marker i').getBoundingClientRect();
                const m = window.__marks[window.__marks.length - 1]; const g = screenGeom(); const v = document.getElementById('viewer').getBoundingClientRect();
                return {cx: i.left + i.width/2, cy: i.top + i.height/2, ex: v.left + g.ox + m.x * g.sc, ey: v.top + g.oy + m.y * g.sc, m}; }""")
            marker_ok = abs(pos["cx"] - pos["ex"]) < 3 and abs(pos["cy"] - pos["ey"]) < 3
            marker_detail = f"marker @({pos['cx']:.0f},{pos['cy']:.0f}) expected ({pos['ex']:.0f},{pos['ey']:.0f}) for model target {pos['m']['x']:.0f},{pos['m']['y']:.0f}"
            pg.screenshot(path=f"{OUT}/marker.png")
        if info["phase"] == "think" and info["len"] > 40 and not shot_done:
            pg.screenshot(path=f"{OUT}/thinking.png"); shot_done = True
        time.sleep(0.04)
    time.sleep(1.0)
    evs = pg.evaluate("window.__evs"); lat = pg.evaluate("window.__lat")
    steps = [e for e in evs if e["type"] == "step"]
    check("C1 phases observed: observe, think, act", {"observe", "think", "act"} <= seen_phase, str(sorted(seen_phase)))
    distinct = len(set(seen_len))
    check("C2 model text streams in gradually", distinct >= 6 and seen_len == sorted(seen_len) or distinct >= 6, f"{distinct} distinct lengths while thinking, max {max(seen_len or [0])} chars")
    check("C3 click marker drawn at the model's target", marker_ok, marker_detail)
    check("C4 feed: steps in order, no duplicates", [e["step"] for e in steps] == sorted(set(e["step"] for e in steps)) and len(steps) >= 4, str([e["step"] for e in steps]))
    check("C5 final 'Completato' row shows answer", pg.locator("#feed .row.done:not(.stopped)").count() == 1 and "DONE-STUB" in pg.inner_text("#feed"))
    check("C6 feed rows == events (nothing lost)", pg.locator("#feed .row.step").count() == len(steps), f"{pg.locator('#feed .row.step').count()} rows / {len(steps)} steps")
    check("C7 live card gone when run is over", pg.evaluate("document.getElementById('liveCard').innerHTML === ''") and not pg.evaluate("document.getElementById('phaseChip').classList.contains('show')"))
    mx = lambda a: (st.median(a) * 1000, max(a) * 1000)
    print(f"      push latency server->screen: events median {mx(lat['ev'])[0]:.0f} ms (max {mx(lat['ev'])[1]:.0f}); live msgs median {mx(lat['live'])[0]:.0f} ms (max {mx(lat['live'])[1]:.0f})")
    check("C8 events appear within 150 ms of being produced", mx(lat["ev"])[1] < 150, f"max {mx(lat['ev'])[1]:.0f} ms")
    time.sleep(3.0)
    check("C9 marker faded away afterwards", pg.evaluate("document.querySelectorAll('#overlay .marker, #overlay .toast').length") == 0)
    pg.screenshot(path=f"{OUT}/after_run.png")

    # ---- D. reconnect mid-run: no lost / duplicated rows
    pg.evaluate("window.__evs = []")
    pg.click("text=Compito e opzioni"); pg.fill("#taskBox", "Do the scripted steps again."); pg.click("#runBtn"); time.sleep(1.2)
    for _ in range(3):
        pg.evaluate("es.onerror = null; es.close(); sseOpen = false; setTimeout(connectEvents, 0)")   # drop + resume like a network blip
        time.sleep(0.9)
    t0 = time.time()
    while time.time() - t0 < 40 and pg.evaluate("running"): time.sleep(0.3)
    time.sleep(0.8)
    seqs = pg.evaluate("Array.from(document.querySelectorAll('#feed .row')).length")
    server = pg.evaluate("fetch('/agent/status?since=0').then(r => r.json()).then(d => d.events.filter(e => e.type !== 'cycle').length)")
    check("D1 after 3 reconnects the feed has every event once", seqs == server, f"{seqs} rows vs {server} events on server")
    check("D2 SSE is open again", pg.evaluate("sseOpen"))

    # ---- E. Stop while the model is generating
    pg.click("text=Compito e opzioni"); pg.fill("#taskBox", "Do the scripted steps (stop test)."); pg.click("#runBtn"); time.sleep(1.0)
    t0 = time.time()
    while time.time() - t0 < 10 and pg.evaluate("live.phase !== 'think'"): time.sleep(0.03)
    t = time.time(); pg.click("#runBtn")
    while time.time() - t < 10 and "Avvia" not in pg.inner_text("#runBtn"): time.sleep(0.02)
    dt = time.time() - t; time.sleep(0.3)
    check("E1 Stop mid-generation returns to Avvia fast", "Avvia" in pg.inner_text("#runBtn") and dt < 1.5, f"{dt:.2f}s")
    check("E2 live card cleared after stop", pg.evaluate("live.phase === null"))
    check("E3 labelled 'Fermato'", pg.locator("#feed .row.done.stopped").count() >= 1)

    # ---- F. second viewer + hidden tab + fallback
    pg2 = b.new_page(viewport={"width": 900, "height": 600}); pg2.goto(BASE + "/"); time.sleep(2)
    check("F1 a second viewer gets the stream too", pg2.evaluate("[screenImg.naturalWidth, mode]") == [1440, "stream"])
    time.sleep(0.5); n_loops = settled_loops(1)
    check("F2 still ONE capture loop for two viewers", n_loops == 1, f"loops={n_loops}")
    pg2.close(); time.sleep(1.0)
    check("F3 first viewer unaffected after the other left", pg.evaluate("mode") == "stream" and settled_loops(1) == 1)
    pg.evaluate("Object.defineProperty(document, 'hidden', {value: true, configurable: true}); document.dispatchEvent(new Event('visibilitychange'))")
    time.sleep(7)
    check("F4 hidden tab closes the stream and the capture loop stops", (not pg.evaluate("screenImg.hasAttribute('src')")) and loops() == 0, f"loops={loops()}")
    pg.evaluate("Object.defineProperty(document, 'hidden', {value: false, configurable: true}); document.dispatchEvent(new Event('visibilitychange'))")
    t_vis = time.time(); st5 = None
    while time.time() - t_vis < 8:
        st5 = pg.evaluate("[screenImg.naturalWidth, mode, document.hidden]")
        if st5[0] == 1440 and st5[1] == "stream": break
        time.sleep(0.05)
    check("F5 visible again: stream resumes", st5[0] == 1440 and st5[1] == "stream" and loops() == 1, f"{time.time()-t_vis:.2f}s -> {st5}, loops={loops()}")
    pg.route("**/stream.mjpg*", lambda r: r.abort())
    pg.evaluate("window.__errlog = []; screenImg.addEventListener('error', () => window.__errlog.push([mode, document.hidden, Date.now()]))")
    pg.evaluate("useStream()"); t6 = time.time(); shot = None
    while time.time() - t6 < 6:
        shot = pg.evaluate("[mode, screenImg.naturalWidth]")
        if shot[0] == "poll" and shot[1] == 1440: break
        time.sleep(0.05)
    check("F6 stream failure falls back to screenshot polling", shot[0] == "poll" and shot[1] == 1440, f"{time.time()-t6:.2f}s {shot} errors={pg.evaluate('window.__errlog')}")
    pg.unroute("**/stream.mjpg*"); time.sleep(6)
    check("F7 ...and returns to the stream once it works again", pg.evaluate("mode") == "stream")

    check("no JavaScript errors", not errs, "; ".join(errs[:3]))
    b.close()
print(f"\n{sum(res)}/{len(res)} passed")
sys.exit(0 if all(res) else 1)
