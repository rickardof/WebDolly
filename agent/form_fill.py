#!/usr/bin/env python3
"""DOM-based batch form-filling: extract -> decide -> apply -> verify.

Standalone module, driven via cdp.py (docker exec into the browser
container). Deliberately does NOT touch Fara's CUA conversation -- the
decision call here is a separate, stateless side-request, so it never
pollutes agent_core's message history or its prompt-cache prefix.

Usage (standalone test): python3 form_fill.py [fixture.html]
Library usage: from form_fill import run_batch_fill
"""

import json
import os
import cancel
import re
import subprocess
import urllib.request

LLAMA_URL = os.environ.get("FARA_URL", "http://127.0.0.1:8090/v1/chat/completions")

# Guardrail: what counts as "a form worth batch-filling". A bare field
# count is a poor discriminator -- it was set to 5 to keep consent modals
# and login boxes out, but that also excluded a legitimate 4-question
# survey page (observed: page 2 of a real two-page flow fell back to the
# slow per-field vision path for no good reason). The rule below is both
# narrower and more useful: it ignores credential forms outright, and
# requires actual QUESTIONS (a choice group with real options, a
# textarea, a multi-option select) rather than counting bare checkboxes,
# which is what a cookie banner or a terms-acceptance gate is made of.
MIN_FIELDS_TO_TRIGGER = 3
MIN_QUESTION_LIKE_FIELDS = 2

# -- low-level cdp plumbing ---------------------------------------------

def cdp_eval(js_expr: str):
    """Runs a JS expression inside the browser container and returns its
    parsed JSON value. The expression MUST return a JSON.stringify'd string
    -- CDP only hands back primitives by value; bare objects come back as
    an opaque remote objectId with no usable value field.
    """
    result = subprocess.run(
        ["docker", "exec", os.environ.get("FARA_CONTAINER", "fara-browser-pw"), "cdp.py", "eval", js_expr],
        capture_output=True, text=True, timeout=20, check=True,
    )
    resp = json.loads(result.stdout)
    if "error" in resp:
        raise RuntimeError(f"CDP eval error: {resp['error']}")
    value = resp["result"]["result"].get("value")
    if value is None:
        raise RuntimeError(f"CDP eval returned no value (not stringified?): {resp}")
    return json.loads(value)


# -- extraction -----------------------------------------------------------
#
# Choice-type grouping (radio/checkbox-like widgets, where several
# elements form ONE question) is the hard part, and is shared verbatim by
# extraction, apply, and verify (via _SHARED_JS below) -- each is a
# separate cdp_eval() call with no shared JS state, so if extraction and
# apply computed groups independently they could silently drift apart.
# Running the exact same _getChoiceGroups() in all three guarantees a
# field id extracted now still resolves to the same elements later.
#
# Two sources of groups are combined:
#  1. Native <input type=radio/checkbox>, grouped by the nearest <fieldset>
#     first. This was a real bug found against a Qualtrics-style
#     reconstruction: Qualtrics gives each checkbox OPTION its own unique
#     name/id (one native input per option), so grouping by a shared
#     `name` (the original approach) treated every option as its own
#     separate one-choice question. <fieldset>+<legend> is the actual
#     HTML-standard mechanism for grouping controls under one question
#     regardless of their individual names, so it's checked first; `name`
#     grouping is only the fallback for inputs with no fieldset ancestor.
#  2. ARIA widget patterns (WAI-ARIA Authoring Practices), for sites whose
#     choice controls aren't native <input> elements at all: role=radio
#     grouped by the nearest role=radiogroup; role=checkbox grouped by the
#     nearest role=group, or by shared parent as a fallback; role=switch
#     as a standalone boolean; role=listbox/option as a custom dropdown.
#  ARIA role=slider is deliberately NOT supported: setting an arbitrary
#  value reliably would need drag or arrow-key stepping with an assumed
#  step size, which isn't something that can be done with confidence
#  across arbitrary widget implementations -- better to leave it to
#  Fara's normal vision-driven interaction than fake support for it.

_SHARED_JS = r"""
function _visible(el) {
  return !!(el.offsetParent !== null || el.getClientRects().length);
}
function _textOf(el) {
  const clone = el.cloneNode(true);
  clone.querySelectorAll('input,select,textarea,[role]').forEach(n => n.remove());
  return clone.textContent.trim();
}
function _labelFor(el) {
  if (el.id) {
    const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (lbl && lbl.textContent.trim()) return lbl.textContent.trim();
  }
  const parentLabel = el.closest('label');
  if (parentLabel) {
    const clone = parentLabel.cloneNode(true);
    clone.querySelectorAll('input,select,textarea').forEach(n => n.remove());
    const t = clone.textContent.trim();
    if (t) return t;
  }
  if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
  if (el.placeholder) return el.placeholder.trim();
  const t = _textOf(el);
  if (t) return t;
  return null;
}
function _groupLegend(container) {
  if (!container) return null;
  if (container.tagName === 'FIELDSET') {
    const legend = container.querySelector('legend');
    if (legend && legend.textContent.trim()) return legend.textContent.trim();
  }
  const labelledBy = container.getAttribute && container.getAttribute('aria-labelledby');
  if (labelledBy) {
    const text = labelledBy.split(/\s+/).map(id => {
      const e = document.getElementById(id);
      return e ? e.textContent.trim() : '';
    }).join(' ').trim();
    if (text) return text;
  }
  const label = container.getAttribute && container.getAttribute('aria-label');
  if (label && label.trim()) return label.trim();
  return null;
}
function _precedingHeadingText(el) {
  let node = el;
  while (node) {
    let sib = node.previousElementSibling;
    while (sib) {
      const t = sib.textContent.trim();
      if (t && t.length < 300) return t;
      sib = sib.previousElementSibling;
    }
    node = node.parentElement;
    if (!node || node.tagName === 'BODY') break;
  }
  return null;
}
function _isChecked(el) {
  if (el.tagName === 'INPUT') return el.checked;
  if (el.getAttribute('aria-checked') === 'true') return true;
  if (el.getAttribute('aria-pressed') === 'true') return true;
  if (el.getAttribute('aria-selected') === 'true') return true;
  if (el.getAttribute('data-selected') === 'true') return true;
  if (el.getAttribute('data-checked') === 'true') return true;
  // A form-less option whose group says which one is chosen (see
  // _customChecked): asked before the class-name guess below, because these
  // widgets spell their state with a hashed class that means nothing.
  const custom = _customChecked(el);
  if (custom !== null) return custom;
  // Custom-widget selected markers: "selected", "is-selected", "chosen",
  // "checked", "is-active". `active` alone deliberately excluded -- too
  // many unrelated navs use it. Only applied to non-input elements, which
  // are already group members (verified by _getChoiceGroups/_getButtonGroups).
  return /\b(selected|is-selected|is-active|chosen|checked)\b/i.test(_classOf(el));
}

// -- choice groups with no form semantics whatsoever ----------------------
//
// Modern survey builders render a Likert scale as styled <div>s: no input,
// no role, no label text, and a chosen state expressed only by a hashed CSS
// class (emotion / styled-components). run_20261109_165350 is the page this
// was built on: www.surveycake.com, 1031 nodes, 0 <input>, 0 [role=radio],
// 0 <fieldset>, and 91 consecutive batch-fill peeks that found nothing while
// the model clicked one circle per step at ~12s each.
//
// _getButtonGroups cannot see these: it wants a button/role/tabindex,
// non-empty text, and a chosen marker spelled in words. All three absent.
//
// The signature below is deliberately narrow. Anything it cannot read for
// certain is dropped and left to the vision path, because the cost of
// silence is a slow page while the cost of a wrong read is overwriting an
// answer the participant already gave.
// var, not const: this shared block is evaluated as a TOP-LEVEL script on
// every cdp_eval (_EXTRACT_JS, _APPLY_JS_TEMPLATE, ...), and a top-level
// const throws "has already been declared" the second time round. Every
// other constant in here lives inside a function for the same reason.
var _CUSTOM_OPT_ATTR_RE = /^data-[a-z0-9-]*(option|choice|answer|radio|checkbox)/i;
var _MIN_CUSTOM_OPTIONS = 3;

// className is a string on HTML elements and an SVGAnimatedString on SVG ones.
function _classOf(el) {
  return typeof el.className === 'string' ? el.className.trim() : '';
}

// The data-* attribute by which this element identifies itself as one
// choice among several, or null. Native and ARIA widgets are excluded:
// they are already covered, and better, above.
function _customOptionAttr(el) {
  const tag = el.tagName;
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' || tag === 'OPTION') return null;
  const role = el.getAttribute('role');
  if (role && /^(radio|checkbox|option|switch|slider)$/i.test(role)) return null;
  for (const a of el.attributes) {
    if (_CUSTOM_OPT_ATTR_RE.test(a.name) && (a.value || '').trim()) return a.name;
  }
  return null;
}

// The nearest ancestor holding a whole option set. In a matrix that is the
// <tr> of one statement, never the <table> (which holds every statement's
// options at once), because the walk stops at the FIRST ancestor that has
// enough of them.
function _customGroupAnchor(el, attr) {
  const sel = '[' + CSS.escape(attr) + ']';
  for (let p = el.parentElement, i = 0; p && i < 12; p = p.parentElement, i++) {
    if (p.querySelectorAll(sel).length >= _MIN_CUSTOM_OPTIONS) return p;
  }
  return null;
}

// Index of the one member whose class differs from all the others; -1 when
// they are all alike (nothing chosen); null when the state cannot be read
// (two or more differ), which drops the group.
function _customSelectedIndex(els) {
  const cls = els.map(_classOf);
  const freq = {};
  cls.forEach(c => freq[c] = (freq[c] || 0) + 1);
  const distinct = Object.keys(freq);
  if (distinct.length === 1) return -1;
  if (distinct.length !== 2) return null;
  const minority = distinct.filter(c => freq[c] === 1);
  if (minority.length !== 1) return null;
  return cls.indexOf(minority[0]);
}

function _customMembers(anchor, attr, consumed) {
  return Array.from(anchor.querySelectorAll('[' + CSS.escape(attr) + ']'))
    .filter(e => _visible(e) && !(consumed && consumed.has(e)));
}

// Recomputed from the live DOM so it also answers AFTER a click, when all
// the verify step has is the element itself. null means "not one of these
// widgets, or unreadable" -- never a guess.
function _customChecked(el) {
  const attr = _customOptionAttr(el);
  if (!attr) return null;
  const anchor = _customGroupAnchor(el, attr);
  if (!anchor) return null;
  const members = _customMembers(anchor, attr, null);
  if (members.length < _MIN_CUSTOM_OPTIONS) return null;
  const sel = _customSelectedIndex(members);
  if (sel === null) return null;
  return sel >= 0 && members[sel] === el;
}

// Option labels from the table header, by ordinary HTML table semantics:
// the column index of an option's cell, looked up in the header row. This
// is what gives "Strongly disagree".."Strongly agree" to options whose own
// markup carries no text at all.
function _matrixOptionLabels(els) {
  const tr = els[0].closest('tr');
  if (!tr) return null;
  const table = tr.closest('table');
  if (!table) return null;
  const head = table.querySelector('tr');
  if (!head || head === tr) return null;
  const heads = Array.from(head.children).map(c => c.textContent.trim());
  const cells = Array.from(tr.children);
  const out = els.map(e => {
    const cell = e.closest('td, th');
    const i = cell ? cells.indexOf(cell) : -1;
    return (i >= 0 && heads[i]) ? heads[i] : '';
  });
  return out.every(t => t) ? out : null;
}

// The statement a matrix row is asking about: its first cell, as long as
// that cell is not itself one of the option cells.
function _matrixRowLabel(els) {
  const tr = els[0].closest('tr');
  if (!tr || !tr.children.length) return null;
  const first = tr.children[0];
  if (els.some(e => first.contains(e))) return null;
  const t = first.textContent.trim();
  return (t && t.length < 300) ? t : null;
}

function _getCustomOptionGroups(consumed) {
  const byAttr = new Map();
  for (const el of document.querySelectorAll('*')) {
    const attr = _customOptionAttr(el);
    if (!attr || !_visible(el) || consumed.has(el)) continue;
    if (!byAttr.has(attr)) byAttr.set(attr, []);
    byAttr.get(attr).push(el);
  }
  const groups = [];
  let idx = 0;
  byAttr.forEach((els, attr) => {
    if (els.length < _MIN_CUSTOM_OPTIONS) return;
    const seen = new Set();
    els.forEach(el => {
      if (seen.has(el)) return;
      const anchor = _customGroupAnchor(el, attr);
      if (!anchor) return;
      const members = _customMembers(anchor, attr, consumed);
      if (members.length < _MIN_CUSTOM_OPTIONS) return;
      if (members.some(e => seen.has(e))) { members.forEach(e => seen.add(e)); return; }
      members.forEach(e => seen.add(e));
      // Structurally alike and actually clickable, or it is not an option set.
      const tag = members[0].tagName;
      if (!members.every(e => e.tagName === tag)) return;
      if (!members.every(e => getComputedStyle(e).cursor === 'pointer')) return;
      // Unreadable state: drop it. Filling a group whose current answer we
      // cannot see would overwrite that answer.
      const sel = _customSelectedIndex(members);
      if (sel === null) return;
      members.forEach(e => consumed.add(e));
      const id = 'co:' + (idx++);
      groups.push({
        id: id, kind: 'radio', els: members, custom: true,
        optionLabels: _matrixOptionLabels(members),
        text: _matrixRowLabel(members) || _groupLegend(anchor)
              || _precedingHeadingText(members[0]) || id,
      });
    });
  });
  return groups;
}
function _optionLabel(el) {
  if (el.tagName === 'INPUT') return _labelFor(el) || el.value;
  return _textOf(el) || el.getAttribute('data-value') || el.getAttribute('aria-label') || '';
}

// Returns [{ id, kind: 'radio'|'checkbox', els: [elements], text }, ...]
// covering both native <input> groups and ARIA role=radio/checkbox
// groups, with stable ids recomputable from the live DOM each call.
function _getChoiceGroups() {
  const groups = [];
  const consumed = new Set();

  Array.from(document.querySelectorAll('fieldset')).forEach((fs, idx) => {
    ['radio', 'checkbox'].forEach(type => {
      const els = Array.from(fs.querySelectorAll(`input[type=${type}]`))
        .filter(e => _visible(e) && !e.disabled);
      if (!els.length) return;
      els.forEach(e => consumed.add(e));
      groups.push({ id: `fs:${idx}:${type}`, kind: type, els,
        text: _groupLegend(fs) || `fs:${idx}:${type}` });
    });
  });

  const seenNameKeys = new Set();
  document.querySelectorAll('input[type=radio], input[type=checkbox]').forEach(el => {
    if (!_visible(el) || el.disabled || consumed.has(el)) return;
    const name = el.name || el.id;
    const key = el.type + ':' + name;
    if (!name || seenNameKeys.has(key)) return;
    seenNameKeys.add(key);
    const els = el.name
      ? Array.from(document.querySelectorAll(
          `input[type="${el.type}"][name="${CSS.escape(el.name)}"]`)).filter(e => _visible(e) && !e.disabled)
      : [el];
    groups.push({ id: `name:${key}`, kind: el.type, els,
      text: _groupLegend(el.closest('fieldset')) || name });
  });

  Array.from(document.querySelectorAll('[role=radiogroup]')).forEach((rg, idx) => {
    const els = Array.from(rg.querySelectorAll('[role=radio]')).filter(_visible);
    if (!els.length) return;
    els.forEach(e => consumed.add(e));
    groups.push({ id: `rg:${idx}`, kind: 'radio', els, text: _groupLegend(rg) || `rg:${idx}` });
  });

  const radioParents = new Map();
  document.querySelectorAll('[role=radio]').forEach(e => {
    if (!_visible(e) || consumed.has(e)) return;
    const p = e.parentElement;
    if (!radioParents.has(p)) radioParents.set(p, []);
    radioParents.get(p).push(e);
  });
  Array.from(radioParents.entries()).forEach(([parent, els], idx) => {
    els.forEach(e => consumed.add(e));
    groups.push({ id: `rgp:${idx}`, kind: 'radio', els,
      text: _groupLegend(parent) || _precedingHeadingText(els[0]) || `rgp:${idx}` });
  });

  Array.from(document.querySelectorAll('[role=group]')).forEach((g, idx) => {
    const els = Array.from(g.querySelectorAll('[role=checkbox]')).filter(e => _visible(e) && !consumed.has(e));
    if (!els.length) return;
    els.forEach(e => consumed.add(e));
    groups.push({ id: `cg:${idx}`, kind: 'checkbox', els, text: _groupLegend(g) || `cg:${idx}` });
  });

  const cbParents = new Map();
  document.querySelectorAll('[role=checkbox]').forEach(e => {
    if (!_visible(e) || consumed.has(e)) return;
    const p = e.parentElement;
    if (!cbParents.has(p)) cbParents.set(p, []);
    cbParents.get(p).push(e);
  });
  Array.from(cbParents.entries()).forEach(([parent, els], idx) => {
    els.forEach(e => consumed.add(e));
    const text = _groupLegend(parent) || _precedingHeadingText(els[0]) || `cgp:${idx}`;
    groups.push({ id: els.length > 1 ? `cgp:${idx}` : `cb:${idx}`, kind: 'checkbox', els, text });
  });

  // Last pass: custom-widget radio groups built from <button>/role=button
  // siblings (likert-btn, option-pill, choice-chip, ...). These are picked
  // up only if nothing above already consumed them, and extraction treats
  // them exactly like a radio group thanks to _isChecked's class-marker
  // branch.
  _getButtonGroups(consumed).forEach(g => groups.push(g));
  // Very last: option sets with no form semantics at all, identified by a
  // data-* attribute and read by odd-one-out. Last so that anything with a
  // real input, an ARIA role or a worded class marker is claimed first.
  _getCustomOptionGroups(consumed).forEach(g => groups.push(g));
  return groups;
}

function _getSwitches() {
  return Array.from(document.querySelectorAll('[role=switch]')).filter(_visible).map((el, idx) => ({
    id: `sw:${idx}`, el,
    text: _labelFor(el) || _groupLegend(el.closest('[role=group]')) || _precedingHeadingText(el) || `sw:${idx}`,
  }));
}

function _getListboxes() {
  return Array.from(document.querySelectorAll('[role=listbox]')).map((lb, idx) => {
    const opts = Array.from(lb.querySelectorAll('[role=option]')).filter(_visible);
    return { id: `lb:${idx}`, els: opts,
      text: _groupLegend(lb) || _precedingHeadingText(lb) || `lb:${idx}` };
  }).filter(g => g.els.length);
}

// A custom-widget radio group: sibling <button>/<div role=button> elements
// with the same base class, where none is a native input/role=radio (those
// are already covered above) and at least one carries a "selected" marker
// (an extra class suffix like `selected|active|checked|is-selected|on`, or
// `aria-pressed=true`, `aria-checked=true`, or `data-selected=true`).
// Catches survey tools that render Likert scales as <button class="likert-btn">
// / <button class="likert-btn selected">, which native + ARIA extraction misses.
// Each group is a parent whose children match this shape; the LABEL of the
// question is the preceding heading text (same mechanism as role=radio groups).
function _getButtonGroups(consumed) {
  const groups = [];
  const _SELECT_MARKER_RE = /\b(selected|active|checked|is-selected|is-active|on|chosen|current)\b/i;
  const _looksSelected = el => {
    if (el.getAttribute('aria-pressed') === 'true') return true;
    if (el.getAttribute('aria-checked') === 'true') return true;
    if (el.getAttribute('aria-selected') === 'true') return true;
    if (el.getAttribute('data-selected') === 'true') return true;
    if (el.getAttribute('data-checked') === 'true') return true;
    return _SELECT_MARKER_RE.test(el.className || '');
  };
  // Base class token = the first non-selected class token the element carries
  // (so "likert-btn selected" and "likert-btn" share base "likert-btn"). An
  // element with only a selected-marker class is base "" -- not a group key.
  const _baseKey = el => {
    const cls = (el.className || '').split(/\s+/).filter(Boolean);
    const base = cls.filter(c => !_SELECT_MARKER_RE.test(c));
    return base.join(' ');
  };

  const parents = new Map();  // parentNode -> Map<baseKey, [elements]>
  const sel = 'button, [role="button"], [tabindex]:not(input):not(textarea):not(select):not(a)';
  document.querySelectorAll(sel).forEach(el => {
    if (!_visible(el) || el.disabled || consumed.has(el)) return;
    if (el.tagName === 'INPUT') return;
    const txt = (el.textContent || '').trim();
    if (!txt || txt.length > 40) return;
    const base = _baseKey(el);
    if (!base) return;
    const p = el.parentElement;
    if (!p) return;
    if (!parents.has(p)) parents.set(p, new Map());
    const byKey = parents.get(p);
    if (!byKey.has(base)) byKey.set(base, []);
    byKey.get(base).push(el);
  });

  let idx = 0;
  parents.forEach((byKey, parent) => {
    byKey.forEach((els, key) => {
      if (els.length < 2) return;
      const uniq = new Set(els.map(e => (e.textContent || '').trim()));
      if (uniq.size < 2) return;
      // Need at least one visually "selected" sibling OR a role=button/tabindex
      // signature, otherwise we might capture a toolbar of actions.
      const hasSelected = els.some(_looksSelected);
      const allButtonish = els.every(e => e.tagName === 'BUTTON'
        || e.getAttribute('role') === 'button' || e.hasAttribute('tabindex'));
      if (!hasSelected && !allButtonish) return;
      els.forEach(e => consumed.add(e));
      groups.push({
        id: `bg:${idx++}`, kind: 'radio', els,
        text: _groupLegend(parent.closest('fieldset'))
          || _precedingHeadingText(parent)
          || _labelFor(parent)
          || `bg:${idx}`,
      });
    });
  });
  return groups;
}
"""

_EXTRACT_JS = _SHARED_JS + r"""
(function() {
  const fields = [];

  _getChoiceGroups().forEach(g => {
    const options = g.optionLabels || g.els.map(_optionLabel);
    const checked = g.els.map((e, i) => _isChecked(e) ? options[i] : null)
                         .filter(o => o !== null);
    fields.push({
      id: g.id, type: g.kind, text: g.text, options,
      current: g.kind === 'radio' ? (checked[0] || null) : checked,
      answered: checked.length > 0,
    });
  });

  _getSwitches().forEach(s => {
    const checked = s.el.getAttribute('aria-checked') === 'true';
    fields.push({
      id: s.id, type: 'checkbox', text: s.text, options: ['true', 'false'],
      current: checked ? ['true'] : [], answered: true,
    });
  });

  _getListboxes().forEach(lb => {
    const options = lb.els.map(_optionLabel);
    const selected = lb.els.filter(e => e.getAttribute('aria-selected') === 'true').map(_optionLabel);
    fields.push({
      id: lb.id, type: 'select', text: lb.text, options, multiple: false,
      current: selected[0] || null, answered: selected.length > 0,
    });
  });

  document.querySelectorAll('select').forEach(el => {
    if (!_visible(el) || el.disabled) return;
    const id = el.id || el.name;
    if (!id) return;
    const options = Array.from(el.querySelectorAll('option'))
      .map(o => o.textContent.trim()).filter(t => t);
    const selected = Array.from(el.selectedOptions).map(o => o.textContent.trim());
    const hasValue = Array.from(el.selectedOptions).some(o => o.value !== '');
    fields.push({
      id, type: 'select', text: _labelFor(el) || id, options,
      multiple: !!el.multiple,
      current: el.multiple ? selected : (selected[0] || null),
      answered: el.multiple ? selected.length > 0 : hasValue,
    });
  });

  document.querySelectorAll('textarea').forEach(el => {
    if (!_visible(el) || el.disabled) return;
    const id = el.id || el.name;
    if (!id) return;
    fields.push({
      id, type: 'textarea', text: _labelFor(el) || id,
      current: el.value, answered: el.value.trim() !== '',
    });
  });

  document.querySelectorAll('input[type=range]').forEach(el => {
    if (!_visible(el) || el.disabled) return;
    const id = el.id || el.name;
    if (!id) return;
    const initial = el.getAttribute('value');
    fields.push({
      id, type: 'range', text: _labelFor(el) || id,
      min: el.min, max: el.max, step: el.step,
      current: el.value,
      answered: initial !== null && el.value !== initial,
    });
  });

  document.querySelectorAll('input[type=date]').forEach(el => {
    if (!_visible(el) || el.disabled) return;
    const id = el.id || el.name;
    if (!id) return;
    fields.push({
      id, type: 'date', text: _labelFor(el) || id, format: 'YYYY-MM-DD',
      current: el.value, answered: el.value.trim() !== '',
    });
  });

  document.querySelectorAll(
    'input[type=text], input[type=email], input[type=number], input[type=tel], input:not([type])'
  ).forEach(el => {
    if (!_visible(el) || el.disabled) return;
    const id = el.id || el.name;
    if (!id) return;
    fields.push({
      id, type: 'text', text: _labelFor(el) || id,
      current: el.value, answered: el.value.trim() !== '',
    });
  });

  return JSON.stringify(fields);
})()
"""


def extract_fields() -> list:
    return cdp_eval(_EXTRACT_JS)


def page_has_credential_input() -> bool:
    """True if the page contains a password field -- a hard stop for
    batch-filling, regardless of how many other fields are present."""
    return cdp_eval(
        "JSON.stringify(!!document.querySelector('input[type=password]'))")


def _is_question_like(f: dict) -> bool:
    """A real question, as opposed to a consent/terms checkbox: a choice
    group offering a genuine choice, a free-text answer box, or a
    multi-option dropdown."""
    if f["type"] in ("textarea", "range", "date"):
        return True
    if f["type"] in ("radio", "checkbox", "select"):
        return len(f.get("options", [])) >= 2
    # A one-line text box counts only when it is phrased as a question
    # ("In what year were you born?"), so pages made only of short-answer
    # questions are filled -- while bare identity fields like "Prolific ID"
    # or "Email" (no question mark) still never trigger a batch-fill.
    if f["type"] == "text":
        return (f.get("text") or "").strip().endswith("?")
    return False


def batch_fill_verdict(fields: list) -> tuple:
    """Returns (should_fill, reason). Centralises the trigger rule so the
    agent loop and the standalone CLI can never disagree about it."""
    if page_has_credential_input():
        return False, "page contains a password field -- never batch-filled"
    if len(fields) < MIN_FIELDS_TO_TRIGGER:
        return False, (f"only {len(fields)} field(s) found "
                       f"(min {MIN_FIELDS_TO_TRIGGER}) -- ids: {[f['id'] for f in fields]}")
    question_like = [f for f in fields if _is_question_like(f)]
    if len(question_like) < MIN_QUESTION_LIKE_FIELDS:
        return False, (f"{len(fields)} field(s) but only {len(question_like)} look like real "
                       f"questions (min {MIN_QUESTION_LIKE_FIELDS}) -- likely a consent/"
                       f"acceptance box rather than a form")
    return True, f"{len(fields)} fields, {len(question_like)} question-like"


# -- decision (stateless side-call, NOT part of Fara's CUA conversation) --
#
# Fields are decided in small chunks rather than one giant call. Measured
# empirically: asking for 15 fields (incl. several "write 2-3 sentences"
# textareas) in one call regularly degenerated -- the model either fell
# back to its trained <tool_call> wrapper format instead of plain JSON, or
# entered a token-for-token repetition loop echoing the same answer block
# until max_tokens cut it off mid-object, and a couple of runs silently
# replaced an elaborated textarea answer with a lazy "..." placeholder.
# Keeping each call's expected output short (a handful of fields) keeps
# generation length far from whatever threshold triggers that behavior --
# this is the main reliability lever, more effective than sampler tuning
# alone (repeat_penalty is still set as a cheap extra guard).
CHUNK_SIZE = 6
DEFAULT_TEMPERATURE = 0.1
TRUNCATION_RATIO = 0.95  # completion_tokens this close to max_tokens => suspect cutoff


# -- rating scales -----------------------------------------------------------
#
# Likert-style questions often show only numbers, with the meaning given by
# two end labels ("Not higher" ... "Much higher"). Handed to the model as
# bare options "1".."6" plus a separate scale_anchors list, the 9B picked the
# wrong end on 9/9 such questions (e.g. answered 1 = "Not higher" to "Is
# Everest higher than Kilimanjaro?"). Writing the meaning INTO the options
# ("6 (Much higher)") puts it where the model reads the choice.

def scale_view_for_llm(f: dict) -> dict:
    anchors = f.get("scale_anchors")
    opts = f.get("options") or []
    if not anchors or len(anchors) < 2 or len(opts) < 3 or not all(re.fullmatch(r"-?\d+", o) for o in opts):
        return f
    lo, hi = anchors[0], anchors[-1]
    new_opts = list(opts)
    new_opts[0] = f"{opts[0]} ({lo})"
    new_opts[-1] = f"{opts[-1]} ({hi})"
    g = {k: v for k, v in f.items() if k != "scale_anchors"}
    g["options"] = new_opts
    g["text"] = f"{f.get('text', '')} [rating scale: {opts[0]} = {lo}, {opts[-1]} = {hi}]"
    g["_orig_options"] = opts
    return g


def scale_answer_back(f: dict, answer):
    """Map "6 (Much higher)" back to the real option "6"."""
    orig = f.get("_orig_options")
    if not orig or not isinstance(answer, str):
        return answer
    m = re.match(r"\s*(-?\d+)", answer)
    return m.group(1) if m and m.group(1) in orig else answer


def _chunk(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def ask_llm_for_answers(fields: list, persona: str = "",
                         temperature: float = DEFAULT_TEMPERATURE,
                         chunk_size: int = CHUNK_SIZE) -> tuple:
    """Returns (answers, failed_ids). A chunk that degenerates even on
    retry is skipped rather than aborting the whole page: observed in a
    real run, one bad chunk raised out of here and cost the entire
    batch-fill (all other fields fell back to per-field vision clicking
    for no reason). Partial progress plus a named list of what to leave
    to the vision agent is strictly better."""
    answers, failed = {}, []
    for chunk in _chunk(fields, chunk_size):
        try:
            got = _ask_llm_for_chunk(chunk, persona, temperature)
            answers.update(got)
            # A valid reply that simply omits some questions (seen with the
            # 9B on open-ended Italian text, ~2 in 3 samples): ask again for
            # just the missing ones, deterministically, before giving up.
            missing = [f for f in chunk if f["id"] not in got]
            if missing:
                try:
                    answers.update(_ask_llm_for_chunk(missing, persona, 0.0))
                except (RuntimeError, ValueError):
                    pass
            continue
        except (RuntimeError, ValueError):
            pass
        try:
            # One retry at temperature 0 (maximally deterministic) --
            # covers a one-off degenerate sampling draw.
            answers.update(_ask_llm_for_chunk(chunk, persona, 0.0))
        except (RuntimeError, ValueError):
            failed.extend(f["id"] for f in chunk)
    return answers, failed


def _ask_llm_for_chunk(fields: list, persona: str, temperature: float) -> dict:
    persona_line = (
        f"Persona/context for your answers: {persona}\n\n" if persona else ""
    )
    prompt = (
        "You are filling out a web form with synthetic test data (authorized "
        "QA testing of the site owner's own form -- not a real person's "
        "information). Below is a JSON list of the questions currently "
        "visible on the page.\n\n"
        f"{persona_line}"
        "ATTENTION CHECKS -- HARDEST RULE, APPLY FIRST. For EVERY question, scan "
        "its FULL `text` field AND every option label for an embedded instruction. "
        "Patterns to catch: 'to show you are paying attention select X', "
        "'this is an attention check, choose Y', 'please select [specific option]', "
        "'choose the [third/second/last] option', 'select strongly agree/disagree "
        "for this one', 'what colour is the sky', 'if you are reading this select Z'. "
        "If a question contains such an instruction, your answer MUST obey it EXACTLY, "
        "overriding persona, consistency, variance, and what feels natural. One failed "
        "attention check invalidates the whole submission and loses the reward. Treat "
        "any question whose text includes 'attention', 'paying attention', 'to verify', "
        "'to confirm you are reading', or names a specific option as directive. "
        "Only once no attention-check instruction is present, fall back to the normal "
        "answer rules below.\n\n"
        "Answer ALL of them with plausible, internally consistent values. "
        "Quality rules: read each question fully. "
        "Stay consistent with the persona and with any answers "
        "already given earlier in this survey (listed below if any). Answer "
        "open-ended questions specifically about what is asked, in the "
        "language the question is written in -- no generic filler. Do not "
        "pick the same scale position for every question unless the persona "
        "genuinely holds that view throughout. "
        "Reply with ONLY a single JSON object mapping each question id to "
        "your chosen answer, following these rules per type:\n"
        "- radio / select (multiple=false): the EXACT option text given.\n"
        "- checkbox / select (multiple=true): a JSON array of exact option "
        "texts (empty array if none apply).\n"
        "- text / textarea: a short plausible answer; for questions asking "
        "to 'elaborate' or 'explain', write 2-3 full sentences of real "
        "content -- never a placeholder like '...' or 'N/A'.\n"
        "- range: a single number (as a string) between min and max.\n"
        "- date: a plausible date as a string in YYYY-MM-DD format.\n"
        "- If a field has scale_anchors, its options are positions on a "
        "scale running from the first anchor (lowest option) to the last "
        "anchor (highest option).\n"
        "- Personal facts the persona/context does not state (age, birth "
        "date, education, income, etc.): invent a plausible value that is "
        "consistent with everything the persona DOES state (e.g. an age "
        "matching their job and life stage). Never contradict the persona.\n"
        "Output the object once, then stop -- no repetition, no markdown "
        "fences, no extra commentary after it.\n\n"
        f"Questions:\n{json.dumps(fields, indent=2)}"
    )
    payload = {
        "model": "x",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "repeat_penalty": 1.15,
        # Measured: the model writes the correct JSON answer immediately and
        # then, out of its computer-use training, keeps emitting
        # "<tool_call>" until max_tokens. Stop right there.
        "stop": ["<tool_call>", "</tool_call>"],
        "max_tokens": 250 + 150 * len(fields),
        # This is a plain content-decision call, not a visual-grounding
        # click -- unlike Fara's CUA steps (where the <think> phase may
        # matter for coordinate accuracy and must stay on), skipping it
        # here measured ~2.4x faster with no quality or reliability loss
        # across repeated trials (still real 2-3 sentence answers, no
        # placeholders, no malformed JSON).
        "chat_template_kwargs": {"enable_thinking": False},
    }
    result = cancel.post_chat(LLAMA_URL, payload, timeout=120)
    content = result["choices"][0]["message"]["content"]
    usage = result.get("usage", {})
    completion = usage.get("completion_tokens", 0)
    requested = payload["max_tokens"]
    # Hitting max_tokens only means failure if no complete answer came out
    # before the runaway: a valid JSON object covering every requested id
    # is accepted even when trailing junk was cut off (previously such
    # correct answers were discarded as "degenerate").
    try:
        obj = _extract_json_object(content)
    except ValueError:
        obj = None
    ids = {f["id"] for f in fields}
    if obj is not None and ids <= set(obj):
        return obj
    if completion >= requested * TRUNCATION_RATIO:
        raise RuntimeError(
            f"model output likely truncated/degenerate ({completion}/{requested} "
            f"completion tokens) for field chunk {[f['id'] for f in fields]!r}"
        )
    if obj is None:
        raise ValueError(f"no JSON object in model reply: {content[:200]!r}")
    return obj


def _extract_json_object(content: str) -> dict:
    """Finds the model's answer object in a raw completion.

    The model's reasoning (a <think> block, not always cleanly closed) can
    itself contain brace-like fragments -- e.g. a sketched example before
    the real answer -- so naive first-'{'-to-last-'}' slicing can capture
    a stray early fragment and leave real content dangling after it
    (json.loads then raises "Extra data"). The answer is reliably the
    LAST well-formed JSON object the model emitted, so scan candidate
    start positions from the end of the string backward and decode from
    there instead.
    """
    stripped = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    decoder = json.JSONDecoder()
    brace_idxs = [i for i, c in enumerate(stripped) if c == "{"]
    for idx in reversed(brace_idxs):
        try:
            obj, _ = decoder.raw_decode(stripped, idx)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ValueError(f"no JSON object in model reply: {content!r}")


# -- apply ------------------------------------------------------------------
#
# Two phases, in one script so both run in the same synchronous tick before
# the browser repaints: CHOICE elements (radio/checkbox/select) first, then
# FREEFORM elements (text/textarea/range/date) second. Some forms reveal or
# repopulate other fields from a radio/checkbox/select's onchange handler
# (an "Other: please specify" box, a cascading dropdown, a hidden section).
# Running choices first means any such side effect has already happened by
# the time freeform fields are located and written.

_APPLY_JS_TEMPLATE = _SHARED_JS + r"""
(function() {
  const answers = %(answers_json)s;
  const nativeInputSetter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value').set;
  const nativeTextareaSetter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype, 'value').set;
  const applied = {};
  const errors = {};

  function setNativeValue(el, value) {
    const setter = el.tagName === 'TEXTAREA' ? nativeTextareaSetter : nativeInputSetter;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  const freeform = [];
  const choiceGroups = new Map(_getChoiceGroups().map(g => [g.id, g]));
  const switches = new Map(_getSwitches().map(s => [s.id, s]));
  const listboxes = new Map(_getListboxes().map(lb => [lb.id, lb]));

  for (const [id, answer] of Object.entries(answers)) {
    try {
      if (choiceGroups.has(id)) {
        const g = choiceGroups.get(id);
        if (g.kind === 'radio') {
          let hit = false;
          g.els.forEach(el => {
            if (_optionLabel(el) === answer) { el.click(); hit = true; }
          });
          applied[id] = hit;
        } else {
          const wanted = Array.isArray(answer) ? answer : [answer];
          let hitCount = 0;
          g.els.forEach(el => {
            const should = wanted.includes(_optionLabel(el));
            if (should !== _isChecked(el)) { el.click(); }
            if (should) hitCount++;
          });
          applied[id] = hitCount > 0 || wanted.length === 0;
        }
        continue;
      }
      if (switches.has(id)) {
        const s = switches.get(id);
        const wanted = Array.isArray(answer) ? answer.includes('true') : !!answer;
        if (wanted !== _isChecked(s.el)) { s.el.click(); }
        applied[id] = true;
        continue;
      }
      if (listboxes.has(id)) {
        const lb = listboxes.get(id);
        const opt = lb.els.find(o => _optionLabel(o) === answer);
        if (!opt) { errors[id] = `option not found: ${answer}`; continue; }
        opt.click();
        applied[id] = true;
        continue;
      }
      const el = document.getElementById(id) || document.getElementsByName(id)[0];
      if (!el) { errors[id] = 'element not found'; continue; }
      if (el.tagName === 'SELECT') {
        if (el.multiple) {
          const wanted = Array.isArray(answer) ? answer : [answer];
          let hitCount = 0;
          Array.from(el.options).forEach(o => {
            const text = o.textContent.trim();
            o.selected = wanted.includes(text) || wanted.includes(o.value);
            if (o.selected) hitCount++;
          });
          el.dispatchEvent(new Event('change', { bubbles: true }));
          applied[id] = hitCount > 0 || wanted.length === 0;
        } else {
          const opt = Array.from(el.options).find(o => o.textContent.trim() === answer);
          if (!opt) { errors[id] = `option not found: ${answer}`; continue; }
          el.value = opt.value;
          el.dispatchEvent(new Event('change', { bubbles: true }));
          applied[id] = true;
        }
        continue;
      }
      // text/textarea/range/date: deferred to the freeform pass below, so
      // any element a choice above just revealed already exists by then.
      freeform.push([id, answer]);
    } catch (e) {
      errors[id] = String(e);
    }
  }

  for (const [id, answer] of freeform) {
    try {
      const el = document.getElementById(id) || document.getElementsByName(id)[0];
      if (!el) { errors[id] = 'element not found'; continue; }
      if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
        setNativeValue(el, String(answer));
        applied[id] = true;
      } else {
        errors[id] = `unsupported element tag: ${el.tagName}`;
      }
    } catch (e) {
      errors[id] = String(e);
    }
  }

  return JSON.stringify({ applied, errors });
})()
"""


def apply_answers(answers: dict) -> dict:
    js = _APPLY_JS_TEMPLATE % {"answers_json": json.dumps(answers)}
    return cdp_eval(js)


# -- verify (DOM read-back, not a screenshot) -------------------------------

_VERIFY_JS_TEMPLATE = _SHARED_JS + r"""
(function() {
  const ids = %(ids_json)s;
  const state = {};
  const choiceGroups = new Map(_getChoiceGroups().map(g => [g.id, g]));
  const switches = new Map(_getSwitches().map(s => [s.id, s]));
  const listboxes = new Map(_getListboxes().map(lb => [lb.id, lb]));

  for (const id of ids) {
    if (choiceGroups.has(id)) {
      const g = choiceGroups.get(id);
      const checked = g.els.filter(_isChecked).map(_optionLabel);
      state[id] = g.kind === 'radio' ? (checked[0] || null) : checked;
      continue;
    }
    if (switches.has(id)) {
      state[id] = _isChecked(switches.get(id).el) ? ['true'] : [];
      continue;
    }
    if (listboxes.has(id)) {
      const lb = listboxes.get(id);
      const sel = lb.els.filter(e => e.getAttribute('aria-selected') === 'true').map(_optionLabel);
      state[id] = sel[0] || null;
      continue;
    }
    const el = document.getElementById(id) || document.getElementsByName(id)[0];
    if (!el) { state[id] = null; continue; }
    if (el.tagName === 'SELECT') {
      if (el.multiple) {
        state[id] = Array.from(el.selectedOptions).map(o => o.textContent.trim());
      } else {
        state[id] = el.options[el.selectedIndex] ? el.options[el.selectedIndex].textContent.trim() : null;
      }
    } else {
      state[id] = el.value;
    }
  }
  return JSON.stringify(state);
})()
"""


def verify_fields(ids: list) -> dict:
    js = _VERIFY_JS_TEMPLATE % {"ids_json": json.dumps(ids)}
    return cdp_eval(js)


# -- orchestration ----------------------------------------------------------

def _is_multi_valued(f: dict) -> bool:
    return f["type"] == "checkbox" or (f["type"] == "select" and bool(f.get("multiple")))


def _compute_mismatches(fields: list, answers: dict, verified: dict) -> list:
    mismatches = []
    for f in fields:
        fid = f["id"]
        if fid not in answers:
            continue
        intended = answers.get(fid)
        actual = verified.get(fid)
        if _is_multi_valued(f):
            intended_set = set(intended) if isinstance(intended, list) else set()
            actual_set = set(actual) if isinstance(actual, list) else set()
            if intended_set != actual_set:
                mismatches.append({"id": fid, "intended": intended, "actual": actual})
        else:
            if intended is not None and str(intended) != str(actual):
                mismatches.append({"id": fid, "intended": intended, "actual": actual})
    return mismatches


def run_batch_fill(persona: str = "", max_apply_attempts: int = 2) -> dict:
    """Runs the full extract -> decide -> apply -> verify cycle once.

    After the first apply+verify pass, any mismatched fields (e.g. a
    cascading dropdown whose options got reset by another field's change
    handler) are retried up to `max_apply_attempts` total tries before
    being reported as a real failure -- most such mismatches are
    transient DOM side effects that settle once re-applied.

    Returns a summary dict always, even when skipped or partially failed,
    so the caller (the main CUA loop) can turn it into a short text
    observation for Fara's next step instead of a screenshot.
    """
    fields = extract_fields()
    ok, reason = batch_fill_verdict(fields)
    if not ok:
        return {"skipped": True, "reason": reason, "fields": fields}

    # Extraction reports each field's CURRENT value, so a field already
    # answered in an earlier pass (e.g. a conditional section revealed
    # after that pass, re-triggering batch-fill with a new field-id set)
    # is never re-asked, and never gets its existing valid answer
    # overwritten with a different synthetic one.
    unanswered = [f for f in fields if not f.get("answered")]
    preserved = {f["id"]: f["current"] for f in fields if f.get("answered")}

    if unanswered:
        new_answers, undecided = ask_llm_for_answers(unanswered, persona=persona)
    else:
        new_answers, undecided = {}, []
    answers = {**preserved, **new_answers}

    apply_errors = {}
    verify_result = dict(preserved)
    mismatches = []
    attempts = 0

    pending = dict(new_answers)
    while pending and attempts < max_apply_attempts:
        attempts += 1
        apply_result = apply_answers(pending)
        apply_errors.update(apply_result.get("errors", {}))
        verify_result.update(verify_fields(list(pending.keys())))
        mismatches = _compute_mismatches(fields, new_answers, verify_result)
        retry_ids = {m["id"] for m in mismatches} - set(apply_errors.keys())
        pending = {k: v for k, v in new_answers.items() if k in retry_ids}

    return {
        "skipped": False,
        "field_count": len(fields),
        "already_answered_count": len(preserved),
        "newly_answered_count": len(new_answers),
        "undecided": undecided,
        "answers": answers,
        "apply_errors": apply_errors,
        "mismatches": mismatches,
        "verified_state": verify_result,
        "apply_attempts": attempts,
    }


# -- DOM-first advance (Next/Submit) ----------------------------------------
#
# Measured on the complex fixture: after batch-fill the vision loop spent
# ~39s of a 60s task doing 4-5 screenshot->model->action round-trips whose
# only purpose was to scroll down and locate the "Next" button -- a
# question the DOM answers instantly and exactly. Clicking it directly
# (element.click() works regardless of scroll position) collapses those
# steps into one ~0.3s call.
#
# Scope is deliberately narrow for safety: this only runs right after a
# clean batch-fill on the same page, and only matches FORWARD-navigation
# labels. Anything destructive or backward (delete/cancel/back/logout) is
# explicitly excluded, and an ambiguous page (no match, or several equally
# plausible matches) returns nothing so the vision agent handles it.

_ADVANCE_LABELS = [
    "next", "continue", "submit", "proceed", "forward", "start", "begin",
    "avanti", "continua", "invia", "procedi", "prosegui", "conferma", "inizia",
    "next page", "next question", "save and continue", "salva e continua",
]
_BLOCKED_LABELS = [
    "back", "previous", "prev", "cancel", "delete", "remove", "clear", "reset",
    "logout", "log out", "sign out", "exit", "quit", "close", "decline",
    "indietro", "precedente", "annulla", "elimina", "cancella", "esci",
    "rifiuta", "chiudi", "disconnetti",
]

_FIND_ADVANCE_JS_TEMPLATE = _SHARED_JS + r"""
(function() {
  const allow = %(allow_json)s;
  const block = %(block_json)s;

  function labelOf(el) {
    const t = (el.value && el.tagName === 'INPUT' ? el.value : el.textContent) || '';
    const aria = el.getAttribute('aria-label') || '';
    return (t.trim() || aria.trim()).replace(/\s+/g, ' ').toLowerCase();
  }

  const candidates = Array.from(document.querySelectorAll(
    'button, input[type=submit], input[type=button], [role=button], a.btn, a.button'
  )).filter(el => {
    if (!_visible(el) || el.disabled) return false;
    if (el.getAttribute('aria-disabled') === 'true') return false;
    const label = labelOf(el);
    if (!label || label.length > 40) return false;
    if (block.some(b => label.includes(b))) return false;
    return allow.some(a => label === a || label.startsWith(a + ' ') || label === a + '!');
  });

  if (!candidates.length) return JSON.stringify({ found: false, reason: 'no forward-navigation button matched' });

  // The advance control is conventionally the last one in document order
  // (bottom of the form); a submit input also outranks a generic button.
  candidates.sort((a, b) => {
    const aSubmit = (a.tagName === 'INPUT' && a.type === 'submit') || a.type === 'submit';
    const bSubmit = (b.tagName === 'INPUT' && b.type === 'submit') || b.type === 'submit';
    if (aSubmit !== bSubmit) return aSubmit ? 1 : -1;
    return a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
  });

  const chosen = candidates[candidates.length - 1];
  const r = chosen.getBoundingClientRect();
  return JSON.stringify({
    found: true,
    label: labelOf(chosen),
    tag: chosen.tagName,
    candidate_count: candidates.length,
    all_labels: candidates.map(labelOf),
    rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
  });
})()
"""

_CLICK_ADVANCE_JS_TEMPLATE = _SHARED_JS + r"""
(function() {
  const allow = %(allow_json)s;
  const block = %(block_json)s;
  function labelOf(el) {
    const t = (el.value && el.tagName === 'INPUT' ? el.value : el.textContent) || '';
    const aria = el.getAttribute('aria-label') || '';
    return (t.trim() || aria.trim()).replace(/\s+/g, ' ').toLowerCase();
  }
  const candidates = Array.from(document.querySelectorAll(
    'button, input[type=submit], input[type=button], [role=button], a.btn, a.button'
  )).filter(el => {
    if (!_visible(el) || el.disabled) return false;
    if (el.getAttribute('aria-disabled') === 'true') return false;
    const label = labelOf(el);
    if (!label || label.length > 40) return false;
    if (block.some(b => label.includes(b))) return false;
    return allow.some(a => label === a || label.startsWith(a + ' ') || label === a + '!');
  });
  if (!candidates.length) return JSON.stringify({ clicked: false });
  candidates.sort((a, b) => {
    const aSubmit = (a.tagName === 'INPUT' && a.type === 'submit') || a.type === 'submit';
    const bSubmit = (b.tagName === 'INPUT' && b.type === 'submit') || b.type === 'submit';
    if (aSubmit !== bSubmit) return aSubmit ? 1 : -1;
    return a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
  });
  const chosen = candidates[candidates.length - 1];
  chosen.click();
  return JSON.stringify({ clicked: true, label: labelOf(chosen) });
})()
"""

_FINGERPRINT_JS = _SHARED_JS + r"""
(function() {
  // Cheap page-identity signal used to confirm a click actually advanced
  // the page, rather than being silently swallowed (e.g. client-side
  // validation blocking submit).
  const ids = _getChoiceGroups().map(g => g.id).join(',');
  const text = (document.body ? document.body.innerText : '').slice(0, 3000);
  let h = 0;
  for (let i = 0; i < text.length; i++) { h = ((h << 5) - h + text.charCodeAt(i)) | 0; }
  return JSON.stringify({ url: location.href, field_ids: ids, text_hash: h,
                           text_len: text.length });
})()
"""


def _labels_js_args() -> dict:
    return {"allow_json": json.dumps(_ADVANCE_LABELS),
            "block_json": json.dumps(_BLOCKED_LABELS)}


def find_advance_button() -> dict:
    return cdp_eval(_FIND_ADVANCE_JS_TEMPLATE % _labels_js_args())


def page_fingerprint() -> dict:
    return cdp_eval(_FINGERPRINT_JS)


def click_advance_button(settle_seconds: float = 1.2) -> dict:
    """Finds and clicks the page's forward-navigation button via the DOM,
    then confirms the page actually changed. Returns a dict describing
    what happened; `advanced` False means the caller should fall back to
    the normal vision loop rather than assume progress."""
    import time as _time

    found = find_advance_button()
    if not found.get("found"):
        return {"attempted": False, "reason": found.get("reason", "no button found")}

    before = page_fingerprint()
    clicked = cdp_eval(_CLICK_ADVANCE_JS_TEMPLATE % _labels_js_args())
    if not clicked.get("clicked"):
        return {"attempted": False, "reason": "button vanished between find and click"}

    _time.sleep(settle_seconds)
    after = page_fingerprint()
    advanced = (before.get("url") != after.get("url")
                or before.get("field_ids") != after.get("field_ids")
                or before.get("text_hash") != after.get("text_hash"))
    return {
        "attempted": True, "advanced": advanced,
        "label": clicked.get("label"),
        "candidate_count": found.get("candidate_count"),
        "url_before": before.get("url"), "url_after": after.get("url"),
    }


def summarize_for_agent(result: dict) -> str:
    """Turns a run_batch_fill() result into a short text observation to feed
    back into Fara's normal CUA loop (replacing what would otherwise be a
    verification screenshot)."""
    if result.get("skipped"):
        return f"[batch-fill] skipped: {result['reason']}"
    n = result["field_count"]
    undecided = result.get("undecided", [])
    bad = len(result["mismatches"]) + len(result["apply_errors"]) + len(undecided)
    ok = n - bad
    already = result.get("already_answered_count", 0)
    msg = f"[batch-fill] filled {ok}/{n} fields successfully."
    if already:
        msg += f" ({already} were already answered and left untouched.)"
    if undecided:
        msg += f" Could not decide answers for {undecided} -- fill those yourself."
    if result.get("apply_errors_human"):
        msg += " " + " ".join(result["apply_errors_human"])
    elif result["apply_errors"]:
        msg += f" Errors: {result['apply_errors']}."
    if result["mismatches"]:
        msg += f" Mismatches: {result['mismatches']}."
    return msg


if __name__ == "__main__":
    import sys
    import time

    fixture = sys.argv[1] if len(sys.argv) > 1 else "test_form.html"
    subprocess.run(["docker", "exec", os.environ.get("FARA_CONTAINER", "fara-browser-pw"), "cdp.py", "navigate",
                     f"file:///data/{fixture}"], check=True)
    time.sleep(1)

    print("=== extracted fields ===")
    fields = extract_fields()
    print(json.dumps(fields, indent=2))

    t0 = time.time()
    result = run_batch_fill(persona="A 29 year old software engineer from Milan who values privacy.")
    elapsed = time.time() - t0
    print(f"\n=== batch-fill result ({elapsed:.1f}s) ===")
    print(json.dumps(result, indent=2))
    print("\n=== agent-facing summary ===")
    print(summarize_for_agent(result))
    sys.exit(1 if (result.get("mismatches") or result.get("apply_errors")) else 0)
