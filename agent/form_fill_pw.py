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
_FIELD_BRIDGE_TIMEOUT = 30.0

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
    const raw = g.els.map(_optionLabel).map(l => (l || '').trim());
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
      answered: checked.length > 0, widget: g.els[0].tagName === 'INPUT' ? 'native' : 'aria' };
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
    fields.push({ id, type: 'select', text: lb.text, options: lb.els.map(_optionLabel),
      multiple: false, current: sel[0] || null, answered: sel.length > 0, widget: 'listbox' });
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
_FIND = f"const e=(window['{_FF_KEY}']||new Map()).get(t);"

# Get an element handle from the Map for Playwright fallback operations.
_GET_HANDLE_JS = f"(t) => (window['{_FF_KEY}']||new Map()).get(t)"

_AIM_JS = "(t) => {" + _FIND + r"""
  if (!e) return null;
  e.scrollIntoView({block: 'center', inline: 'center'});
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
  const hit = document.elementFromPoint(x, y);
  const related = hit && (hit === e || hit === target || target.contains(hit) || e.contains(hit));
  const stray = hit && hit !== e && (hit.tagName === 'INPUT' || hit.tagName === 'TEXTAREA' || hit.tagName === 'SELECT');
  return { x, y, ok: !!related && !stray };
}"""

_IS_ON_JS = "(t) => {" + _FIND + r"""
  if (!e) return null;
  if (e.tagName === 'INPUT') return e.checked;
  if (e.getAttribute('aria-checked') === 'true') return true;
  if (e.getAttribute('aria-pressed') === 'true') return true;
  if (e.getAttribute('aria-selected') === 'true') return true;
  if (e.getAttribute('data-selected') === 'true') return true;
  if (e.getAttribute('data-checked') === 'true') return true;
  return /\b(selected|is-selected|is-active|chosen|checked)\b/i.test(e.className || '');
}"""

_IS_NATIVE_JS = "(t) => {" + _FIND + " return !!e && e.tagName === 'INPUT'; }"

_FOCUS_SELECT_JS = "(t) => {" + _FIND + r"""
  if (!e) return false;
  e.scrollIntoView({block: 'center'}); e.focus();
  if (typeof e.select === 'function') { e.select(); return true; }
  return false;
}"""

_VALUE_JS = "(t) => {" + _FIND + " return e ? e.value : null; }"

_FRAME_OFFSET_JS = "el => { const r = el.getBoundingClientRect(); return [r.left + el.clientLeft, r.top + el.clientTop]; }"


def _frame_offset(frame):
    dx = dy = 0.0
    while frame.parent_frame is not None:
        ox, oy = frame.frame_element().evaluate(_FRAME_OFFSET_JS)
        dx, dy = dx + ox, dy + oy
        frame = frame.parent_frame
    return dx, dy


def _raw_click(session, frame, tag) -> bool:
    aim = frame.evaluate(_AIM_JS, tag)
    if not aim or not aim["ok"]:
        return False
    dx, dy = _frame_offset(frame)
    x, y = aim["x"] + dx, aim["y"] + dy
    # Small jitter on click coordinates (humans never hit the exact pixel)
    x += random.uniform(-2, 2)
    y += random.uniform(-2, 2)
    session.send("Input.dispatchMouseEvent",
                 {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    # Brief hold between press and release (50-120ms, like a real click)
    time.sleep(random.uniform(0.05, 0.12))
    session.send("Input.dispatchMouseEvent",
                 {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
    return True


def _set_checked(session, frame, tag, want: bool):
    if frame.evaluate(_IS_ON_JS, tag) == want:
        return
    if _raw_click(session, frame, tag) and frame.evaluate(_IS_ON_JS, tag) == want:
        STATS["fast"] += 1
        return
    STATS["fallback"] += 1
    handle = frame.evaluate_handle(_GET_HANDLE_JS, tag)
    el = handle.as_element()
    if el:
        el.click(force=True, timeout=ACTION_TIMEOUT_MS)


def _select_option_el(session, frame, tag):
    if frame.evaluate(_IS_ON_JS, tag):
        return
    if _raw_click(session, frame, tag) and frame.evaluate(_IS_ON_JS, tag):
        STATS["fast"] += 1
        return
    STATS["fallback"] += 1
    handle = frame.evaluate_handle(_GET_HANDLE_JS, tag)
    el = handle.as_element()
    if el:
        el.click(timeout=ACTION_TIMEOUT_MS)


def _type_text(session, frame, tag, text: str):
    """Replace a text box's content with human-like per-word insertion."""
    if not frame.evaluate(_FOCUS_SELECT_JS, tag):
        STATS["fallback"] += 1
        handle = frame.evaluate_handle(_GET_HANDLE_JS, tag)
        el = handle.as_element()
        if el:
            el.fill(text, timeout=ACTION_TIMEOUT_MS)
        return
    # Short strings (codes, numbers): just insert directly
    if len(text) <= 12:
        session.send("Input.insertText", {"text": text})
    else:
        # Break into word-level chunks for natural cadence
        words = text.split(" ")
        i = 0
        while i < len(words):
            chunk_size = random.randint(1, 3)
            chunk = " ".join(words[i:i + chunk_size])
            if i > 0:
                chunk = " " + chunk
            session.send("Input.insertText", {"text": chunk})
            i += chunk_size
            if i < len(words):
                try:
                    cancel.sleep(random.uniform(0.05, 0.25))
                except Exception:
                    pass
    if frame.evaluate(_VALUE_JS, tag) == text:
        STATS["fast"] += 1
        return
    STATS["fallback"] += 1
    handle = frame.evaluate_handle(_GET_HANDLE_JS, tag)
    el = handle.as_element()
    if el:
        el.fill(text, timeout=ACTION_TIMEOUT_MS)


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


def _apply_field_dom(session, fr, f: dict, fid: str, answer):
    """Apply ONE field's answer to the DOM. Runs inside a bridge call. Raises
    on failure (the caller classifies nav errors vs real errors)."""
    t = f["type"]
    if t in ("radio", "checkbox") or f.get("widget") == "listbox":
        options = f["options"]
        wanted = answer if isinstance(answer, list) else [answer]
        if t == "radio" or f.get("widget") == "listbox":
            match = _match_option(answer, options)
            if match is None:
                raise ValueError(f"option not found: {answer}")
            idx = options.index(match)
            if f.get("widget") == "listbox":
                _select_option_el(session, fr, f"{fid}|{idx}")
            else:
                _set_checked(session, fr, f"{fid}|{idx}", True)
        elif f.get("widget") == "switch":
            on = answer is True or "true" in {_norm_opt(w) for w in wanted}
            _set_checked(session, fr, f"{fid}|0", on)
        else:
            wanted_norm = {_norm_opt(w) for w in wanted}
            for i, label in enumerate(options):
                _set_checked(session, fr, f"{fid}|{i}", _norm_opt(label) in wanted_norm)
    elif t == "select":
        handle = fr.evaluate_handle(_GET_HANDLE_JS, fid)
        el = handle.as_element()
        labels = answer if isinstance(answer, list) else [answer]
        el.select_option(label=labels, timeout=ACTION_TIMEOUT_MS)
    elif t == "range":
        fr.evaluate("([t, v]) => {" + _FIND + """
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e, v);
            e.dispatchEvent(new Event('input', {bubbles:true}));
            e.dispatchEvent(new Event('change', {bubbles:true})); }""", [fid, str(answer)])
    elif t == "date":
        handle = fr.evaluate_handle(_GET_HANDLE_JS, fid)
        el = handle.as_element()
        if el:
            el.fill(str(answer), timeout=ACTION_TIMEOUT_MS)
    elif t in ("text", "textarea"):
        _type_text(session, fr, fid, str(answer))
    else:
        raise ValueError(f"unsupported type {t}")


def _apply_field(b, fields_by_id: dict, fid: str, answer):
    """Bridge worker: set up (cached) CDP session + frames and apply one
    field. Returns {applied} / {error} / {nav} -- never raises -- so the
    orchestrator can keep going (or stop on navigation) per field."""
    page, frames = _frames(b)
    # Opening a CDP session costs ~86ms; keep one per page on the bridge.
    if getattr(b, "_cdp_page", None) is not page:
        b._cdp_session = page.context.new_cdp_session(page)
        b._cdp_page = page
    f = fields_by_id.get(fid)
    if f is None:
        return {"error": "unknown field id"}
    try:
        fr = frames[f["_frame"]]
    except (IndexError, KeyError):
        return {"nav": True}       # the frame is gone — the page navigated
    try:
        _apply_field_dom(b._cdp_session, fr, f, fid, answer)
        return {"applied": True}
    except Exception as e:
        if _is_nav_error(e):
            b._cdp_page = None     # session is stale after a navigation
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
    if f.get("type") not in ("text",):
        return None
    hay = f"{f.get('text','')} {f.get('id','')}".lower()
    if _re.search(r"prolific\s*[-_]?\s*(id|pid)|\bpid\b|participant\s*[-_]?\s*id", hay):
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
                       | set(identity_missing))
    new_answers.update(identity_answers)

    res = apply_answers(fields, new_answers)
    apply_errors = res.get("errors", {})

    if memory is not None:
        for f in fields:
            if f["id"] in new_answers and f["id"] not in apply_errors:
                memory.append((f.get("text") or f["id"], new_answers[f["id"]]))
    return {
        "skipped": False, "field_count": len(fields),
        "already_answered_count": len(preserved), "newly_answered_count": len(new_answers),
        "undecided": undecided, "answers": {**preserved, **new_answers},
        "apply_errors": apply_errors, "mismatches": [],
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
    handle = frames[idx].evaluate_handle(_GET_HANDLE_JS, "__adv")
    el = handle.as_element()
    if not el:
        return {"attempted": False, "reason": "advance button lost from Map"}
    el.click(timeout=ACTION_TIMEOUT_MS)
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
