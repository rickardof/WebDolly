"""Playwright-backed batch form-filling: extract -> decide -> apply -> verify.

Same pipeline and the same proven decision logic as form_fill.py (it is
reused, not copied); what changes is everything that touches the browser:

  * Extraction runs in EVERY frame, not just the top document -- surveys
    embedded in an iframe (common for Qualtrics-style wrappers) were
    invisible to the cdp.py version (measured: 0 of 4 fields found).
  * It always targets the tab actually on screen (pw_bridge.active_page),
    not the first tab -- after a survey opens via target=_blank, cdp.py
    kept reading the hidden dashboard tab.
  * Answers are applied with real input (locator.check/fill/click/
    select_option), so events are isTrusted=true. Sites that ignore
    synthetic events rejected the cdp.py version's submissions outright.
  * Each extracted element is tagged with a data-ff attribute, so apply
    and verify address exactly the element extraction saw, instead of
    re-deriving groups in a separate JS call.
  * "Did the page advance?" no longer trusts a text change alone: a
    rejection message appearing used to read as success.
"""

import json
import random
import time

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

# How often each input path was taken -- for tests/diagnostics only.
STATS = {"fast": 0, "fallback": 0}

# -- extraction (tags every element it reports) -----------------------------

# Playwright treats a string starting with "function" as a function body,
# so every script is one arrow function with the shared helpers inside.
_TAG_EXTRACT_JS = "() => {" + base._SHARED_JS + r"""
return (function() {
  const PREFIX = %(prefix_json)s;
  document.querySelectorAll('[data-ff]').forEach(e => e.removeAttribute('data-ff'));
  const fields = [];
  const tag = (el, t) => el.setAttribute('data-ff', t);
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
    for idx, fr in enumerate(frames):
        prefix = "" if fr == page.main_frame else f"f{idx}:"
        try:
            out = json.loads(fr.evaluate(_TAG_EXTRACT_JS % {"prefix_json": json.dumps(prefix)}))
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

def _q(tag: str) -> str:
    return '[data-ff="' + tag.replace("\\", "\\\\").replace('"', '\\"') + '"]'


# Fast trusted input. Measured: Playwright's locator.click() costs 100-150ms
# per click (scroll, two-frame stability wait, hit-target check, post-click
# navigation wait) -- ~3s for a 15-field form -- while the actual trusted
# event, a raw CDP Input.dispatchMouseEvent press+release, costs ~4ms. So
# clicks are aimed and hit-tested in one cheap evaluate, sent raw, and the
# result checked; only if something covers the target or the click had no
# effect does it fall back to the full (slow, careful) Playwright click.

# Element lookup by data-ff tag inside page JS. Measured: locator.evaluate()
# costs ~95ms per call (it re-resolves the selector through Playwright's
# polling machinery) versus ~3ms for a plain frame.evaluate -- and the
# first fast-path version made 2-3 locator calls per option, which is why
# it was no faster than plain locator.click(). Locators are now only used
# for the slow, careful fallback.
_FIND = "const e = Array.from(document.querySelectorAll('[data-ff]')).find(x => x.getAttribute('data-ff') === t);"

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
  return e.tagName === 'INPUT' ? e.checked
    : (e.getAttribute('aria-checked') === 'true' || e.getAttribute('aria-selected') === 'true');
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
    # Covered target or no effect: the full actionable Playwright click.
    loc = frame.locator(_q(tag))
    if frame.evaluate(_IS_NATIVE_JS, tag):
        loc.set_checked(want, timeout=ACTION_TIMEOUT_MS, force=True)
    else:
        loc.click(timeout=ACTION_TIMEOUT_MS)


def _select_option_el(session, frame, tag):
    if frame.evaluate(_IS_ON_JS, tag):
        return
    if _raw_click(session, frame, tag) and frame.evaluate(_IS_ON_JS, tag):
        STATS["fast"] += 1
        return
    STATS["fallback"] += 1
    frame.locator(_q(tag)).click(timeout=ACTION_TIMEOUT_MS)


def _type_text(session, frame, tag, text: str):
    """Replace a text box's content with human-like per-word insertion.
    Breaks text into small chunks (1-3 words) with natural pauses between,
    instead of inserting the entire string at once (which is a bot tell)."""
    if not frame.evaluate(_FOCUS_SELECT_JS, tag):
        STATS["fallback"] += 1
        frame.locator(_q(tag)).fill(text, timeout=ACTION_TIMEOUT_MS)
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
    frame.locator(_q(tag)).fill(text, timeout=ACTION_TIMEOUT_MS)


def _apply(b, fields_by_id: dict, answers: dict):
    page, frames = _frames(b)
    # Opening a CDP session costs ~86ms; keep one per page on the bridge.
    if getattr(b, "_cdp_page", None) is not page:
        b._cdp_session = page.context.new_cdp_session(page)
        b._cdp_page = page
    try:
        return _apply_with(b._cdp_session, frames, fields_by_id, answers)
    except Exception:
        b._cdp_page = None  # stale session (page navigated/closed): rebuild next time
        raise


def _apply_with(session, frames, fields_by_id: dict, answers: dict):
    applied, errors, freeform = {}, {}, []

    def frame_of(fid):
        return frames[fields_by_id[fid]["_frame"]]

    # Reading-time distribution per field type. Current 0.3-1.2s was fast
    # enough that a 10-field survey finished in ~8s where a human takes
    # 60-180s. These ranges still beat human speed but close the gap to the
    # point the overall cadence is plausible rather than machine-like.
    def _read_delay(f: dict) -> float:
        t = f.get("type")
        label = (f.get("label") or "") + " " + " ".join(f.get("options") or [])
        long_q = len(label) > 160
        if t in ("radio", "select") or f.get("widget") == "listbox":
            lo, hi = (0.9, 2.2) if long_q else (0.6, 1.5)
        elif t == "checkbox":
            lo, hi = (0.8, 1.9) if long_q else (0.5, 1.3)
        elif t in ("text", "textarea"):
            lo, hi = (1.6, 3.5) if long_q else (1.1, 2.4)
        elif t == "date":
            lo, hi = 0.6, 1.5
        elif t == "range":
            lo, hi = 0.6, 1.6
        else:
            lo, hi = 0.4, 1.1
        return random.uniform(lo, hi)

    def _reading_sleep(f: dict):
        try:
            cancel.sleep(_read_delay(f))
        except cancel.Cancelled:
            raise
        except Exception:
            pass

    for fid, answer in answers.items():
        f = fields_by_id.get(fid)
        if f is None:
            errors[fid] = "unknown field id"
            continue
        if f["type"] in ("text", "textarea", "date", "range"):
            freeform.append((fid, answer))  # after choices: they may reveal fields
            continue
        # Reading delay BEFORE each field — including the first — because a
        # human reads the first question too, they just land on the page a
        # fraction earlier (that part is covered by the agent's own dwell).
        _reading_sleep(f)
        try:
            fr = frame_of(fid)
            if f["type"] in ("radio", "checkbox") or f.get("widget") == "listbox":
                options = f["options"]
                wanted = answer if isinstance(answer, list) else [answer]
                if f["type"] == "radio" or f.get("widget") == "listbox":
                    if answer not in options:
                        errors[fid] = f"option not found: {answer}"
                        continue
                    loc = fr.locator(_q(f"{fid}|{options.index(answer)}"))
                    if f.get("widget") == "listbox":
                        _select_option_el(session, fr, f"{fid}|{options.index(answer)}")
                    else:
                        _set_checked(session, fr, f"{fid}|{options.index(answer)}", True)
                else:
                    if f.get("widget") == "switch":
                        wanted = ["true"] if (answer is True or "true" in wanted) else []
                        _set_checked(session, fr, f"{fid}|0", bool(wanted))
                    else:
                        for i, label in enumerate(options):
                            _set_checked(session, fr, f"{fid}|{i}", label in wanted)
                applied[fid] = True
            elif f["type"] == "select":
                loc = fr.locator(_q(fid))
                labels = answer if isinstance(answer, list) else [answer]
                loc.select_option(label=labels, timeout=ACTION_TIMEOUT_MS)
                applied[fid] = True
            else:
                errors[fid] = f"unsupported type {f['type']}"
        except Exception as e:
            errors[fid] = str(e).splitlines()[0][:200]

    for fid, answer in freeform:
        f = fields_by_id[fid]
        # Text fields deserve the longer reading delay too; they also need
        # composition time, which _type_text's per-char cadence already covers.
        _reading_sleep(f)
        try:
            loc = frame_of(fid).locator(_q(fid))
            if f["type"] == "range":
                # Playwright has no fill() for range; set it like a native
                # drag would end up (value + input/change events).
                frame_of(fid).evaluate("([t, v]) => {" + _FIND + """
                    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e, v);
                    e.dispatchEvent(new Event('input', {bubbles:true}));
                    e.dispatchEvent(new Event('change', {bubbles:true})); }""", [fid, str(answer)])
            elif f["type"] == "date":
                loc.fill(str(answer), timeout=ACTION_TIMEOUT_MS)
            else:
                _type_text(session, frame_of(fid), fid, str(answer))
            applied[fid] = True
        except Exception as e:
            errors[fid] = str(e).splitlines()[0][:200]
    return {"applied": applied, "errors": errors}


def apply_answers(fields: list, answers: dict) -> dict:
    by_id = {f["id"]: f for f in fields}
    return pw_bridge.run(_apply, by_id, answers)


# -- verify (DOM read-back through the same tags) ---------------------------

_VERIFY_JS = "(spec) => {" + base._SHARED_JS + r"""
return ((spec) => {
  const out = {};
  for (const f of spec) {
    if (f.kind === 'group') {
      const els = Array.from(document.querySelectorAll('[data-ff]'))
        .filter(e => e.getAttribute('data-ff').startsWith(f.id + '|'))
        .sort((a, b) => +a.getAttribute('data-ff').split('|').pop() - +b.getAttribute('data-ff').split('|').pop());
      if (!els.length) { out[f.id] = null; continue; }
      const idx = els.map((e, i) => (f.listbox ? e.getAttribute('aria-selected') === 'true'
                                                : _isChecked(e)) ? i : -1).filter(i => i >= 0);
      out[f.id] = {__idx: idx};
    } else {
      const el = Array.from(document.querySelectorAll('[data-ff]'))
        .find(e => e.getAttribute('data-ff') === f.id);
      if (!el) { out[f.id] = null; continue; }
      if (el.tagName === 'SELECT') {
        const s = Array.from(el.selectedOptions).map(o => o.textContent.trim());
        out[f.id] = el.multiple ? s : (s[0] || null);
      } else out[f.id] = el.value;
    }
  }
  return JSON.stringify(out);
})(spec);
}"""


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

    apply_errors, verify_result, mismatches, attempts = {}, dict(preserved), [], 0
    pending = dict(new_answers)
    while pending and attempts < max_apply_attempts:
        attempts += 1
        res = apply_answers(fields, pending)
        apply_errors.update(res["errors"])
        verify_result.update(verify_fields([f for f in fields if f["id"] in pending]))
        mismatches = _compute_mismatches(fields, new_answers, verify_result)
        retry = {m["id"] for m in mismatches} - set(apply_errors)
        pending = {k: v for k, v in new_answers.items() if k in retry}

    if memory is not None:
        for f in fields:
            if f["id"] in new_answers and f["id"] not in apply_errors:
                memory.append((f.get("text") or f["id"], new_answers[f["id"]]))
    return {
        "skipped": False, "field_count": len(fields),
        "already_answered_count": len(preserved), "newly_answered_count": len(new_answers),
        "undecided": undecided, "answers": {**preserved, **new_answers},
        "apply_errors": apply_errors, "mismatches": mismatches,
        "verified_state": verify_result, "apply_attempts": attempts,
        "frames_used": sorted({f["_frame"] for f in fields}),
    }


# -- DOM-first advance (Next/Submit) ----------------------------------------

_FIND_ADVANCE_JS = r"""
(args) => {
  const [allow, block] = args;
  document.querySelectorAll('[data-ff-adv]').forEach(e => e.removeAttribute('data-ff-adv'));
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
      // Survey tools often label "next" with just an arrow (Qualtrics
      // classic: <input id="NextButton" value=">>">), so arrows and a
      // next-ish id/aria-label count too.
      if (['>>', '>', '→', '»', '›', '->', '=>'].includes(l)) return true;
      if (/(^|[\s_-])next(button|[\s_-]|$)/.test(meta) || meta.startsWith('next')) return true;
      if (!l || l.length > 40) return false;
      return allow.some(a => l === a || l.startsWith(a + ' ') || l === a + '!');
    });
  if (!c.length) return null;
  const chosen = c[c.length - 1];  // advance control is conventionally last
  chosen.setAttribute('data-ff-adv', '1');
  return { label: labelOf(chosen), count: c.length };
}
"""


def _find_advance(b):
    page, frames = _frames(b)
    # Prefer frames that hold the form we just filled (the iframe's own
    # Next button, not the wrapper page's), then the rest.
    for idx, fr in sorted(enumerate(frames),
                          key=lambda t: 0 if t[1].locator("[data-ff]").count() else 1):
        try:
            hit = fr.evaluate(_FIND_ADVANCE_JS, [base._ADVANCE_LABELS, base._BLOCKED_LABELS])
        except Exception:
            continue
        if hit:
            return idx, hit
    return None, None


def _snapshot(b):
    page, frames = _frames(b)
    tagged = 0
    for fr in frames:
        try:
            tagged += fr.locator("[data-ff]:visible").count()
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
    frames[idx].locator("[data-ff-adv]").click(timeout=ACTION_TIMEOUT_MS)
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
