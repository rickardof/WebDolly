#!/usr/bin/env python3
"""Capture REAL Prolific pages as replayable test fixtures.

Why: the launch bugs we keep chasing are geometry bugs (how tall the 'Start
study' dialog is, where its confirm link sits, how much scroll room there
actually is). Hand-written HTML approximations of those pages are guesses, and
guesses are what sent the agent clicking at an off-screen coordinate. This
walks up to the live browser, waits for each state of the launch flow to
appear, and saves it as MHTML (self-contained: markup + CSS + images) next to
a JSON record of the exact measurements.

Strictly READ-ONLY: it only ever runs Page.captureSnapshot and reading
Runtime.evaluate expressions, so it is safe to leave running against the
production browser while the agent works.

  ./venv_pw/bin/python scripts/tools/capture_fixture.py --minutes 60

Fixtures land in var/fixtures/prolific/ (gitignored -- they contain the
logged-in account's Prolific ID and email).
"""
import argparse, base64, json, os, re, sys, time
from urllib.request import urlopen

import websocket  # websocket-client

CDP = os.environ.get("FARA_CDP_URL", "http://127.0.0.1:9333")
OUT = os.environ.get(
    "FARA_FIXTURE_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "var", "fixtures", "prolific"))

SEL = {
    "reserve": '[data-testid="reserve"]',
    "open_dialog": '[data-testid="open-study-in-new-window-button"]',
    "dialog": '[data-testid="start-study-dialog"]',
    "start": '[data-testid="dialog-start-study"]',
}

# What the page looks like, in the vocabulary the harness uses.
CLASSIFY = """(() => {
  const q = s => document.querySelector(s);
  const vis = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const path = location.pathname;
  const dialog = vis(q('%(dialog)s'));
  const reserve = vis(q('%(reserve)s'));
  const open_dialog = vis(q('%(open_dialog)s'));
  let state = 'other';
  if (dialog) state = 'start_dialog';
  else if (/^\\/studies\\/[0-9a-f]{24}\\/?$/i.test(path)) {
    state = open_dialog ? 'detail_reserved' : (reserve ? 'detail_unreserved' : 'detail_other');
  } else if (/^\\/studies\\/?$/.test(path)) state = 'studies_list';
  return {state, path, url: location.href,
          banner: /either finish or return (this|your) submission/i.test(
                    document.body ? document.body.innerText : '')};
})()""" % SEL

# The measurements the geometry bugs turn on.
MEASURE = """(() => {
  const out = {viewport: {w: innerWidth, h: innerHeight},
               dpr: devicePixelRatio,
               doc_scroll: {top: scrollY,
                            range: document.documentElement.scrollHeight - innerHeight},
               body_overflow: getComputedStyle(document.body).overflow,
               boxes: {}, scrollers: {}};
  const sels = %(sels)s;
  const scrollableAncestor = el => {
    for (let p = el && el.parentElement; p; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (/(auto|scroll|overlay)/.test(cs.overflowY) && p.scrollHeight > p.clientHeight + 1)
        return p;
    }
    return null;
  };
  for (const [name, sel] of Object.entries(sels)) {
    const el = document.querySelector(sel);
    if (!el) { out.boxes[name] = null; continue; }
    const r = el.getBoundingClientRect();
    out.boxes[name] = {x: r.x, y: r.y, width: r.width, height: r.height,
                       below_fold: r.bottom > innerHeight,
                       disabled: el.getAttribute('aria-disabled') === 'true'
                                 || el.hasAttribute('disabled'),
                       tag: el.tagName, href: el.getAttribute('href') || null,
                       target: el.getAttribute('target') || null};
    const sc = scrollableAncestor(el);
    out.scrollers[name] = sc ? {tag: sc.tagName, cls: sc.className,
                                testid: sc.getAttribute('data-testid'),
                                range: sc.scrollHeight - sc.clientHeight,
                                top: sc.scrollTop} : null;
  }
  const dlg = document.querySelector('%(dialog)s');
  if (dlg) {
    out.dialog = {scrollHeight: dlg.scrollHeight, clientHeight: dlg.clientHeight,
                  own_range: dlg.scrollHeight - dlg.clientHeight,
                  rect: dlg.getBoundingClientRect().toJSON(),
                  overflowY: getComputedStyle(dlg).overflowY,
                  position: getComputedStyle(dlg).position};
    out.checkboxes = [...dlg.querySelectorAll('input[type=checkbox]')].map(c => {
      const r = (c.closest('label') || c).getBoundingClientRect();
      return {checked: c.checked, testid: c.getAttribute('data-testid'),
              x: r.x, y: r.y, width: r.width, height: r.height,
              below_fold: r.bottom > innerHeight};
    });
  }
  return out;
})()""" % {"sels": json.dumps(SEL), "dialog": SEL["dialog"]}


class Target:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=25)
        self.n = 0

    def call(self, method, params=None):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method,
                                 "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    def evaluate(self, expr):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True,
                       "awaitPromise": False})
        return (r.get("result") or {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def page_targets():
    try:
        data = json.loads(urlopen(CDP.rstrip("/") + "/json/list", timeout=8).read())
    except Exception:
        return []
    return [t for t in data
            if t.get("type") == "page" and "prolific.com" in (t.get("url") or "")]


def save(state, snapshot_mhtml, meta, outdir):
    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, state)
    with open(base + ".mhtml", "w", encoding="utf-8") as f:
        f.write(snapshot_mhtml)
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
    size = os.path.getsize(base + ".mhtml")
    print(f"[capture] {state:18s} -> {base}.mhtml ({size // 1024} KiB)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--want", default="start_dialog,detail_reserved,"
                                      "detail_unreserved,studies_list")
    ap.add_argument("--force", action="store_true",
                    help="re-capture states already on disk")
    a = ap.parse_args()

    outdir = os.path.abspath(a.out)
    want = [w.strip() for w in a.want.split(",") if w.strip()]
    have = set()
    if not a.force:
        for w in want:
            if os.path.exists(os.path.join(outdir, w + ".mhtml")):
                have.add(w)
    if have:
        print(f"[capture] already have: {', '.join(sorted(have))}", flush=True)
    print(f"[capture] waiting for: {', '.join(w for w in want if w not in have)}",
          flush=True)

    deadline = time.time() + a.minutes * 60
    while time.time() < deadline and set(want) - have:
        for t in page_targets():
            ws_url = t.get("webSocketDebuggerUrl")
            if not ws_url:
                continue
            tgt = None
            try:
                tgt = Target(ws_url)
                info = tgt.evaluate(CLASSIFY) or {}
                state = info.get("state")
                if state in want and state not in have:
                    meta = {"state": state, "url": info.get("url"),
                            "path": info.get("path"),
                            "orphan_banner": info.get("banner"),
                            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "measurements": tgt.evaluate(MEASURE)}
                    snap = tgt.call("Page.captureSnapshot", {"format": "mhtml"})
                    save(state, snap.get("data", ""), meta, outdir)
                    have.add(state)
            except Exception as e:  # noqa: BLE001
                print(f"[capture] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            finally:
                if tgt:
                    tgt.close()
        if set(want) - have:
            time.sleep(a.poll)

    missing = [w for w in want if w not in have]
    print(f"[capture] done. captured={sorted(have)} missing={missing}", flush=True)
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
