#!/usr/bin/env python3
"""End-to-end humanization + fingerprint test suite.

Runs against an isolated test container (`fara-browser-test`, CDP :9334).
Never touches the production stack (:8766 / :9333 / fara-browser-pw).

Each test VERIFIES REAL STATE:
 - typing: reads the textarea value back via Playwright, checks exact match
 - mouse: queries xdotool getmouselocation, checks the cursor really moved
 - scroll: watches `wheel` events dispatched to the page, checks deltas
 - fingerprint: reads `navigator.*` from a live page

The chained-xdotool typing bug this suite now catches:
 - the args structure looks fine (type/sleep/type/sleep...)
 - the walltime looks fine (xdotool types ~500 chars of garbage ≈ 5s)
 - BUT the field value is 'hsleep0.1131type--clearmodifiers...' instead
   of 'hello'. Only an end-to-end DOM read notices.

Exit code 0 if all tests pass, 1 otherwise. Prints a summary.
Run:  python3 tests/humanization_e2e.py
"""
import json
import math
import os
import random
import subprocess
import sys
import time
import unicodedata

# Make the agent/ modules importable without installing them.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "agent"))

# Isolate: force every agent_core.docker_exec call to the test container.
# Must be set BEFORE importing agent_core.
os.environ.setdefault("FARA_CONTAINER", "fara-browser-test")
os.environ.setdefault("FARA_CDP_URL", "http://127.0.0.1:9334")

CDP_URL = os.environ["FARA_CDP_URL"]
CONTAINER = os.environ["FARA_CONTAINER"]


# ─── test harness ─────────────────────────────────────────────────────────────

_RESULTS = []  # list of (name, ok, detail)


def _t(name):
    """Decorator that RUNS the test immediately at @-time. The test body
    is a zero-arg function with no return (passes) or returns a str detail."""
    def deco(fn):
        try:
            detail = fn()
            _RESULTS.append((name, True, detail or ""))
            print(f"  [PASS] {name}  {detail or ''}")
        except AssertionError as e:
            _RESULTS.append((name, False, str(e)))
            print(f"  [FAIL] {name}  {e}")
        except Exception as e:
            import traceback
            tb = traceback.format_exc().splitlines()[-3:]
            _RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
            print(f"  [ERR ] {name}  {type(e).__name__}: {e}")
            for line in tb:
                print(f"         {line}")
        return fn
    return deco


def _ensure_test_container():
    """Start fara-browser-test if it's not running. Build if missing."""
    r = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}:{{.State}}"],
                       capture_output=True, text=True)
    found = [line for line in r.stdout.splitlines() if line.startswith(CONTAINER + ":")]
    if not found:
        print(f"  → {CONTAINER} not found; creating from fara-browser-pw image…")
        img = subprocess.run(["docker", "images", "-q", "fara-browser-pw:latest"],
                             capture_output=True, text=True).stdout.strip()
        if not img:
            print("  → building fara-browser then fara-browser-pw images…")
            subprocess.run(["docker", "build", "-t", "fara-browser",
                            os.path.join(_REPO, "docker/browser")], check=True)
            subprocess.run(["docker", "build", "-t", "fara-browser-pw",
                            os.path.join(_REPO, "docker/browser-pw")], check=True)
        subprocess.run([
            "docker", "run", "-d", "--name", CONTAINER,
            "-e", "TZ=Europe/Rome", "-e", "LANG=it_IT.UTF-8", "-e", "LC_ALL=it_IT.UTF-8",
            "-p", "127.0.0.1:5902:5900", "-p", "127.0.0.1:9334:9333",
            "-v", f"{_REPO}/var/profiles/test:/home/browser/chromium-profile",
            "-v", f"{_REPO}/fixtures/test:/data",
            "fara-browser-pw",
        ], check=True)
    else:
        state = found[0].split(":", 1)[1]
        if "running" not in state.lower():
            print(f"  → starting {CONTAINER}…")
            subprocess.run(["docker", "start", CONTAINER], check=True)
    # Wait for CDP
    for _ in range(30):
        r = subprocess.run(["curl", "-sf", f"{CDP_URL}/json/version"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"CDP at {CDP_URL} never came up")


def _cleanup_strays():
    """Kill any leftover xdotool processes from aborted previous runs: they
    keep typing into whatever is focused and poison every subsequent test
    with mystery characters."""
    subprocess.run(["docker", "exec", CONTAINER, "pkill", "-9", "xdotool"],
                   capture_output=True)
    time.sleep(0.3)


def _close_extra_tabs(b):
    """Close all but one tab so stray keypresses can't bleed between tabs."""
    pages = [p for c in b.contexts for p in c.pages]
    for p in pages[1:]:
        try:
            p.close()
        except Exception:
            pass
    time.sleep(0.2)


# ─── fixtures ─────────────────────────────────────────────────────────────────

def _new_test_page(ctx, html_body=""):
    """Open a fresh page in the test container with a known body."""
    page = ctx.new_page()
    # about:blank in CDP is safe; attach a known body via set_content
    page.set_content(f"<!doctype html><html><body>{html_body}</body></html>")
    return page


def _focused_textarea(page, textarea_id="t"):
    """A big textarea, focused, ready to receive xdotool keystrokes."""
    page.evaluate(f"""() => {{
        document.body.innerHTML =
          '<textarea id="{textarea_id}" autofocus style="width:600px;height:200px;font-size:18px"></textarea>';
        document.getElementById('{textarea_id}').focus();
    }}""")
    page.wait_for_timeout(300)
    page.bring_to_front()
    page.evaluate(f"document.getElementById('{textarea_id}').focus()")
    page.wait_for_timeout(200)


def _read_textarea(page, textarea_id="t"):
    return page.evaluate(f"document.getElementById('{textarea_id}').value")


# ─── TESTS: typing (the regression we just caught) ────────────────────────────

def _run_typing_tests(ac, ctx):
    print("\n== TYPING (end-to-end DOM) ==")

    @_t("type:fast_path_short_ascii")
    def _():
        page = _new_test_page(ctx)
        _focused_textarea(page)
        ac._human_type("hi")
        page.wait_for_timeout(400)
        got = _read_textarea(page)
        assert got == "hi", f"expected 'hi', got {got!r}"
        page.close()
        return "'hi' typed via fast path"

    @_t("type:chain_plain_ascii")
    def _():
        page = _new_test_page(ctx)
        _focused_textarea(page)
        expected = "hello world test case"
        random.seed(1)  # deterministic, avoids typo path
        ac._human_type(expected)
        page.wait_for_timeout(500)
        got = _read_textarea(page)
        assert got == expected, f"expected {expected!r}, got {got!r}"
        page.close()
        return f"{len(expected)} chars matched"

    @_t("type:chain_italian_accents")
    def _():
        page = _new_test_page(ctx)
        _focused_textarea(page)
        expected = "La città è perché così però"
        random.seed(2)
        ac._human_type(expected)
        page.wait_for_timeout(500)
        got = _read_textarea(page)
        # Compare NFC to tolerate xdotool decomposing diacritics.
        n_got = unicodedata.normalize("NFC", got)
        n_exp = unicodedata.normalize("NFC", expected)
        assert n_got == n_exp, f"expected {n_exp!r}, got {n_got!r}"
        page.close()
        return f"accents preserved: {expected!r}"

    @_t("type:chain_punctuation")
    def _():
        page = _new_test_page(ctx)
        _focused_textarea(page)
        expected = "Hello, world! Is it; here? Yes."
        random.seed(3)
        ac._human_type(expected)
        page.wait_for_timeout(500)
        got = _read_textarea(page)
        assert got == expected, f"expected {expected!r}, got {got!r}"
        page.close()
        return "punctuation preserved"

    @_t("type:chain_long_stress")
    def _():
        page = _new_test_page(ctx)
        _focused_textarea(page)
        expected = ("Lorem ipsum dolor sit amet consectetur adipiscing elit "
                    "sed do eiusmod tempor incididunt ut labore et dolore magna aliqua")
        random.seed(4)
        ac._human_type(expected)
        page.wait_for_timeout(1500)
        got = _read_textarea(page)
        assert got == expected, f"len got={len(got)} exp={len(expected)} got[:80]={got[:80]!r}"
        page.close()
        return f"{len(expected)} chars matched"

    @_t("type:typo_recovery_leaves_field_clean")
    def _():
        page = _new_test_page(ctx)
        _focused_textarea(page)
        # Find a seed that forces the typo path for this text.
        expected = "Lorem ipsum dolor sit amet consectetur"
        seed_with_typo = None
        for s in range(200):
            random.seed(s)
            captured = []
            orig = ac.docker_exec
            def _cap(*a):
                captured.append(a)
                class R: stdout = ""; stderr = ""; returncode = 0
                return R()
            ac.docker_exec = _cap
            ac._human_type(expected)
            ac.docker_exec = orig
            if captured and "BackSpace" in captured[0]:
                seed_with_typo = s; break
        assert seed_with_typo is not None, "no seed in 200 fires the typo path"
        random.seed(seed_with_typo)
        ac._human_type(expected)
        page.wait_for_timeout(1000)
        got = _read_textarea(page)
        assert got == expected, (
            f"typo not corrected: expected {expected!r} got {got!r} "
            f"(seed={seed_with_typo})")
        page.close()
        return f"typo (seed {seed_with_typo}) fully corrected via BackSpace"

    @_t("type:walltime_matches_sleeps")
    def _():
        """If chain is broken, type runs through all args ≈ no sleep time.
        If working, walltime ≥ sum of inter-char sleep delays."""
        page = _new_test_page(ctx)
        _focused_textarea(page)
        text = "the quick brown fox jumps"  # 25 chars → 24 inter-char sleeps
        random.seed(5)
        t0 = time.monotonic()
        ac._human_type(text)
        dt = time.monotonic() - t0
        # Lower bound: 24 sleeps × 30ms (min of uniform(.03,.12)) = 720ms
        # Realistic range: 1.2s – 5s depending on draws
        assert dt > 1.0, f"walltime {dt:.2f}s too fast (sleeps bypassed by chain bug?)"
        page.wait_for_timeout(500)
        got = _read_textarea(page)
        assert got == text, f"content wrong: {got!r}"
        page.close()
        return f"walltime {dt:.2f}s (expected 1-5s)"


# ─── TESTS: mouse / click / scroll ────────────────────────────────────────────

def _run_mouse_tests(ac, ctx):
    print("\n== MOUSE / CLICK / SCROLL ==")

    @_t("mouse:move_lands_on_target")
    def _():
        ac._human_move(123, 456)
        x, y = ac._get_cursor_pos()
        assert (x, y) == (123, 456), f"cursor at {(x,y)} not (123,456)"
        return "cursor exact"

    @_t("mouse:bezier_arcs_not_straight")
    def _():
        # Park the cursor
        ac.docker_exec("xdotool", "mousemove", "50", "50")
        captured = []
        orig = ac.docker_exec
        def _cap(*a):
            captured.append(a)
            return orig(*a)
        ac.docker_exec = _cap
        ac._human_move(800, 450)
        ac.docker_exec = orig
        big = captured[-1]
        moves = []
        i = 0
        while i < len(big):
            if big[i] == "mousemove":
                moves.append((int(big[i+1]), int(big[i+2])))
                i += 3
            elif big[i] == "sleep":
                i += 2
            else:
                i += 1
        assert len(moves) >= 8, f"only {len(moves)} waypoints"
        x0, y0 = moves[0]; x1, y1 = moves[-1]
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy)
        max_perp = max(abs(dy*(px-x0) - dx*(py-y0))/L for (px, py) in moves)
        assert max_perp > 5, f"path is almost a straight line (max perp {max_perp:.1f}px)"
        return f"{len(moves)} waypoints, bulge {max_perp:.0f}px"

    @_t("click:aim_jitter_average")
    def _():
        """Jitter is ±1-2px gaussian; over N clicks mean distance from
        target should be > 0 and < 4."""
        captured = []
        orig = ac.docker_exec
        def _cap(*a):
            captured.append(a)
            # Return a fake result (we don't want 60 real cursor moves)
            class R: stdout = "X=0\nY=0\nSCREEN=0\nWINDOW=0"; stderr = ""; returncode = 0
            return R()
        orig_near = ac._near_checkbox_state
        orig_pos = ac._get_cursor_pos
        ac._near_checkbox_state = lambda x, y: None
        ac._get_cursor_pos = lambda: (0, 0)
        ac.docker_exec = _cap
        devs = []
        for _ in range(60):
            captured.clear()
            ac.dispatch_action("left_click", {"coordinate": [500, 400]})
            # Find the big mousemove call (not click/getmouselocation)
            big = None
            for c in captured:
                if "mousemove" in c:
                    big = c; break
            assert big is not None, f"no mousemove in captured calls: {captured}"
            last_aim = None
            for j in range(len(big) - 1, -1, -1):
                if big[j] == "mousemove":
                    last_aim = (int(big[j+1]), int(big[j+2])); break
            assert last_aim, "no mousemove waypoints in big call"
            devs.append(math.hypot(last_aim[0]-500, last_aim[1]-400))
        ac.docker_exec = orig
        ac._near_checkbox_state = orig_near
        ac._get_cursor_pos = orig_pos
        mean = sum(devs)/len(devs)
        off = sum(1 for d in devs if d >= 1)
        assert 0.5 < mean < 4.0, f"mean deviation {mean:.2f}px out of range"
        assert off > len(devs)*0.5, f"only {off}/{len(devs)} clicks had aim-offset"
        return f"mean {mean:.2f}px, {off}/{len(devs)} offset ≥1px"

    @_t("scroll:cadence_has_delay")
    def _():
        captured = []
        orig = ac.docker_exec
        def _cap(*a):
            captured.append(a)
            return orig(*a) if "mousemove" in a or "getmouselocation" in a else type("R",(),{"stdout":"","stderr":"","returncode":0})()
        ac.docker_exec = _cap
        ac.dispatch_action("scroll", {"pixels": -300})
        ac.docker_exec = orig
        has_click = any("click" in c and "--delay" in c for c in captured)
        assert has_click, f"no scroll click with --delay in {captured}"
        return "wheel burst spaced with --delay"

    @_t("scroll:actually_scrolls_page")
    def _():
        page = _new_test_page(ctx)
        page.evaluate("""() => {
            document.body.innerHTML = '<div style="height:4000px"></div>';
            window.scrollTo(0, 0);
        }""")
        page.bring_to_front()
        page.wait_for_timeout(400)
        # Focus the page so wheel events route there
        ac.docker_exec("xdotool", "mousemove", "720", "450")
        before = page.evaluate("window.scrollY")
        ac.dispatch_action("scroll", {"pixels": -500})
        page.wait_for_timeout(600)
        after = page.evaluate("window.scrollY")
        delta = after - before
        page.close()
        assert delta > 50, f"page didn't scroll (before={before}, after={after})"
        return f"page scrolled {delta}px"


# ─── TESTS: fingerprint (what detection scripts see) ──────────────────────────

def _run_fingerprint_tests(ctx, stealth_js):
    print("\n== FINGERPRINT (JS stealth + container) ==")
    # Make sure the stealth init-script is registered for new pages.
    for c in [ctx]:
        try:
            c.add_init_script(stealth_js)
        except Exception:
            pass

    @_t("fp:webdriver_hidden")
    def _():
        page = _new_test_page(ctx)
        r = page.evaluate("() => navigator.webdriver")
        page.close()
        assert r is False, f"navigator.webdriver={r} (expected False)"
        return ""

    @_t("fp:language_italian")
    def _():
        page = _new_test_page(ctx)
        r = page.evaluate("() => [navigator.language, navigator.languages]")
        page.close()
        assert r[0] == "it-IT", f"language={r[0]} (expected it-IT)"
        assert r[1] and r[1][0] == "it-IT", f"languages={r[1]} first should be it-IT"
        return f"language={r[0]} languages={r[1]}"

    @_t("fp:timezone_rome")
    def _():
        page = _new_test_page(ctx)
        r = page.evaluate("() => [Intl.DateTimeFormat().resolvedOptions().timeZone, new Date().getTimezoneOffset()]")
        page.close()
        tz, off = r
        assert tz == "Europe/Rome", f"tz={tz} (expected Europe/Rome)"
        assert off in (-120, -60), f"offset={off} not CET/CEST"
        return f"tz={tz} offset={-off/60:+.0f}h"

    @_t("fp:device_memory_patched")
    def _():
        page = _new_test_page(ctx)
        r = page.evaluate("() => navigator.deviceMemory")
        page.close()
        assert r == 8, f"deviceMemory={r} (expected 8)"
        return ""

    @_t("fp:notifications_permission_default")
    def _():
        page = _new_test_page(ctx)
        r = page.evaluate("async () => (await navigator.permissions.query({name:'notifications'})).state")
        page.close()
        assert r == "default", f"notifications perm={r} (expected default)"
        return ""

    @_t("fp:microphone_permission_untouched")
    def _():
        """We only patched 'notifications' — other names should keep native
        behaviour. If we accidentally forced 'default' everywhere, a
        detector that compares perms across names would notice."""
        page = _new_test_page(ctx)
        r = page.evaluate("async () => (await navigator.permissions.query({name:'microphone'})).state")
        page.close()
        # Native in kiosk is 'denied' or 'prompt'; MUST NOT equal our forced 'default'
        assert r in ("denied", "prompt"), f"microphone perm={r} (expected native denied/prompt)"
        return f"state={r}"

    @_t("fp:webgl_vendor_renderer_patched")
    def _():
        page = _new_test_page(ctx)
        r = page.evaluate("""() => {
            const c = document.createElement('canvas').getContext('webgl');
            if (!c) return ['none', 'none'];
            const e = c.getExtension('WEBGL_debug_renderer_info');
            if (!e) return ['no-ext', 'no-ext'];
            return [c.getParameter(e.UNMASKED_VENDOR_WEBGL), c.getParameter(e.UNMASKED_RENDERER_WEBGL)];
        }""")
        page.close()
        vendor, renderer = r
        assert "Intel" in vendor, f"webgl vendor={vendor} (expected contains 'Intel')"
        assert "Mesa" in renderer or "Intel" in renderer, f"webgl renderer={renderer}"
        return f"{vendor} / {renderer[:60]}"

    @_t("fp:webgl2_vendor_patched_too")
    def _():
        page = _new_test_page(ctx)
        r = page.evaluate("""() => {
            const c = document.createElement('canvas').getContext('webgl2');
            if (!c) return 'none';
            const e = c.getExtension('WEBGL_debug_renderer_info');
            return e ? c.getParameter(e.UNMASKED_VENDOR_WEBGL) : 'no-ext';
        }""")
        page.close()
        assert "Intel" in r, f"webgl2 vendor={r}"
        return r

    @_t("fp:no_cdc_or_webdriver_vars")
    def _():
        page = _new_test_page(ctx)
        r = page.evaluate("""() => Object.keys(window).concat(Object.keys(document))
            .filter(k => /^cdc_|__webdriver|_phantom|callPhantom|__nightmare/.test(k))""")
        page.close()
        assert r == [], f"found automation leak keys: {r}"
        return "no leaks"

    @_t("fp:chrome_object_present")
    def _():
        page = _new_test_page(ctx)
        r = page.evaluate("() => [typeof window.chrome, window.chrome && Object.keys(window.chrome).slice(0,6)]")
        page.close()
        assert r[0] == "object", f"window.chrome missing: {r}"
        return f"keys={r[1]}"

    @_t("fp:multi_engine_consistency")
    def _():
        """CRITICAL: no contradictions. Accept-Language header, nav.language,
        timezone and platform/UA all must point to the same identity."""
        page = _new_test_page(ctx)
        seen_headers = {}
        def on_request(req):
            if req.frame == page.main_frame and req.resource_type == "document":
                seen_headers.update(req.headers)
        page.on("request", on_request)
        page.goto("https://httpbin.org/headers", wait_until="domcontentloaded", timeout=30000)
        try:
            body_h = page.evaluate("() => JSON.parse(document.body.innerText).headers")
        except Exception:
            body_h = {}
        js = page.evaluate("""() => ({
            lang: navigator.language,
            tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
            platform: navigator.platform,
            uaHasLinux: /Linux/.test(navigator.userAgent),
        })""")
        accept_lang = body_h.get("Accept-Language", seen_headers.get("accept-language", "?"))
        page.close()
        assert js["lang"] == "it-IT", f"nav.lang={js['lang']}"
        assert js["tz"] == "Europe/Rome", f"tz={js['tz']}"
        assert "it" in accept_lang.lower(), f"Accept-Language mismatches lang: {accept_lang}"
        assert js["uaHasLinux"] and "Linux" in js["platform"], (
            f"UA/platform mismatch: platform={js['platform']} UA linux={js['uaHasLinux']}")
        return f"lang=it-IT tz=Europe/Rome Accept-Language={accept_lang}"


# ─── TESTS: regression guard for the exact bug just fixed ─────────────────────

def _run_regression_tests(ac, ctx):
    print("\n== REGRESSION ==")

    @_t("regression:xdotool_type_args_1_present")
    def _():
        """Every chained `type` must carry `--args 1` or xdotool consumes
        subsequent sleep/type args as literal text."""
        captured = []
        orig = ac.docker_exec
        def _cap(*a):
            captured.append(a)
            class R: stdout=""; stderr=""; returncode=0
            return R()
        ac.docker_exec = _cap
        ac._human_type("hello world test case")
        ac.docker_exec = orig
        big = captured[0]
        type_positions = [i for i, a in enumerate(big) if a == "type"]
        assert type_positions, "no `type` subcommand emitted"
        for i in type_positions:
            window = big[i:i+4]
            assert "--args" in window, (
                f"`type` at index {i} missing --args: {window!r}")
        return f"{len(type_positions)} type calls, all with --args"

    @_t("regression:no_literal_sleep_in_field")
    def _():
        """Even if the chain parser changes behaviour again, the text
        'sleep0' must never appear in a field we typed into."""
        page = _new_test_page(ctx)
        _focused_textarea(page)
        for seed in range(5):
            page.evaluate("document.getElementById('t').value=''; document.getElementById('t').focus()")
            page.wait_for_timeout(150)
            random.seed(seed)
            ac._human_type("Benchmark string for regression check")
            page.wait_for_timeout(600)
            got = _read_textarea(page)
            assert "sleep0" not in got and "--clearmodifiers" not in got, (
                f"xdotool args leaked into field: {got!r}")
        page.close()
        return "5 runs, no args leaked"


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"Target: container={CONTAINER}  cdp={CDP_URL}")
    _ensure_test_container()
    _cleanup_strays()

    import agent_core as ac   # noqa: E402  (needs the container running)
    from pw_bridge import _STEALTH_JS  # noqa: E402
    from playwright.sync_api import sync_playwright  # noqa: E402

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP_URL)
        _close_extra_tabs(b)
        for ctx in b.contexts:
            try:
                ctx.add_init_script(_STEALTH_JS)
            except Exception:
                pass
        ctx = b.contexts[0]

        try:
            _run_typing_tests(ac, ctx)
            _run_mouse_tests(ac, ctx)
            _run_fingerprint_tests(ctx, _STEALTH_JS)
            _run_regression_tests(ac, ctx)
        finally:
            # Always clean up on exit so a subsequent run isn't poisoned by
            # a half-finished xdotool chain.
            _cleanup_strays()

    total = len(_RESULTS)
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    failed = total - passed
    print(f"\n== SUMMARY ==  {passed}/{total} passed, {failed} failed")
    if failed:
        print("\nFailed:")
        for name, ok, detail in _RESULTS:
            if not ok:
                print(f"  - {name}: {detail}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
