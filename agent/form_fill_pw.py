"""Playwright-backed batch form-filling: extract -> decide -> apply.

Same pipeline and the same proven decision logic as form_fill.py (it is
reused, not copied); what changes is everything that touches the browser:

  * Extraction runs in EVERY frame, not just the top document -- surveys
    embedded in an iframe (common for Qualtrics-style wrappers) were
    invisible to the cdp.py version (measured: 0 of 4 fields found).
  * It always targets the tab actually on screen (pw_bridge.active_page),
    not the first tab -- after a survey opens via target=_blank, cdp.py
    kept reading the hidden dashboard tab.
  * Answers are applied with real input (CDP mouse events / InsertText),
    so events are isTrusted=true. Sites that ignore synthetic events
    rejected the cdp.py version's submissions outright.
  * Extracted elements are stored in a JS-side Map (no DOM mutation),
    so apply addresses exactly the element extraction saw without
    tripping MutationObserver-based anti-bot detection.
  * "Did the page advance?" no longer trusts a text change alone: a
    rejection message appearing used to read as success.
"""

import json
import math
import random
import re
import secrets
import time
import unicodedata

import cancel
import form_fill as base
import pw_bridge

# Reused unchanged from the cdp.py implementation.
ask_llm_for_answers = base.ask_llm_for_answers
summarize_for_agent = base.summarize_for_agent
_compute_mismatches = base._compute_mismatches
_is_question_like = base._is_question_like
MIN_FIELDS_TO_TRIGGER = base.MIN_FIELDS_TO_TRIGGER
MIN_QUESTION_LIKE_FIELDS = base.MIN_QUESTION_LIKE_FIELDS

ACTION_TIMEOUT_MS = 2500

# Human reading/typing delays run in the AGENT thread, between one-field bridge
# calls, NOT inside them -- so the bridge is never blocked for more than a
# single field's DOM work and a form of any size stays fully human-paced
# without ever hitting a bridge-call timeout. This is the per-field bridge
# timeout: one field is at most a few 2.5s actions plus some evaluate calls.
_FIELD_BRIDGE_TIMEOUT = 60.0   # real xdotool input (wheel scroll + arc + typing
#   a long answer char-by-char) runs inside the per-field bridge call, so give
#   it headroom; a radio/checkbox is still ~1-2s.

# Playwright errors that mean "the action took effect and the page then
# navigated / re-rendered", which tears down the JS execution context. For a
# form apply this is success (the control changed and the page advanced), not
# a failure, so it must not be reported as an apply error.
_NAV_ERR_MARKERS = (
    "Execution context was destroyed",
    "because of a navigation",
    "Target closed",
    "Target page, context or browser has been closed",
    "frame was detached",
    "Frame was detached",
)


def _is_nav_error(e) -> bool:
    s = str(e)
    return any(m in s for m in _NAV_ERR_MARKERS)


def _norm_opt(s) -> str:
    """Normalise an option label or model answer for tolerant comparison.
    Fixes 'option not found' when extraction captured a decorated label
    ('🅏 I agree') but the model answered the clean text ('I agree'), or vice
    versa. Leading emoji / symbol decoration is stripped BEFORE NFKC, because
    NFKC turns some of it into letters (🅏 -> 'wc'), which would defeat the
    strip; then NFKC-fold the rest, collapse whitespace, casefold."""
    s = str(s).strip()
    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isspace() or unicodedata.category(ch)[0] == "S" or ord(ch) >= 0x1F000:
            i += 1
        else:
            break
    s = unicodedata.normalize("NFKC", s[i:]).strip()
    s = re.sub(r"\s+", " ", s)
    return s.casefold()


def _match_option(answer, options: list):
    """The exact option string the answer refers to, or None. Exact match
    first, then normalised (unicode / leading symbol / whitespace / case)."""
    if answer in options:
        return answer
    na = _norm_opt(answer)
    for o in options:
        if _norm_opt(o) == na:
            return o
    return None


# How often each input path was taken -- for tests/diagnostics only.
STATS = {"fast": 0, "fallback": 0}

# Session-unique key for the JS-side element Map.  Elements are stored here
# instead of as DOM attributes (data-ff), invisible to MutationObserver.
_FF_KEY = f"__ff{secrets.token_hex(4)}"

# -- extraction (stores elements in a JS Map, zero DOM mutation) -----------

# Playwright treats a string starting with "function" as a function body,
# so every script is one arrow function with the shared helpers inside.
_TAG_EXTRACT_JS = "() => {" + base._SHARED_JS + r"""
return (function() {
  const PREFIX = %(prefix_json)s;
  const K = %(ff_key)s;
  if (!window[K]) window[K] = new Map();
  window[K].clear();
  const fields = [];
  const tag = (el, t) => window[K].set(t, el);
  // A single field's question: its own label, else the enclosing
  // question's legend / aria-labelledby. Qualtrics text-entry questions put
  // the question only in the fieldset legend; without this the model saw
  // just the id ("QR~QID6") and answered an age question with a job.
  // (Not used for choice OPTIONS -- those must not inherit the question.)
  const fieldLabel = (el) => _labelFor(el) || _groupLegend(el.closest('fieldset'))
    || _groupLegend(el.closest('[aria-labelledby]')) || null;

  _getChoiceGroups().forEach(g => {
    const id = PREFIX + g.id;
    g.els.forEach((el, i) => tag(el, id + '|' + i));
    // Rating scales often label only their two ends ("Not higher" ...
    // "Much higher") and leave each circle blank or numbered. Unlabelled
    // options become "1".."N", and the end labels are reported as
    // scale_anchors so the model knows which end means what (seen in a
    // real Qualtrics survey, where vision needed 16 steps for one such
    // question).
    // optionLabels: set for a form-less option set, where the options carry
    // no text of their own and their labels come from the matrix header.
    const raw = (g.optionLabels || g.els.map(_optionLabel)).map(l => (l || '').trim());
    const unlabeled = raw.every(l => !l || /^-?\d+$/.test(l));
    const options = unlabeled ? raw.map((l, i) => l || String(i + 1)) : raw;
    let anchors;
    if (unlabeled) {
      const box = g.els[0].closest('fieldset') || g.els[0].closest('table') ||
                  (g.els[0].parentElement && g.els[0].parentElement.parentElement);
      const q = (g.text || '').trim();
      anchors = [...new Set(((box && box.innerText) || '').split('\n').map(x => x.trim())
        .filter(x => x && x.length < 60 && !options.includes(x) && !q.includes(x)))].slice(0, 4);
    }
    const checkedIdx = g.els.map((e, i) => _isChecked(e) ? i : -1).filter(i => i >= 0);
    const checked = checkedIdx.map(i => options[i]);
    const f = { id, type: g.kind, text: g.text, options,
      current: g.kind === 'radio' ? (checked[0] || null) : checked,
      answered: checked.length > 0,
      widget: g.custom ? 'custom'
                       : (g.els[0].tagName === 'INPUT' ? 'native' : 'aria') };
    if (anchors && anchors.length) f.scale_anchors = anchors;
    fields.push(f);
  });

  _getSwitches().forEach(s => {
    const id = PREFIX + s.id;
    tag(s.el, id + '|0');
    const on = s.el.getAttribute('aria-checked') === 'true';
    fields.push({ id, type: 'checkbox', text: s.text, options: ['true', 'false'],
      current: on ? ['true'] : [], answered: true, widget: 'switch' });
  });

  _getListboxes().forEach(lb => {
    const id = PREFIX + lb.id;
    lb.els.forEach((el, i) => tag(el, id + '|' + i));
    const sel = lb.els.filter(e => e.getAttribute('aria-selected') === 'true').map(_optionLabel);
    // Qualtrics (and some other builders) mark the FIRST option as
    // aria-selected on page load just for focus/keyboard-navigation, without
    // the user having answered. Trusting that marker caused batch_fill to
    // skip age dropdowns as "already answered" and leave them empty, which
    // Qualtrics then flagged with a red validation border the model got
    // stuck on. Treat the listbox as answered only when (a) something is
    // selected, (b) it's not just the first option, and (c) the container
    // doesn't carry a validation-error marker.
    const container = lb.els[0] && lb.els[0].closest('[role=listbox]');
    const invalid = container && (container.getAttribute('aria-invalid') === 'true'
                                  || /\b(has-error|is-invalid|error|invalid)\b/i.test(container.className || ''));
    const onlyFirst = sel.length === 1
                      && lb.els.length > 1
                      && lb.els[0].getAttribute('aria-selected') === 'true';
    const answered = sel.length > 0 && !invalid && !onlyFirst;
    fields.push({ id, type: 'select', text: lb.text, options: lb.els.map(_optionLabel),
      multiple: false, current: answered ? sel[0] : null, answered: answered, widget: 'listbox' });
  });

  document.querySelectorAll('select').forEach(el => {
    if (!_visible(el) || el.disabled) return;
    const raw = el.id || el.name; if (!raw) return;
    const id = PREFIX + raw; tag(el, id);
    const selected = Array.from(el.selectedOptions).map(o => o.textContent.trim());
    fields.push({ id, type: 'select', text: fieldLabel(el) || raw,
      options: Array.from(el.options).map(o => o.textContent.trim()).filter(t => t),
      multiple: !!el.multiple, current: el.multiple ? selected : (selected[0] || null),
      answered: el.multiple ? selected.length > 0
                            : Array.from(el.selectedOptions).some(o => o.value !== ''),
      widget: 'native' });
  });

  const simple = [
    ['textarea', 'textarea'],
    ['input[type=range]', 'range'],
    ['input[type=date]', 'date'],
    ['input[type=text], input[type=email], input[type=number], input[type=tel], input:not([type])', 'text'],
  ];
  simple.forEach(([sel, type]) => {
    document.querySelectorAll(sel).forEach(el => {
      if (!_visible(el) || el.disabled) return;
      const raw = el.id || el.name; if (!raw) return;
      const id = PREFIX + raw; tag(el, id);
      const f = { id, type, text: fieldLabel(el) || raw, current: el.value, widget: 'native' };
      if (type === 'range') {
        Object.assign(f, { min: el.min, max: el.max, step: el.step });
        const initial = el.getAttribute('value');
        f.answered = initial !== null && el.value !== initial;
      } else {
        f.answered = el.value.trim() !== '';
      }
      if (type === 'date') f.format = 'YYYY-MM-DD';
      fields.push(f);
    });
  });

  return JSON.stringify({ fields, has_password: !!document.querySelector('input[type=password]') });
})();
}"""


def _frames(b):
    page = b.active_page()
    return page, [f for f in page.frames if not f.is_detached()]


def _extract(b):
    page, frames = _frames(b)
    all_fields, has_password = [], False
    fmt = {"ff_key": json.dumps(_FF_KEY)}
    for idx, fr in enumerate(frames):
        prefix = "" if fr == page.main_frame else f"f{idx}:"
        fmt["prefix_json"] = json.dumps(prefix)
        try:
            out = json.loads(fr.evaluate(_TAG_EXTRACT_JS % fmt))
        except Exception:
            continue  # frame navigated away / not scriptable -- skip it
        has_password = has_password or out["has_password"]
        for f in out["fields"]:
            f["_frame"] = idx
            all_fields.append(f)
    return {"fields": all_fields, "has_password": has_password}


def extract_fields() -> list:
    return pw_bridge.run(_extract)["fields"]


def batch_fill_verdict(fields: list, has_password: bool = None) -> tuple:
    if has_password is None:
        has_password = pw_bridge.run(_extract)["has_password"]
    if has_password:
        return False, "page contains a password field -- never batch-filled"
    # One real question is enough. Qualtrics (and most survey tools) show
    # 1-2 questions per page; the old 3-field minimum meant a real Qualtrics
    # survey never batch-filled once and fell back to vision clicking
    # throughout. What keeps consent/terms gates and login boxes out is the
    # question-like test (bare checkboxes and plain text inputs don't
    # count) plus the password check, not a field count.
    q = [f for f in fields if _is_question_like(f)]
    if len(q) < 1:
        return False, (f"{len(fields)} field(s), none of them a real question "
                       f"-- ids: {[f['id'] for f in fields]}")
    return True, f"{len(fields)} fields, {len(q)} question-like"


# -- apply (real input, trusted events) -------------------------------------

# Element lookup from the JS-side Map (no DOM attribute query).
# A node left in the Map by an earlier extraction can be DETACHED after the page
# re-renders. It stays truthy, and getBoundingClientRect() on a detached node
# returns all zeros (standard CSSOM, verified against Chromium), so the aim
# landed at (0,0): _scroll_into_view_real then read cy=0, computed a negative
# delta and wheeled the page UP ~40 ticks to the top before giving up, wrecking
# the scroll position for the remaining fields. isConnected is the DOM's own
# answer, so treat a detached node as missing: every JS below then reports "not
# found" and the caller hands that one field to the vision path, untouched.
_FIND = (f"let e=(window['{_FF_KEY}']||new Map()).get(t);"
         " if (e && !e.isConnected) e = null;")

# Get an element handle from the Map for Playwright fallback operations.
_GET_HANDLE_JS = f"(t) => (window['{_FF_KEY}']||new Map()).get(t)"

_AIM_JS = "(t) => {" + _FIND + r"""
  if (!e) return null;
  // NO scrollIntoView: the real cursor scrolls the control into view with
  // wheel events (_scroll_into_view_real), the way the vision path does.
  let target = e, r = e.getBoundingClientRect();
  const tiny = r.width < 4 || r.height < 4 || getComputedStyle(e).opacity === '0';
  if (e.tagName === 'INPUT' && tiny) {
    // Visually-hidden native input (Qualtrics-style): click its label,
    // which is what a person clicks, and which toggles the input.
    const lbl = e.closest('label') ||
      (e.id && document.querySelector('label[for="' + CSS.escape(e.id) + '"]'));
    if (lbl) { target = lbl; r = lbl.getBoundingClientRect(); }
  }
  // Aim near the left of a label (where the control/its text is), never at
  // its centre: labels can contain a nested text box ("Other: specify").
  const x = target === e ? r.left + r.width / 2 : r.left + Math.min(12, r.width / 2);
  const y = r.top + r.height / 2;
  const vh = window.innerHeight || document.documentElement.clientHeight;
  // ok = on screen AND hittable at (x,y). elementFromPoint returns null for a
  // point outside the viewport, so a field below/above the fold reports ok
  // false and the caller wheels toward it (cy gives the direction).
  const hit = document.elementFromPoint(x, y);
  const related = hit && (hit === e || hit === target || target.contains(hit) || e.contains(hit));
  const stray = hit && hit !== e && (hit.tagName === 'INPUT' || hit.tagName === 'TEXTAREA' || hit.tagName === 'SELECT');
  return { x, y, cy: y, vh, ok: !!related && !stray };
}"""

# Carries the shared helpers so the post-click verify reads a form-less
# option set the same way extraction did: _customChecked recomputes the
# group from the live DOM, which is all this has (one element, after a
# click that may have re-rendered the row).
_IS_ON_JS = "(t) => {" + base._SHARED_JS + _FIND + r"""
  if (!e) return null;
  if (e.tagName === 'INPUT') return e.checked;
  if (e.getAttribute('aria-checked') === 'true') return true;
  if (e.getAttribute('aria-pressed') === 'true') return true;
  if (e.getAttribute('aria-selected') === 'true') return true;
  if (e.getAttribute('data-selected') === 'true') return true;
  if (e.getAttribute('data-checked') === 'true') return true;
  const custom = _customChecked(e);
  if (custom !== null) return custom;
  return /\b(selected|is-selected|is-active|chosen|checked)\b/i.test(_classOf(e));
}"""

_IS_NATIVE_JS = "(t) => {" + _FIND + " return !!e && e.tagName === 'INPUT'; }"

_FOCUS_SELECT_JS = "(t) => {" + _FIND + r"""
  if (!e) return false;
  e.scrollIntoView({block: 'center'}); e.focus();
  if (typeof e.select === 'function') { e.select(); return true; }
  return false;
}"""

_VALUE_JS = "(t) => {" + _FIND + " return e ? e.value : null; }"

_SELECT_OPTIONS_JS = "(t) => {" + _FIND + " return (e && e.options) ? [...e.options].map(o => o.text) : null; }"

_SELECTED_LABEL_JS = "(t) => {" + _FIND + " return (e && e.selectedOptions && e.selectedOptions[0]) ? e.selectedOptions[0].text : null; }"

_FRAME_OFFSET_JS = "el => { const r = el.getBoundingClientRect(); return [r.left + el.clientLeft, r.top + el.clientTop]; }"


def _frame_offset(frame):
    dx = dy = 0.0
    while frame.parent_frame is not None:
        ox, oy = frame.frame_element().evaluate(_FRAME_OFFSET_JS)
        dx, dy = dx + ox, dy + oy
        frame = frame.parent_frame
    return dx, dy


# -- real OS input (xdotool), injected by agent_core ------------------------
# Batch-fill must drive the SAME real cursor the vision path uses -- xdotool:
# mouse-wheel scroll, Bézier cursor move, click, real keystrokes -- never CDP
# Input.dispatch* events or DOM writes. Behavioural fraud scoring (Qualtrics
# RelevantID, reCAPTCHA) flags a form filled with no wheel events and synthetic
# input, and that happened ONLY on batch-filled pages, never on the vision
# path. So there is NO CDP and NO DOM fallback here: a field the real path
# can't set is reported unapplied and left for the vision path (also real
# xdotool), so nothing batch-fill does is ever flaggable.
_REAL = None


def set_real_input(backend):
    """agent_core injects {move, click, wheel, type, key} (all xdotool)."""
    global _REAL
    _REAL = backend


def _scroll_into_view_real(frame, tag):
    """Bring the control on screen with REAL xdotool wheel events (like the
    vision path), never scrollIntoView. Returns the aim dict once the point is
    on screen and hittable, or None. Runs in the bridge thread."""
    aim = frame.evaluate(_AIM_JS, tag)
    for _ in range(10):
        if not aim:
            return None
        if aim["ok"]:
            return aim
        vh = aim["vh"] or 900
        # Wheel toward the control by an amount PROPORTIONAL to how far off it
        # is (~100px per tick, like the vision path): fixed 2-4 ticks couldn't
        # reach a field far down a long form within the loop. Target ~40% down
        # the viewport; stop as soon as it is on screen and hittable (ok).
        delta = aim["cy"] - vh * 0.4
        ticks = max(1, min(14, int(round(abs(delta) / 100))))
        _REAL["wheel"](delta > 0, ticks)             # delta>0: below -> scroll down
        time.sleep(random.uniform(0.12, 0.3))
        aim = frame.evaluate(_AIM_JS, tag)
    return aim if (aim and aim["ok"]) else None


def _real_click(frame, tag) -> bool:
    """Scroll the control into view and click it with the real cursor
    (xdotool arc + click). False if it couldn't be brought on screen."""
    aim = _scroll_into_view_real(frame, tag)
    if not aim:
        return False
    dx, dy = _frame_offset(frame)
    # Small jitter -- humans never hit the exact pixel.
    x = aim["x"] + dx + random.uniform(-2, 2)
    y = aim["y"] + dy + random.uniform(-2, 2)
    _REAL["move"](int(round(x)), int(round(y)))
    _REAL["click"]()
    return True


def _set_checked(frame, tag, want: bool) -> bool:
    """Set a radio/checkbox/switch to `want` with a real click. True on
    success. No DOM fallback: a miss is reported so the vision path takes over."""
    if frame.evaluate(_IS_ON_JS, tag) == want:
        return True
    for _ in range(2):
        if not _real_click(frame, tag):
            return False
        time.sleep(random.uniform(0.05, 0.15))
        if frame.evaluate(_IS_ON_JS, tag) == want:
            STATS["fast"] += 1
            return True
    return False


_LISTBOX_COMMITTED_JS = "(t) => {" + _FIND + r"""
  if (!e) return false;
  if (e.getAttribute('aria-selected') !== 'true') return false;
  const lb = e.closest('[role=listbox]');
  if (lb && (lb.getAttribute('aria-invalid') === 'true'
             || /\b(has-error|is-invalid|error|invalid)\b/i.test(lb.className || ''))) {
    return false;
  }
  return true;
}"""


def _select_option_el(frame, tag) -> bool:
    """Pick an ARIA listbox option with a real click. True on success.

    Uses _LISTBOX_COMMITTED_JS (not _IS_ON_JS) for the post-click verify:
    on Qualtrics the first option can carry aria-selected=true just from
    page-load focus, and the container keeps aria-invalid=true until a real
    change event lands. We only trust the commit when aria-selected is on
    AND the listbox has cleared its invalid marker. Each attempt also fires
    a synthetic change event on the container so builders that gate the
    required-field check on it will see one from the real click."""
    if frame.evaluate(_LISTBOX_COMMITTED_JS, tag):
        return True
    for _ in range(3):
        if not _real_click(frame, tag):
            return False
        time.sleep(random.uniform(0.08, 0.18))
        # Nudge builders that gate validation on a change/input event.
        try:
            frame.evaluate("(t) => {" + _FIND + r"""
                if (!e) return;
                const lb = e.closest('[role=listbox]');
                if (lb) {
                    lb.dispatchEvent(new Event('change', { bubbles: true }));
                    lb.dispatchEvent(new Event('input', { bubbles: true }));
                }
            }""", tag)
        except Exception:
            pass
        time.sleep(random.uniform(0.05, 0.12))
        if frame.evaluate(_LISTBOX_COMMITTED_JS, tag):
            STATS["fast"] += 1
            return True
    return False


def _set_select_native(frame, fid, match) -> bool:
    """Native <select>: focus/open it with a real click, then navigate with the
    real keyboard (Home + Down x index) and confirm with Return. No DOM write."""
    options = frame.evaluate(_SELECT_OPTIONS_JS, fid) or []
    try:
        idx = [_norm_opt(o) for o in options].index(_norm_opt(match))
    except ValueError:
        return False
    if not _real_click(frame, fid):
        return False
    time.sleep(random.uniform(0.1, 0.25))
    _REAL["key"]("Home")
    time.sleep(random.uniform(0.05, 0.12))
    if idx > 0:
        _REAL["key"]("Down", idx)
        time.sleep(random.uniform(0.05, 0.12))
    _REAL["key"]("Return")
    time.sleep(random.uniform(0.05, 0.12))
    sel = frame.evaluate(_SELECTED_LABEL_JS, fid)
    if sel is not None and _norm_opt(str(sel)) == _norm_opt(match):
        STATS["fast"] += 1
        return True
    return False


def _type_text(frame, tag, text: str) -> bool:
    """Focus a text box with a real click, clear it (Ctrl+A), and type with the
    real keyboard (xdotool, human cadence). True if the value landed. No DOM
    fill fallback.

    One retry on mismatch: xdotool occasionally drops accented chars under
    slow / newly-started Xvfb (seen in CI, where the first flake was always
    on an Italian textarea with è/ì/ù/perché). The retry waits a touch
    longer, clears again, and re-types once; still a real-input path, no DOM
    writes."""
    if not _real_click(frame, tag):
        return False
    for attempt in range(2):
        _REAL["key"]("ctrl+a")
        time.sleep(random.uniform(0.03, 0.08) if attempt == 0
                   else random.uniform(0.10, 0.18))
        _REAL["type"](text)
        time.sleep(random.uniform(0.05, 0.12) if attempt == 0
                   else random.uniform(0.20, 0.30))
        if frame.evaluate(_VALUE_JS, tag) == text:
            STATS["fast"] += 1
            return True
    return False


def _read_delay(f: dict, is_first: bool = False) -> float:
    """Lognormal per-field delay that looks human at the page level."""
    t = (f or {}).get("type")
    label = ((f or {}).get("text") or "") + " " + " ".join((f or {}).get("options") or [])
    long_q = len(label) > 160
    if t in ("radio", "select") or (f or {}).get("widget") == "listbox":
        mu, sigma = (2.8, 0.45) if long_q else (2.0, 0.35)
    elif t == "checkbox":
        mu, sigma = (2.4, 0.4) if long_q else (1.7, 0.3)
    elif t in ("text", "textarea"):
        mu, sigma = (3.8, 0.5) if long_q else (2.8, 0.4)
    elif t == "date":
        mu, sigma = 1.8, 0.35
    elif t == "range":
        mu, sigma = 1.8, 0.4
    else:
        mu, sigma = 1.4, 0.3
    delay = random.lognormvariate(math.log(mu), sigma)
    delay = max(0.4, min(delay, mu * 3))
    if is_first:
        delay += random.uniform(2.0, 5.0)
    return delay


def _apply_field_dom(fr, f: dict, fid: str, answer) -> bool:
    """Apply ONE field's answer with REAL xdotool input. Returns True if it
    landed, False if the real path couldn't set it (the caller reports it so
    the vision path takes over). No DOM/CDP writes anywhere. Runs in the bridge
    thread; may raise a nav error, which the caller classifies."""
    t = f["type"]
    if t in ("radio", "checkbox") or f.get("widget") == "listbox":
        options = f["options"]
        wanted = answer if isinstance(answer, list) else [answer]
        if t == "radio" or f.get("widget") == "listbox":
            match = _match_option(answer, options)
            if match is None:
                return False
            idx = options.index(match)
            tag = f"{fid}|{idx}"
            return (_select_option_el(fr, tag) if f.get("widget") == "listbox"
                    else _set_checked(fr, tag, True))
        if f.get("widget") == "switch":
            on = answer is True or "true" in {_norm_opt(w) for w in wanted}
            return _set_checked(fr, f"{fid}|0", on)
        wanted_norm = {_norm_opt(w) for w in wanted}
        ok = True
        for i, label in enumerate(options):
            if not _set_checked(fr, f"{fid}|{i}", _norm_opt(label) in wanted_norm):
                ok = False
        return ok
    if t == "select":
        match = _match_option(answer if not isinstance(answer, list) else answer[0],
                              f.get("options") or [])
        return _set_select_native(fr, fid, match) if match is not None else False
    if t == "date":
        if not _real_click(fr, fid):
            return False
        _REAL["key"]("ctrl+a")
        _REAL["type"](str(answer))
        time.sleep(random.uniform(0.05, 0.12))
        if fr.evaluate(_VALUE_JS, fid) == str(answer):
            STATS["fast"] += 1
            return True
        return False               # native date is finicky: leave it for vision
    if t in ("text", "textarea"):
        return _type_text(fr, fid, str(answer))
    # range (and anything else): too fiddly for reliable real input -- leave it
    # for the vision path rather than touch the DOM.
    return False


def _apply_field(b, fields_by_id: dict, fid: str, answer):
    """Bridge worker: apply one field with real xdotool input. Returns
    {applied} / {error} / {nav} -- never raises. An {error} (including a real
    input that couldn't set the field) leaves it for the vision path."""
    page, frames = _frames(b)
    f = fields_by_id.get(fid)
    if f is None:
        return {"error": "unknown field id"}
    try:
        fr = frames[f["_frame"]]
    except (IndexError, KeyError):
        return {"nav": True}       # the frame is gone — the page navigated
    try:
        if _apply_field_dom(fr, f, fid, answer):
            return {"applied": True}
        return {"error": "real input could not set this field"}
    except Exception as e:
        if _is_nav_error(e):
            return {"nav": True}
        return {"error": str(e).splitlines()[0][:200]}


def apply_answers(fields: list, answers: dict) -> dict:
    """Apply every answer, ONE field per bridge call, with the human reading
    and thinking delays running in THIS (agent) thread between calls -- never
    inside a bridge call. So the Playwright bridge is only ever held for a
    single field's DOM work: a form of any size stays fully human-paced and
    no call can time out. Choice fields go first, then free-text (a choice can
    reveal a dependent text field). A field whose action navigates the page
    (nav) ends the pass -- the remaining fields no longer exist on it."""
    by_id = {f["id"]: f for f in fields}
    choice_items, free_items = [], []
    for fid, answer in answers.items():
        f = by_id.get(fid)
        is_free = f is not None and f["type"] in ("text", "textarea", "date", "range")
        (free_items if is_free else choice_items).append((fid, answer))

    def _human_sleep(seconds: float):
        try:
            cancel.sleep(seconds)
        except cancel.Cancelled:
            raise
        except Exception:
            pass

    applied, errors = {}, {}
    field_idx = 0
    next_pause_at = random.randint(3, 5)
    for fid, answer in choice_items + free_items:
        f = by_id.get(fid)
        # Human pacing happens HERE, bridge free: a 'thinking' pause every few
        # fields, then a per-field reading delay (longer for the first field
        # and for long questions). Fully human at any form size.
        if field_idx > 0 and field_idx >= next_pause_at:
            _human_sleep(random.uniform(1.5, 4.0))
            next_pause_at = field_idx + random.randint(3, 5)
        _human_sleep(_read_delay(f, is_first=(field_idx == 0)))
        field_idx += 1
        res = pw_bridge.run(_apply_field, by_id, fid, answer,
                            timeout=_FIELD_BRIDGE_TIMEOUT)
        if res.get("applied"):
            applied[fid] = True
        if res.get("error"):
            errors[fid] = res["error"]
        if res.get("nav"):
            applied[fid] = True
            break
    return {"applied": applied, "errors": errors}


# -- verify (kept for bench/test tools; no longer called in production) ----

_VERIFY_JS = ("(spec) => {" + base._SHARED_JS + r"""
return ((spec) => {
  const m = window[%(ff_key)s] || new Map();
  const out = {};
  for (const f of spec) {
    if (f.kind === 'group') {
      const els = [];
      for (const [k, el] of m) {
        if (k.startsWith(f.id + '|')) els.push([+k.split('|').pop(), el]);
      }
      els.sort((a, b) => a[0] - b[0]);
      if (!els.length) { out[f.id] = null; continue; }
      const idx = els.map((pair, i) => (f.listbox ? pair[1].getAttribute('aria-selected') === 'true'
                                                  : _isChecked(pair[1])) ? i : -1).filter(i => i >= 0);
      out[f.id] = {__idx: idx};
    } else {
      const el = m.get(f.id);
      if (!el) { out[f.id] = null; continue; }
      if (el.tagName === 'SELECT') {
        const s = Array.from(el.selectedOptions).map(o => o.textContent.trim());
        out[f.id] = el.multiple ? s : (s[0] || null);
      } else out[f.id] = el.value;
    }
  }
  return JSON.stringify(out);
})(spec);
}""") % {"ff_key": json.dumps(_FF_KEY)}


def _verify(b, fields: list):
    page, frames = _frames(b)
    result = {}
    for idx, fr in enumerate(frames):
        spec = []
        for f in fields:
            if f["_frame"] != idx:
                continue
            group = f["type"] in ("radio", "checkbox") or f.get("widget") == "listbox"
            spec.append({"id": f["id"], "kind": "group" if group else "single",
                         "single": f["type"] == "radio" or f.get("widget") == "listbox",
                         "listbox": f.get("widget") == "listbox",
                         "switch": f.get("widget") == "switch"})
        if spec:
            try:
                raw = json.loads(fr.evaluate(_VERIFY_JS, spec))
                for fid, val in raw.items():
                    if isinstance(val, dict) and "__idx" in val:
                        # Map positions back through the SAME option names
                        # extraction produced (incl. "1".."N" for blank
                        # scale circles), so intended/actual compare equal.
                        f = next(x for x in fields if x["id"] == fid)
                        names = [f["options"][i] for i in val["__idx"] if i < len(f["options"])]
                        if f.get("widget") == "switch":
                            val = ["true"] if names else []
                        elif f["type"] == "radio" or f.get("widget") == "listbox":
                            val = names[0] if names else None
                        else:
                            val = names
                    result[fid] = val
            except Exception:
                for s in spec:
                    result[s["id"]] = None
    return result


def verify_fields(fields: list) -> dict:
    return pw_bridge.run(_verify, fields)


# -- orchestration ----------------------------------------------------------


# -- identity fields (Prolific ID / email): copied, never generated ---------
#
# Some studies ask the participant to type their Prolific ID and email
# right before starting. These must be exact (a wrong ID loses the credit),
# so they are never sent to the language model: values come only from
#   1. the PROLIFIC_PID parameter Prolific appends to every study URL
#      (any open tab or frame), and
#   2. identity.json, a local file the user fills once, and
#   3. the user's own task/context text (exact pattern match, not the model).
# If a value isn't known, the field is left empty and reported undecided,
# which also blocks the automatic "Next" -- the page is never submitted
# with an invented or missing ID.

import os as _os
import re as _re
from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs

IDENTITY_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "identity.json")
_PID_PARAMS = ("prolific_pid", "pid", "participant_id", "participantid", "prolificid", "prolific_id")


def _identity_kind(f: dict):
    # Both free-text widgets count. Qualtrics renders a single-line text entry
    # as <input> (extracted as "text") but a multi-line one as <textarea>
    # (extracted as "textarea"), and researchers use either for the Prolific
    # ID. Accepting only "text" meant a textarea ID field was never recognised
    # as identity, so it fell through to the model, got an INVENTED id, and
    # batch-fill auto-advanced the page: measured in run_20261106_082719,
    # where the survey recorded a bad id, showed no completion code, and the
    # study had to be closed NOCODE. The rest of this module already treats
    # ("text", "textarea") as the same free-text kind.
    if f.get("type") not in ("text", "textarea"):
        return None
    hay = f"{f.get('text','')} {f.get('id','')}".lower()
    if _re.search(r"prolific\s*[-_]?\s*(id|pid|identifier)|\bpid\b"
                  r"|participant\s*[-_]?\s*id", hay):
        return "prolific_id"
    if _re.search(r"e-?mail", hay):
        return "email"
    return None


def _pid_from_urls(b):
    for pg in b.pages():
        for fr in pg.frames:
            try:
                q = _parse_qs(_urlparse(fr.url).query)
            except Exception:
                continue
            for k, v in q.items():
                if k.lower() in _PID_PARAMS and v and _re.fullmatch(r"[0-9a-fA-F]{24}", v[0]):
                    return v[0]
    return None


def identity_values(context: str = "") -> dict:
    vals = {}
    # Lowest priority: copied verbatim from what the user wrote in the task.
    m = _re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", context or "")
    if m:
        vals["email"] = m.group(0).rstrip(".")
    m = _re.search(r"\b[0-9a-f]{24}\b", context or "")
    if m:
        vals["prolific_id"] = m.group(0)
    try:
        with open(IDENTITY_FILE) as fh:
            data = json.load(fh)
        vals.update({k: str(v).strip() for k, v in data.items() if str(v).strip()})
    except Exception:
        pass
    try:
        pid = pw_bridge.run(_pid_from_urls)
    except Exception:
        pid = None
    if pid:
        vals["prolific_id"] = pid   # the study URL is the authoritative source
    return vals


def run_batch_fill(persona: str = "", max_apply_attempts: int = 2,
                   memory: list = None) -> dict:
    """`memory` is a running list of (question, answer) pairs already given
    in the current survey; it is shown to the model (consistency across
    pages, e.g. an age given on page 2 vs a birth year asked on page 6) and
    extended with this page's answers. Each page used to be decided with no
    knowledge of the previous ones."""
    ex = pw_bridge.run(_extract)
    fields = ex["fields"]
    ok, reason = batch_fill_verdict(fields, ex["has_password"])
    identity_fields = [f for f in fields if not f.get("answered") and _identity_kind(f)]
    if not ok and not (identity_fields and not ex["has_password"]):
        return {"skipped": True, "reason": reason, "fields": fields}

    known = identity_values(persona) if identity_fields else {}
    identity_answers, identity_missing = {}, []
    for f in identity_fields:
        v = known.get(_identity_kind(f))
        if v:
            identity_answers[f["id"]] = v
        else:
            identity_missing.append(f["id"])

    unanswered = [f for f in fields if not f.get("answered") and not _identity_kind(f)]
    if not ok:
        unanswered = []          # identity-only page: nothing for the model
    # Questions about the advertisement/image/video the participant was just
    # shown are not answered here at all: this path reads the DOM, never the
    # screen, so it would be guessing -- and that is where a manipulation or
    # attention check sits. They go back as undecided, which also stops the
    # DOM auto-advance, so the vision step answers them with the page in view.
    deferred_visual = [f for f in unanswered if base.needs_eyes(f)]
    if deferred_visual:
        skip = {f["id"] for f in deferred_visual}
        unanswered = [f for f in unanswered if f["id"] not in skip]
    preserved = {f["id"]: f["current"] for f in fields if f.get("answered")}
    scaled = {f["id"]: base.scale_view_for_llm(f) for f in unanswered}
    llm_view = [{k: v for k, v in scaled[f["id"]].items() if not k.startswith("_") and k != "widget"}
                for f in unanswered]
    context = persona
    if memory:
        context += ("\n\nAnswers already given earlier in this same survey (stay "
                    "consistent with them):\n" +
                    "\n".join(f"- {q} -> {a}" for q, a in memory[-40:]))
    new_answers, undecided = ask_llm_for_answers(llm_view, persona=context) if unanswered else ({}, [])
    new_answers = {k: v for k, v in new_answers.items() if k in {f["id"] for f in unanswered}}
    new_answers = {k: base.scale_answer_back(scaled[k], v) for k, v in new_answers.items()}
    # Safety net: any question the model left out of its reply is not
    # filled and is reported as undecided -- which also stops the DOM
    # "Next" click, so the page is never submitted with a gap.
    undecided = sorted(set(undecided) | ({f["id"] for f in unanswered} - set(new_answers))
                       | set(identity_missing) | {f["id"] for f in deferred_visual})
    new_answers.update(identity_answers)

    res = apply_answers(fields, new_answers)
    apply_errors = res.get("errors", {})

    # Human-readable errors naming the QUESTION and the OPTION that did not get
    # set, so the vision path knows exactly what to click. Without this the
    # model only saw "fs:2:radio: real input could not set this field" -- a
    # positional id it cannot act on -- and, seeing an 'Other' text box that
    # WAS filled, wrongly concluded the question was answered and looped for
    # dozens of steps. For a choice field the message spells out that a filled
    # text box does not register until its option is selected.
    by_id = {f["id"]: f for f in fields}
    apply_errors_human = []
    for fid in apply_errors:
        f = by_id.get(fid, {})
        q = (f.get("text") or "").strip().replace("\n", " ")[:90] or fid
        want = new_answers.get(fid)
        want = ", ".join(map(str, want)) if isinstance(want, list) else str(want)
        if f.get("type") in ("radio", "checkbox") or f.get("widget") == "listbox":
            apply_errors_human.append(
                f"Could NOT select '{want}' for \"{q}\" -- select that option yourself by "
                "clicking it. If it is an 'Other'/'please specify' choice, the text box being "
                "filled does NOT count until you click the option itself.")
        else:
            apply_errors_human.append(
                f"Could NOT fill \"{q}\" with '{want}' -- enter it yourself.")

    if memory is not None:
        for f in fields:
            if f["id"] in new_answers and f["id"] not in apply_errors:
                memory.append((f.get("text") or f["id"], new_answers[f["id"]]))
    return {
        "skipped": False, "field_count": len(fields),
        "already_answered_count": len(preserved), "newly_answered_count": len(new_answers),
        "undecided": undecided, "answers": {**preserved, **new_answers},
        "deferred_visual": [{"id": f["id"],
                             "text": (f.get("text") or f["id"]).strip().replace("\n", " ")}
                            for f in deferred_visual],
        "apply_errors": apply_errors, "apply_errors_human": apply_errors_human,
        "mismatches": [],
        "verified_state": {}, "apply_attempts": 1,
        "frames_used": sorted({f["_frame"] for f in fields}),
    }


# -- DOM-first advance (Next/Submit) ----------------------------------------

_FIND_ADVANCE_JS = (r"""
(args) => {
  const [allow, block] = args;
  const K = %(ff_key)s;
  if (!window[K]) window[K] = new Map();
  const visible = el => !!(el.offsetParent !== null || el.getClientRects().length);
  const labelOf = el => (((el.tagName === 'INPUT' ? el.value : el.textContent) || '').trim()
                          || (el.getAttribute('aria-label') || '').trim()).replace(/\s+/g, ' ').toLowerCase();
  const c = Array.from(document.querySelectorAll(
    'button, input[type=submit], input[type=button], [role=button], a.btn, a.button'))
    .filter(el => {
      if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
      const l = labelOf(el);
      const meta = [el.id, el.name, el.getAttribute('aria-label'), el.title]
        .filter(Boolean).join(' ').toLowerCase();
      if (block.some(b => l.includes(b) || meta.includes(b))) return false;
      if (['>>', '>', '→', '»', '›', '->', '=>'].includes(l)) return true;
      if (/(^|[\s_-])next(button|[\s_-]|$)/.test(meta) || meta.startsWith('next')) return true;
      if (!l || l.length > 40) return false;
      return allow.some(a => l === a || l.startsWith(a + ' ') || l === a + '!');
    });
  if (!c.length) return null;
  const chosen = c[c.length - 1];
  window[K].set('__adv', chosen);
  return { label: labelOf(chosen), count: c.length };
}
""") % {"ff_key": json.dumps(_FF_KEY)}


_HAS_MAP_ENTRIES_JS = f"() => {{ const m = window['{_FF_KEY}']; return m ? m.size : 0; }}"


def _find_advance(b):
    page, frames = _frames(b)

    def _map_size(fr):
        try:
            return fr.evaluate(_HAS_MAP_ENTRIES_JS) or 0
        except Exception:
            return 0

    for idx, fr in sorted(enumerate(frames),
                          key=lambda t: 0 if _map_size(t[1]) else 1):
        try:
            hit = fr.evaluate(_FIND_ADVANCE_JS, [base._ADVANCE_LABELS, base._BLOCKED_LABELS])
        except Exception:
            continue
        if hit:
            return idx, hit
    return None, None


_VISIBLE_COUNT_JS = f"""() => {{
  const m = window['{_FF_KEY}'];
  if (!m) return 0;
  let n = 0;
  for (const [k, el] of m) {{
    if (k === '__adv') continue;
    try {{ if (el && (el.offsetParent !== null || el.getClientRects().length)) n++; }}
    catch(e) {{}}
  }}
  return n;
}}"""


def _snapshot(b):
    page, frames = _frames(b)
    tagged = 0
    for fr in frames:
        try:
            tagged += fr.evaluate(_VISIBLE_COUNT_JS)
        except Exception:
            pass
    lines = set()
    for fr in frames:
        try:
            txt = fr.evaluate("() => document.body ? document.body.innerText : ''")
            lines.update(l.strip() for l in txt.splitlines() if l.strip())
        except Exception:
            pass
    return {"url": page.url, "pages": len(b.pages()), "tagged_visible": tagged,
            "lines": lines}


def _click_advance(b):
    idx, hit = _find_advance(b)
    if hit is None:
        return {"attempted": False, "reason": "no forward-navigation button matched"}
    before = _snapshot(b)
    page, frames = _frames(b)
    # Click Next/Submit with the SAME real xdotool cursor the fields use, never a
    # synthetic el.click(): a page advance done by DOM click is a behavioural
    # tell on every page of a multi-page survey (no mouse movement, no real
    # event), exactly what we removed from the field inputs.
    if not _real_click(frames[idx], "__adv"):
        return {"attempted": False, "reason": "advance button not reachable with real input"}
    # Poll instead of a fixed settle sleep: a real advance (new URL, new
    # tab, fields gone) is detected within ~100ms; only a click that did
    # NOT advance (e.g. validation error) waits out the full window.
    deadline = time.time() + 1.5
    while True:
        try:
            page = b.active_page()
            page.wait_for_timeout(100)
        except Exception:
            time.sleep(0.1)
        b._sync_tabs()
        after = _snapshot(b)
        moved = (after["url"] != before["url"] or after["pages"] != before["pages"]
                 or (before["tagged_visible"] > 0
                     and after["tagged_visible"] <= before["tagged_visible"] // 2))
        if moved or time.time() > deadline:
            break
    # Advanced = we're somewhere else: new URL, a new tab, or the fields we
    # filled are gone from view. A mere text change does NOT count -- that
    # is exactly what a validation error looks like.
    # (The "fields gone" test only means something if there were tagged
    # fields to begin with -- on an un-extracted page 0 <= 0 is vacuously
    # true, which reported a rejected click as success.)
    fields_gone = (before["tagged_visible"] > 0
                   and after["tagged_visible"] <= before["tagged_visible"] // 2)
    advanced = (after["url"] != before["url"] or after["pages"] != before["pages"]
                or fields_gone)
    # What visibly changed, reported as fact rather than interpreted: a
    # same-page "thank you" and a validation error both look like "didn't
    # navigate", and telling the vision agent only "page did not change"
    # sent it into a 12-step retry loop on a form that had in fact been
    # submitted (measured). Let it read the new text and judge.
    new_text = [l for l in after["lines"] - before["lines"] if len(l) < 200][:6]
    for snap in (before, after):
        snap.pop("lines", None)
    return {"attempted": True, "advanced": advanced, "label": hit["label"],
            "candidate_count": hit["count"], "new_text": new_text,
            "before": before, "after": after}


def click_advance_button() -> dict:
    return pw_bridge.run(_click_advance)


def find_advance_button() -> dict:
    idx, hit = pw_bridge.run(_find_advance)
    return {"found": hit is not None, **(hit or {}), "frame": idx}
