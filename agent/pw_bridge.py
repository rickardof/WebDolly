"""Single-threaded Playwright bridge to the agent's Chromium (over CDP).

Playwright's sync API is not thread-safe, but the GUI server calls into the
browser from several threads (HTTP handlers + the agent thread). Every
Playwright call is therefore funnelled through ONE worker thread that owns
the connection: callers submit a function and block on its result.

Connects to the already-running kiosk Chromium (connect_over_cdp), so the
persistent profile, logins and the Xvfb display Fara sees are untouched --
Playwright only replaces the hand-rolled cdp.py plumbing.
"""

import os
import queue
import cancel
import time
import threading

from playwright.sync_api import sync_playwright

CDP_URL = os.environ.get("FARA_CDP_URL", "http://127.0.0.1:9333")


# Fingerprint-stealth script applied on every context after (re)connect.
# Kept minimal to avoid multi-engine mismatches: fixes signals that are
# clearly off (deviceMemory=null, notifications=denied, WebGL hidden) and
# leaves everything else to the container flags (lang, accept-lang, TZ).
# WebGL params use plausible Linux-Intel values so they match --use-gl.
_STEALTH_JS = r"""
(() => {
  if (window.__fara_stealth_applied) return;
  Object.defineProperty(window, '__fara_stealth_applied',
      {value: true, writable: false, configurable: false, enumerable: false});

  // deviceMemory: null in Linux Chrome without the hint, 8 is the usual
  // bucket for a modern laptop/desktop.
  try {
    Object.defineProperty(Navigator.prototype, 'deviceMemory',
        {get: () => 8, configurable: true});
  } catch (e) {}

  // notifications permission: Linux kiosk reports 'denied' which is a
  // headless-smell. A real browser with no prior interaction returns
  // 'default' (prompt). Patch only that one name; leave the rest alone.
  try {
    const origQuery = Permissions.prototype.query;
    Permissions.prototype.query = function (p) {
      if (p && p.name === 'notifications') {
        return Promise.resolve({
          state: 'default',
          onchange: null,
          addEventListener() {}, removeEventListener() {},
          dispatchEvent() { return false; },
        });
      }
      return origQuery.call(this, p);
    };
  } catch (e) {}

  // WebGL vendor/renderer: with --use-gl=swiftshader Chromium reports the
  // SwiftShader strings, which is itself a tell. Rewrite only the two
  // UNMASKED_* parameters to plausible Linux-Intel values; everything
  // else (precision, extensions, versions) remains the real value for
  // consistency with Canvas 2D and the actual rasterizer.
  const VENDOR = 'Google Inc. (Intel)';
  const RENDERER = 'ANGLE (Intel, Mesa Intel(R) UHD Graphics (KBL GT2), OpenGL ES 2.0)';
  for (const Ctor of [window.WebGLRenderingContext, window.WebGL2RenderingContext]) {
    if (!Ctor) continue;
    try {
      const origGet = Ctor.prototype.getParameter;
      Ctor.prototype.getParameter = function (p) {
        if (p === 37445) return VENDOR;    // UNMASKED_VENDOR_WEBGL
        if (p === 37446) return RENDERER;  // UNMASKED_RENDERER_WEBGL
        return origGet.call(this, p);
      };
    } catch (e) {}
  }
})();
"""


class _Bridge:
    def __init__(self):
        self._q = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="pw-bridge")
        self._thread.start()

    def _connect(self):
        self._pw = self._pw if getattr(self, "_pw", None) else sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(CDP_URL)
        # Fingerprint stealth: inject on every existing context (for all
        # pages opened later in that context) AND evaluate on every open
        # page right now, since init-scripts don't retro-apply. The script
        # is idempotent (window.__fara_stealth_applied guard) but we still
        # track what's been patched per connect() to avoid wasted CDP calls.
        self._stealth_contexts = set()
        self._stealth_pages = set()
        self._apply_stealth()

    def _apply_stealth(self):
        for ctx in self._browser.contexts:
            if id(ctx) not in self._stealth_contexts:
                try:
                    ctx.add_init_script(_STEALTH_JS)
                    self._stealth_contexts.add(id(ctx))
                except Exception:
                    pass
            for page in ctx.pages:
                if id(page) in self._stealth_pages:
                    continue
                try:
                    page.evaluate(_STEALTH_JS)
                    self._stealth_pages.add(id(page))
                except Exception:
                    pass

    def _run(self):
        self._pw = None
        self._browser = None
        self._active = None
        self._known = []
        while True:
            fn, args, kwargs, box, done = self._q.get()
            try:
                if self._browser is None or not self._browser.is_connected():
                    self._connect()
                    self._active, self._known = None, []
                self._sync_tabs()
                box["result"] = fn(self, *args, **kwargs)
            except Exception as e:  # noqa: BLE001 -- re-raised in the caller's thread
                box["error"] = e
                # A dropped CDP connection (browser container restarted)
                # must not wedge the bridge forever: reconnect next call.
                if self._browser is not None and not self._browser.is_connected():
                    self._browser = None
            finally:
                done.set()

    def call(self, fn, *args, timeout: float = 120, **kwargs):
        box, done = {}, threading.Event()
        self._q.put((fn, args, kwargs, box, done))
        if not done.wait(timeout):
            raise TimeoutError(f"browser bridge call {getattr(fn, '__name__', fn)} timed out")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # -- helpers, only ever run inside the worker thread ---------------------

    def pages(self):
        return [p for ctx in self._browser.contexts for p in ctx.pages]

    def _sync_tabs(self):
        """Keep self._active pointing at the tab that is on screen.

        The page itself can't tell us: measured in this kiosk Chromium,
        every tab reports visibilityState='visible' AND hasFocus()=true,
        foreground or not. So the bridge tracks it instead: a newly opened
        tab is what Chromium brings to the front (target=_blank survey
        links), a closed active tab falls back to the last remaining one,
        and explicit switches go through switch_tab(), which uses
        bring_to_front() so the screen and our pointer can't diverge."""
        # The sync API only processes browser events (e.g. "new tab
        # opened") while it waits on something; reading .pages is a local
        # property and doesn't. Without this pump a tab opened by a real
        # click stayed invisible to us indefinitely (measured).
        anchor = self._active or (self.pages()[0] if self.pages() else None)
        if anchor is not None:
            try:
                anchor.wait_for_timeout(50)
            except Exception:
                pass
        pages = self.pages()
        new = [p for p in pages if p not in self._known]
        if self._active is None or self._active not in pages:
            self._active = pages[-1] if pages else None
            if self._active is not None and self._known:
                try:
                    self._active.bring_to_front()
                except Exception:
                    pass
        if new and self._known:
            self._active = new[-1]
        self._known = list(pages)
        # Re-apply stealth whenever a new context appears (e.g. incognito
        # window). Existing contexts stay in self._stealth_contexts and are
        # skipped, so the per-page evaluate below only fires once per page.
        self._apply_stealth()

    def active_page(self):
        """The tab Fara actually sees (see _sync_tabs). cdp.py always used
        the first tab in CDP's target list, so after a survey opened in a
        new tab, DOM reads could hit the hidden dashboard instead."""
        if self._active is None:
            raise RuntimeError("no open pages in the browser")
        return self._active

    def switch_tab(self, delta: int):
        pages = self.pages()
        if len(pages) < 2:
            return self._active
        i = pages.index(self._active) if self._active in pages else len(pages) - 1
        self._active = pages[(i + delta) % len(pages)]
        self._active.bring_to_front()
        return self._active


_bridge = None
_bridge_lock = threading.Lock()


def bridge() -> _Bridge:
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = _Bridge()
        return _bridge


def run(fn, *args, **kwargs):
    """Run fn(bridge, *args) on the Playwright thread and return its result."""
    return bridge().call(fn, *args, **kwargs)


def drop_connection():
    """Make the next call reconnect over CDP, from any thread.

    Deliberately not submitted through the worker queue: it is used exactly
    when the worker is stuck on a dead page, where a queued job would wait
    behind it. Writing the attribute is safe -- the worker only ever reads
    it to decide whether to reconnect, and a redundant reconnect is harmless.
    """
    b = _bridge
    if b is not None:
        b._browser = None
        b._active, b._known = None, []


def recycle_tab(url: str) -> bool:
    """Replace the current tab with a fresh one, over plain CDP HTTP.

    Measured after a hard restart of the server: Playwright reattached to a
    tab whose renderer had wedged, `page.url` came back as '' and every
    evaluate() hung until the bridge timeout -- so nothing that goes through
    Playwright could fix it. The browser's own HTTP endpoint still answers,
    and a new tab comes up clean, so recovery goes around the bridge
    entirely and then forces it to reconnect.
    """
    import json as _json
    import urllib.request as _ur
    base = CDP_URL.rstrip("/")
    try:
        with _ur.urlopen(f"{base}/json/list", timeout=5) as r:
            old = [t.get("id") for t in _json.load(r) if t.get("type") == "page"]
        req = _ur.Request(f"{base}/json/new?{url}", method="PUT")
        with _ur.urlopen(req, timeout=20) as r:
            new_id = _json.load(r).get("id")
    except Exception:
        return False
    for tid in old:
        if tid and tid != new_id:
            try:
                _ur.urlopen(f"{base}/json/close/{tid}", timeout=5).read()
            except Exception:
                pass
    drop_connection()
    return bool(new_id)


# -- convenience operations used by the agent/GUI ----------------------------

def current_url() -> str:
    return run(lambda b: b.active_page().url)


def goto(url: str):
    def _go(b):
        p = b.active_page()
        p.goto(url, wait_until="domcontentloaded", timeout=30000)
        p.bring_to_front()
    return run(_go)


def go_back() -> bool:
    """Browser Back that can't walk out of the logged-in site. Back from
    the first page after login lands on the pre-login page (measured:
    app.prolific.com/studies/<id> -> www.prolific.com, a logged-out
    marketing page), and that navigation also hung until the 15s timeout,
    which killed the whole run. So: don't wait for load events (commit is
    enough, the next screenshot shows the result), and if the host changed,
    go forward again and report that Back was refused."""
    from urllib.parse import urlparse

    def _back(b):
        page = b.active_page()
        before = urlparse(page.url).netloc
        try:
            page.go_back(wait_until="commit", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(400)
        if urlparse(page.url).netloc != before:
            try:
                page.go_forward(wait_until="commit", timeout=8000)
            except Exception:
                pass
            return False
        return True
    return run(_back)


def reload():
    return run(lambda b: b.active_page().reload(wait_until="domcontentloaded", timeout=30000))


def switch_tab(delta: int) -> str:
    """Ctrl+Tab (delta=+1) / Ctrl+Shift+Tab (delta=-1), done through
    Playwright so the on-screen tab and the tracked active tab match."""
    return run(lambda b: b.switch_tab(delta).url)


def reset_to(url: str):
    """Between loop cycles: close every tab but one and load `url` in it.
    Just navigating the current tab back to the dashboard left each
    finished survey's tab open, so a 24/7 loop would pile up tabs."""
    def _reset(b):
        pages = b.pages()
        keep = b._active if b._active in pages else pages[0]
        for p in pages:
            if p is not keep:
                try:
                    p.close()
                except Exception:
                    pass
        b._active = keep
        b._known = [keep]
        keep.bring_to_front()
        keep.goto(url, wait_until="domcontentloaded", timeout=30000)
    return run(_reset)


def wait_for_content(max_s: float = 5.0, quiet_s: float = 0.4) -> float:
    """Wait until the visible page has rendered something and stopped
    changing, instead of a fixed sleep. SPAs like Prolific are
    "DOMContentLoaded" long before the studies list appears (seen in logs:
    the agent's first screenshot showed a blank page with only the cookie
    banner, costing it extra `wait` steps). Returns seconds waited."""
    def _wait(b):
        t0 = time.time()
        last, stable_since = None, time.time()
        while time.time() - t0 < max_s and not cancel.is_cancelled():
            try:
                txt = b.active_page().evaluate(
                    "() => document.body ? document.body.innerText.length : 0")
            except Exception:
                txt = None
            if txt != last:
                last, stable_since = txt, time.time()
            elif txt and time.time() - stable_since >= quiet_s:
                break
            b.active_page().wait_for_timeout(100)
        return time.time() - t0
    return run(_wait, timeout=max_s + 30)


def page_count() -> int:
    return run(lambda b: len(b.pages()))


def evaluate(js: str):
    return run(lambda b: b.active_page().evaluate(js))
