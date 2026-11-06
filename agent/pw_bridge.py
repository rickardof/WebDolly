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
        # no_defaults keeps Playwright from applying its default overrides to the
        # existing default context, which is the one we attach to. It is the
        # public, documented way to avoid two fingerprints: focus emulation
        # (document.hasFocus() keeps returning true on a background tab, which a
        # real browser never does) and media emulation (colorScheme,
        # reducedMotion, forcedColors and contrast forced to Playwright's values
        # instead of the browser's own). Undoing the focus one over raw CDP
        # instead needs a session kept attached per page AND a re-send after
        # every navigation, since Playwright re-applies it for each document.
        self._browser = self._pw.chromium.connect_over_cdp(CDP_URL, no_defaults=True)
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
                if id(page) not in self._stealth_pages:
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


# Stable data-testid selectors captured from Prolific's live reserved-study
# page + its "Start study" launch dialog (ds-dialog-panel). These are
# first-party Prolific UI, identical across every external study, so the launch
# is deterministic and does not depend on the vision model aiming a pixel click
# at a small link that sits just above "Report study" and whose confirm control
# can fall below the dialog fold. The CLICKS themselves are NOT done here: the
# launcher in agent_core drives them with the same humanised xdotool cursor the
# vision path uses, because CDP Input.dispatch* / DOM .click() are flagged by
# Prolific's behavioural fraud scoring (see the input backend note in
# agent_core). These helpers only READ the DOM to ground those real clicks.
_SEL_RESERVE = '[data-testid="reserve"]'            # "Take part in this study"
_SEL_OPEN_DIALOG = '[data-testid="open-study-in-new-window-button"]'
_SEL_DIALOG = '[data-testid="start-study-dialog"]'
_SEL_START = '[data-testid="dialog-start-study"]'   # <a href=<survey> target=_blank>


def front() -> str:
    """Bring the tracked active page to the foreground and return its URL.

    scrot (the model's screenshot) and xdotool (its clicks) act on whatever
    tab Chromium has in front, while every URL guard and DOM read uses this
    bridge's `active_page()`. In this kiosk Playwright cannot tell which tab is
    actually front (all report visible+focused), so without re-asserting it the
    two can drift apart (measured: a survey opened in a new tab, the screenshot
    showed one page while the guards read another, and the model cancelled the
    wrong study). Called before every screenshot to pin front == active."""
    def _f(b):
        try:
            b.active_page().bring_to_front()
        except Exception:
            pass
        try:
            return b.active_page().url
        except Exception:
            return ""
    return run(_f)


def has_selector(selector: str) -> bool:
    return bool(run(lambda b: b.active_page().query_selector(selector) is not None))


def wait_selector(selector: str, timeout_ms: int = 6000) -> bool:
    def _w(b):
        try:
            b.active_page().wait_for_selector(selector, timeout=timeout_ms)
            return True
        except Exception:
            return False
    return bool(run(_w, timeout=timeout_ms / 1000 + 10))


def element_box(selector: str, scroll: bool = True, timeout_ms: int = 4000):
    """Viewport box {x,y,width,height} of the first match (scrolled into view),
    or None. The kiosk browser fills the screen with no toolbar, so viewport
    coordinates equal screenshot/xdotool screen coordinates."""
    def _b(b):
        el = b.active_page().query_selector(selector)
        if el is None:
            return None
        if scroll:
            try:
                el.scroll_into_view_if_needed(timeout=timeout_ms)
            except Exception:
                pass
        try:
            return el.bounding_box()
        except Exception:
            return None
    return run(_b, timeout=timeout_ms / 1000 + 10)


def viewport_size():
    """{'width','height'} of the active page's viewport, or None if unknown.
    Used to reject a click whose target sits outside the visible area."""
    def _v(b):
        try:
            return b.active_page().evaluate(
                "() => ({width: window.innerWidth, height: window.innerHeight})")
        except Exception:
            return None
    return run(_v)


def launch_control_present() -> bool:
    """True when the page still offers a way INTO the study: 'Take part in
    this study', 'Open study in new window', or an already-open 'Start study'
    dialog.

    THIS, not Prolific's "Please either finish or return this submission"
    banner, tells a launchable study from a dead slot. Captured evidence says
    the banner tracks neither: it was PRESENT on a reservation that had never
    been launched and still offered 'Open study in new window'
    (run_20261106_124620 screenshot_03, where reading it as "orphan" steered
    the agent into cancelling a GBP 2,60 study), and ABSENT on a reserved
    study with its Start dialog open (fixture study 6aec5848). Only the
    presence of a control says whether there is still a way in.
    """
    def _p(b):
        page = b.active_page()
        for sel in (_SEL_DIALOG, _SEL_OPEN_DIALOG, _SEL_RESERVE):
            el = page.query_selector(sel)
            if el is None:
                continue
            try:
                if el.is_visible():
                    return True
            except Exception:
                return True
        return False
    return bool(run(_p))


def focus_element(selector: str) -> bool:
    """Give DOM focus to the first match so a REAL xdotool keystroke can
    activate it. Only the focus is programmatic; the activating Enter is a
    genuine keyboard event (isTrusted), unlike a dispatched click. Last-resort
    path for a control that no amount of scrolling brings into view."""
    def _f(b):
        el = b.active_page().query_selector(selector)
        if el is None:
            return False
        try:
            el.focus()
            return True
        except Exception:
            return False
    return bool(run(_f))


def unchecked_checkbox_boxes(dialog_sel: str, scroll: bool = True) -> list:
    """For each UNCHECKED checkbox in the dialog, the viewport box of its
    clickable target (the enclosing <label> if any, else the input itself).
    Empty list if the dialog or its boxes are gone. With scroll=False the
    boxes are read as-is, leaving the caller to bring them into view on the
    real wheel path instead of via JS scrollIntoView."""
    def _c(b):
        page = b.active_page()
        dlg = page.query_selector(dialog_sel)
        if dlg is None:
            return []
        out = []
        for inp in dlg.query_selector_all('input[type="checkbox"]'):
            try:
                if inp.is_checked():
                    continue
            except Exception:
                pass
            target = inp
            try:
                lbl = inp.evaluate_handle("el => el.closest('label')").as_element()
                if lbl is not None:
                    target = lbl
            except Exception:
                pass
            if scroll:
                try:
                    target.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
            try:
                box = target.bounding_box()
            except Exception:
                box = None
            if box:
                out.append(box)
        return out
    return run(_c, timeout=20)


def page_ids() -> list:
    """Identities of the currently-open pages, to detect ones opened later."""
    return run(lambda b: [id(p) for p in b.pages()])


def follow_new_tab(before_ids: list, timeout_ms: int = 9000):
    """Poll for a page opened AFTER before_ids (a target=_blank survey tab),
    make it the active+front page once it has a real URL, and return that URL.
    None if no new tab appears in time."""
    def _f(b):
        before = set(before_ids)
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline and not cancel.is_cancelled():
            for p in b.pages():
                if id(p) in before:
                    continue
                try:
                    p.wait_for_load_state("domcontentloaded", timeout=2500)
                except Exception:
                    pass
                try:
                    u = p.url
                except Exception:
                    u = ""
                if u and u != "about:blank":
                    b._active = p
                    b._known = b.pages()
                    try:
                        p.bring_to_front()
                    except Exception:
                        pass
                    return u
            try:
                b.active_page().wait_for_timeout(200)
            except Exception:
                time.sleep(0.2)
        return None
    return run(_f, timeout=timeout_ms / 1000 + 20)


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
