"""Unit tests for the context-trim + page-change-signal helpers.

Covers:
  * per-speed-mode budget lookup
  * thought-stripping is idempotent and byte-stable (so the llama-server
    KV-cache prefix match keeps working after one batch strip)
  * stripping only fires once per batch (len(uncompacted) > _MAX_THOUGHTS)
  * page-content-hash normalises whitespace
  * page-change-hint emits only for advancing actions, both directions

These are the two regressions this file prevents:
  (1) a change to _strip_old_thoughts that drops thoughts every step and
      invalidates the cache on every step — slow steps come back
  (2) page-change hints emitted for scroll/mouse_move, where no change
      is expected and the signal would be noise
"""

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "agent"))

# agent_core pulls fara_prompts/pw_bridge etc. Stub what we don't need; the
# helpers under test are pure functions over message lists and strings.
_stubs = {
    "playwright": types.ModuleType("playwright"),
    "playwright.sync_api": types.ModuleType("playwright.sync_api"),
    "pw_bridge": types.ModuleType("pw_bridge"),
    "pusher_listener": types.ModuleType("pusher_listener"),
    "cancel": types.ModuleType("cancel"),
    "study_memory": types.ModuleType("study_memory"),
    "study_cache": types.ModuleType("study_cache"),
}
_stubs["playwright.sync_api"].sync_playwright = lambda: None
# cancel.sleep is referenced at import indirectly through decorators
_stubs["cancel"].check = lambda: None
_stubs["cancel"].sleep = lambda s: None
class _Cancelled(Exception): pass
_stubs["cancel"].Cancelled = _Cancelled
_stubs["cancel"].reset = lambda: None
# call_fara dispatches through cancel.post_chat; tests monkeypatch it per-call.
_stubs["cancel"].post_chat = lambda *a, **k: {"choices": [{"message": {"content": ""}}]}
# pw_bridge needs an evaluate for the page-text helper; it won't be called
# from these tests (we feed hashes/strings directly).
def _no_bridge(*a, **k):
    raise RuntimeError("pw_bridge not available in unit tests")
_stubs["pw_bridge"].evaluate = _no_bridge
_stubs["pw_bridge"].goto = _no_bridge
_stubs["pw_bridge"].wait_for_content = _no_bridge
_stubs["pw_bridge"].run = _no_bridge
# study_memory + study_cache: used by run_agent only, not by the helpers.
_stubs["study_memory"].record = lambda *a, **k: None
_stubs["study_memory"].record_user_hint = lambda *a, **k: None
_stubs["study_cache"].entries = lambda: {}
_stubs["study_cache"].mark = lambda *a, **k: None

for name, mod in _stubs.items():
    sys.modules.setdefault(name, mod)

import agent_core

# For the mem:* tests we need the REAL study_memory (user_hints_for, _read,
# _fresh, _lock, enter_hint_count). agent_core already imported the stub,
# which was fine when tests called study_memory directly -- but the
# autochain:* tests go THROUGH agent_core._should_auto_chain_enter, so we
# must also re-bind agent_core.study_memory to the real module or those
# calls land on the stub (no enter_hint_count → AttributeError → False).
sys.modules.pop("study_memory", None)
import study_memory as _real_study_memory
agent_core.study_memory = _real_study_memory


_RESULTS: list[tuple[str, str, str]] = []


def _t(name: str):
    def _run(fn):
        try:
            fn()
            print(f"PASS  {name}")
            _RESULTS.append((name, "PASS", ""))
        except AssertionError as e:
            print(f"FAIL  {name}  ::  {e}")
            _RESULTS.append((name, "FAIL", str(e)))
        except Exception as e:
            print(f"ERR   {name}  ::  {type(e).__name__}: {e}")
            _RESULTS.append((name, "ERR", f"{type(e).__name__}: {e}"))
        return fn
    return _run


def _mk_turn(thoughts: str, tool_call: str = '{"action":"left_click","coordinate":[1,1]}') -> dict:
    return {"role": "assistant",
            "content": f"<think>\n{thoughts}\n</think>\n\n{thoughts}\n<tool_call>\n{tool_call}\n</tool_call>"}


@_t("budget:per_mode_lookup")
def _():
    agent_core._speed_mode = "normal"
    assert agent_core._current_budget() == agent_core._CONTEXT_BUDGETS["normal"]
    agent_core._speed_mode = "hurry"
    assert agent_core._current_budget() == agent_core._CONTEXT_BUDGETS["hurry"]
    agent_core._speed_mode = "super_fast"
    assert agent_core._current_budget() == agent_core._CONTEXT_BUDGETS["super_fast"]


@_t("budget:super_fast_strictly_smaller_than_normal")
def _():
    assert agent_core._CONTEXT_BUDGETS["super_fast"] < agent_core._CONTEXT_BUDGETS["normal"]
    assert agent_core._CONTEXT_BUDGETS["hurry"] < agent_core._CONTEXT_BUDGETS["normal"]
    assert agent_core._CONTEXT_BUDGETS["super_fast"] < agent_core._CONTEXT_BUDGETS["hurry"]


@_t("strip:no_op_in_normal_mode")
def _():
    agent_core._speed_mode = "normal"
    msgs = [{"role": "user", "content": "hi"}] + [_mk_turn(f"thought {i}") for i in range(20)]
    before = [m["content"] for m in msgs]
    agent_core._strip_old_thoughts(msgs)
    after = [m["content"] for m in msgs]
    assert before == after, "normal mode must not mutate anything"


@_t("strip:fires_in_super_fast_above_threshold")
def _():
    agent_core._speed_mode = "super_fast"
    # Pull the live thresholds from the module so the test tracks whatever
    # the speed-mode tiering currently is (D moved what used to be
    # super_fast into hurry, so the numbers do change).
    max_t = agent_core._MAX_THOUGHTS["super_fast"]
    trim  = agent_core._TRIM_TO_THOUGHTS["super_fast"]
    n_turns = max_t + 2  # above the trigger
    msgs = [{"role": "user", "content": "hi"}]
    for i in range(n_turns):
        msgs.append(_mk_turn(f"reasoning {i}"))
    agent_core._strip_old_thoughts(msgs)
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    stripped = [m for m in assistant_msgs if m["content"].startswith("<tool_call>")]
    kept     = [m for m in assistant_msgs if not m["content"].startswith("<tool_call>")]
    assert len(stripped) == n_turns - trim, \
        f"expected {n_turns - trim} stripped, got {len(stripped)}"
    assert len(kept) == trim, f"expected {trim} kept intact (trim_to), got {len(kept)}"


@_t("strip:idempotent_second_call_is_byte_stable")
def _():
    # This is the KV-cache contract: once compacted, further calls must
    # leave the strings byte-identical, or the prefix match breaks every
    # step and the whole speedup is lost.
    agent_core._speed_mode = "super_fast"
    msgs = [{"role": "user", "content": "hi"}]
    for i in range(8):
        msgs.append(_mk_turn(f"reasoning {i}"))
    agent_core._strip_old_thoughts(msgs)
    snapshot = [m["content"] for m in msgs]
    agent_core._strip_old_thoughts(msgs)
    agent_core._strip_old_thoughts(msgs)
    for i, (a, b) in enumerate(zip(snapshot, [m["content"] for m in msgs])):
        assert a == b, f"message {i} mutated on second strip (cache breaker)"


@_t("strip:does_nothing_when_below_threshold")
def _():
    agent_core._speed_mode = "super_fast"
    max_t = agent_core._MAX_THOUGHTS["super_fast"]
    n_turns = max(1, max_t - 1)  # one short of trigger
    msgs = [{"role": "user", "content": "hi"}] + [_mk_turn(f"r{i}") for i in range(n_turns)]
    before = [m["content"] for m in msgs]
    agent_core._strip_old_thoughts(msgs)
    after = [m["content"] for m in msgs]
    assert before == after, "under threshold must not strip"


@_t("strip:preserves_tool_call_payload")
def _():
    agent_core._speed_mode = "super_fast"
    payload = '{"action":"left_click","coordinate":[249.12,569.7]}'
    msgs = [{"role": "user", "content": "hi"}]
    msgs.extend(_mk_turn(f"reasoning {i}", payload) for i in range(8))
    agent_core._strip_old_thoughts(msgs)
    for m in msgs:
        if m["role"] != "assistant": continue
        if m["content"].startswith("<tool_call>"):
            assert payload in m["content"], "tool_call payload lost on strip"


@_t("sim:whitespace_normalised")
def _():
    assert agent_core._WS_RE.sub(" ", "a   b\n\n c").strip() == "a b c"
    assert agent_core._text_similarity("a b c", "a b c") == 1.0
    assert agent_core._text_similarity("a b c", "x y z") == 0.0


@_t("sim:jaccard_math_sanity")
def _():
    # 2 shared, 1 unique each side → 2/4 = 0.5
    s = agent_core._text_similarity("a b c", "a b d")
    assert abs(s - 2/4) < 1e-9, f"expected 0.5, got {s}"


@_t("hint:silent_for_scroll")
def _():
    out = agent_core._page_change_hint("aaa", "scroll", "bbb")
    assert out == "", f"scroll must emit no page-signal, got {out!r}"


@_t("hint:silent_for_mouse_move")
def _():
    out = agent_core._page_change_hint("aaa", "mouse_move", "bbb")
    assert out == ""


@_t("hint:silent_without_baseline")
def _():
    assert agent_core._page_change_hint("", "left_click", "bbb") == ""
    assert agent_core._page_change_hint("aaa", "left_click", "") == ""


@_t("hint:tier_identical_says_no_effect")
def _():
    out = agent_core._page_change_hint("one two three", "key", "one two three")
    assert "IDENTICAL" in out, out
    assert "no visible effect" in out


@_t("hint:tier_minor_update_warns_about_navigation")
def _():
    # Trial-12 unselected vs trial-12-with-choice-1-selected: ~99%+ identical
    # (one new label added to a 100-word page). Hint MUST warn the model not
    # to treat this as an advance — this is the exact bug the user flagged.
    base = " ".join(f"word{i}" for i in range(100))
    after = base + " ChoiceOne selected"
    out = agent_core._page_change_hint(base, "left_click", after)
    assert "MOSTLY unchanged" in out, out
    assert "NOT a navigation" in out
    assert "Verify" in out
    # Sanity: similarity above the 0.92 cutoff.
    assert agent_core._text_similarity(base, after) >= 0.92


@_t("hint:tier_partial_change_ambiguous")
def _():
    # Jaccard = |a∩b| / |a∪b|. With 100 tokens each side and 10 unique on
    # each (90 shared), Jaccard = 90/110 = 0.818 → middle tier (70-92).
    a = " ".join(f"w{i}" for i in range(100))
    b = " ".join([f"w{i}" for i in range(90)] + [f"q{i}" for i in range(10)])
    out = agent_core._page_change_hint(a, "key", b)
    assert "partially changed" in out, out
    sim = agent_core._text_similarity(a, b)
    assert 0.70 <= sim < 0.92, f"wanted middle tier, got sim={sim}"


@_t("hint:tier_major_change_calls_it_navigation")
def _():
    # Trial-12 vs totally different trial-13 with 50 of 100 words different.
    a = " ".join(f"w{i}" for i in range(100))
    b = " ".join([f"w{i}" for i in range(50)] + [f"q{i}" for i in range(50)])
    out = agent_core._page_change_hint(a, "key", b)
    assert "CHANGED significantly" in out, out
    assert "navigation" in out
    sim = agent_core._text_similarity(a, b)
    assert sim < 0.70, f"wanted major tier, got sim={sim}"


@_t("hint:percent_included_in_message")
def _():
    # The percentage must appear so the model sees a quantitative signal
    # it can reason about.
    base = " ".join(f"w{i}" for i in range(100))
    after = base + " extra"
    out = agent_core._page_change_hint(base, "left_click", after)
    assert "%" in out, out


# --- loop detector ----------------------------------------------------------

@_t("stuck:silent_until_threshold")
def _():
    from collections import deque
    d = deque(maxlen=4)
    d.append("p|scroll|-200")
    d.append("p|scroll|-200")
    assert agent_core._stuck_warning(d) == "", "two is not enough"


@_t("stuck:fires_on_three_identical")
def _():
    from collections import deque
    d = deque(maxlen=4)
    for _ in range(3):
        d.append("/survey|scroll|-200")
    out = agent_core._stuck_warning(d)
    assert "STUCK WARNING" in out, out
    assert "scroll|-200" in out
    assert "DIFFERENT" in out


@_t("stuck:silent_when_last_three_differ")
def _():
    from collections import deque
    d = deque(maxlen=4)
    d.append("/survey|scroll|-200")
    d.append("/survey|scroll|-200")
    d.append("/survey|left_click|12,34")
    assert agent_core._stuck_warning(d) == ""


@_t("stuck:silent_across_different_pages")
def _():
    # Same action on three different URL paths is not a loop.
    from collections import deque
    d = deque(maxlen=4)
    d.append("/pageA|scroll|-200")
    d.append("/pageB|scroll|-200")
    d.append("/pageC|scroll|-200")
    assert agent_core._stuck_warning(d) == ""


@_t("sig:click_coords_bucketed")
def _():
    # Jitter within one 50px bucket must hash to the same signature so
    # pw's humanization doesn't defeat the detector.
    s1 = agent_core._action_signature("left_click", {"coordinate": [100, 100]}, "https://x/y")
    s2 = agent_core._action_signature("left_click", {"coordinate": [101, 102]}, "https://x/y")
    s3 = agent_core._action_signature("left_click", {"coordinate": [200, 200]}, "https://x/y")
    assert s1 == s2, f"{s1!r} != {s2!r}"
    assert s1 != s3


@_t("sig:url_path_included")
def _():
    # Different paths on the same host → different signatures.
    a = agent_core._action_signature("scroll", {"pixels": -200}, "https://x/trial1")
    b = agent_core._action_signature("scroll", {"pixels": -200}, "https://x/trial2")
    assert a != b


@_t("sig:text_length_not_content")
def _():
    # Signature must NOT leak typed text content — length only.
    a = agent_core._action_signature("type", {"text": "secret"}, "https://x/y")
    b = agent_core._action_signature("type", {"text": "sixxxx"}, "https://x/y")
    assert a == b
    assert "secret" not in a


# --- re-tiered budgets ------------------------------------------------------

@_t("budget:hurry_inherits_old_super_fast")
def _():
    # Design: hurry now carries what super_fast used to (12k budget), and
    # super_fast is even tighter.
    assert agent_core._CONTEXT_BUDGETS["hurry"] == 12000
    assert agent_core._CONTEXT_BUDGETS["super_fast"] <= 9000


@_t("budget:super_fast_disables_thinking")
def _():
    # In super_fast, call_fara MUST set enable_thinking=False in the
    # chat_template_kwargs regardless of the global ENABLE_THINKING env.
    # We don't actually call the model; we check the payload shape.
    import types
    captured = {}
    def fake_post_chat(url, payload, timeout=120, on_delta=None, extra_headers=None):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "<think></think>\n<tool_call>\n{}\n</tool_call>"}}],
                "timings": {}}
    orig = agent_core.cancel.post_chat
    agent_core.cancel.post_chat = fake_post_chat
    try:
        for mode in ("normal", "hurry"):
            agent_core._speed_mode = mode
            captured.clear()
            agent_core.call_fara([{"role": "user", "content": "hi"}])
            kw = captured["payload"].get("chat_template_kwargs")
            # Normal/hurry respect ENABLE_THINKING (default off in this test env).
            assert kw is not None, f"mode={mode}: chat_template_kwargs missing"
        agent_core._speed_mode = "super_fast"
        captured.clear()
        agent_core.call_fara([{"role": "user", "content": "hi"}])
        kw = captured["payload"].get("chat_template_kwargs")
        assert kw is not None and kw.get("enable_thinking") is False, \
            f"super_fast must force enable_thinking=False, got {kw!r}"
    finally:
        agent_core.cancel.post_chat = orig
        agent_core._speed_mode = "normal"


@_t("budget:max_tokens_shrinks_with_speed")
def _():
    import types
    captured = {}
    def fake_post_chat(url, payload, timeout=120, on_delta=None, extra_headers=None):
        captured.setdefault("max", []).append(payload.get("max_tokens"))
        return {"choices": [{"message": {"content": "<think></think>\n<tool_call>\n{}\n</tool_call>"}}],
                "timings": {}}
    orig = agent_core.cancel.post_chat
    agent_core.cancel.post_chat = fake_post_chat
    try:
        for mode in ("normal", "hurry", "super_fast"):
            agent_core._speed_mode = mode
            agent_core.call_fara([{"role": "user", "content": "hi"}])
        got = captured["max"]
        assert got[0] > got[1] > got[2], f"max_tokens not strictly decreasing: {got}"
    finally:
        agent_core.cancel.post_chat = orig
        agent_core._speed_mode = "normal"


# --- Enter-hint escalation --------------------------------------------------

@_t("mem:enter_directive_fires_on_two_enter_hints")
def _():
    import study_memory
    # Monkeypatch the fs read so we don't touch real state.
    orig_fresh = study_memory._fresh
    orig_read = study_memory._read
    now_ts = 9_999_999_999.0
    study_memory._read = lambda: {
        "example.com": {"user_hints": [
            {"ts": now_ts, "text": "after you select an option you can just press enter"},
            {"ts": now_ts, "text": "no need to scroll, press enter to proceed"},
        ]}
    }
    study_memory._fresh = lambda d: d
    try:
        out = study_memory.user_hints_for("https://example.com/trial/5")
        assert "DIRECTIVE" in out, out
        assert "MUST press Enter" in out
        # The harness auto-chains Enter, so the DIRECTIVE now tells the model
        # NOT to emit a key action itself (pressing Enter twice would advance
        # two trials and submit one with no selection).
        assert "harness ALREADY presses Enter" in out
        assert "NOT emit a key action" in out
    finally:
        study_memory._fresh = orig_fresh
        study_memory._read = orig_read


@_t("mem:enter_directive_silent_on_one_hint")
def _():
    import study_memory
    orig_fresh = study_memory._fresh
    orig_read = study_memory._read
    study_memory._read = lambda: {
        "example.com": {"user_hints": [
            {"ts": 9e9, "text": "press enter after selecting"},
        ]}
    }
    study_memory._fresh = lambda d: d
    try:
        out = study_memory.user_hints_for("https://example.com/")
        assert "DIRECTIVE" not in out, out  # one hint is not enough
        assert "Things the user told you before" in out  # plain block still there
    finally:
        study_memory._fresh = orig_fresh
        study_memory._read = orig_read


@_t("buttongroups:extractor_defined_and_wired")
def _():
    # Pure-string check: _SHARED_JS must contain the new helper AND
    # _getChoiceGroups must call it. Prevents a silent deletion/rename
    # from making the whole button-based batch_fill path disappear.
    import form_fill
    js = form_fill._SHARED_JS
    assert "function _getButtonGroups" in js, "helper not defined"
    assert "_getButtonGroups(consumed)" in js, "not wired into _getChoiceGroups"
    # _isChecked must also recognize class markers now (so a 'selected'
    # button is reported as current when extraction reads the group).
    assert "is-selected" in js and "selected" in js and "aria-pressed" in js, \
        "_isChecked did not pick up class/attr markers"


@_t("buttongroups:is_on_js_in_pw_recognizes_markers")
def _():
    import form_fill_pw
    js = form_fill_pw._IS_ON_JS
    for needle in ("aria-pressed", "aria-selected", "data-selected", "data-checked", "selected"):
        assert needle in js, f"_IS_ON_JS missing marker check: {needle}"


@_t("mem:speed_directive_still_works")
def _():
    import study_memory
    orig_fresh = study_memory._fresh
    orig_read = study_memory._read
    study_memory._read = lambda: {
        "example.com": {"user_hints": [
            {"ts": 9e9, "text": "lavora più velocemente"},
            {"ts": 9e9, "text": "complete this as fast as possible"},
        ]}
    }
    study_memory._fresh = lambda d: d
    try:
        out = study_memory.user_hints_for("https://example.com/")
        assert "DIRECTIVE" in out and "faster" in out, out
        # Enter DIRECTIVE must NOT fire on speed-only hints.
        assert "MUST press Enter" not in out, out
        # Speed-only path keeps the old 'scroll to submit → click submit'
        # rhythm (fine when Enter is NOT in play).
        assert "scroll to submit" in out, out
    finally:
        study_memory._fresh = orig_fresh
        study_memory._read = orig_read


# --- Fix #1: directive ordering + combined-rhythm ----------------------------

@_t("mem:enter_directive_appears_above_speed_when_both_fire")
def _():
    """When both Enter and Speed DIRECTIVEs fire, the Enter block must be
    FIRST in the injected text (prepended last). The old order had Speed
    on top, which pushed 'scroll to submit → click submit' above the
    Enter 'do NOT scroll' line and tripped the model."""
    import study_memory
    orig_fresh = study_memory._fresh
    orig_read = study_memory._read
    study_memory._read = lambda: {
        "example.com": {"user_hints": [
            {"ts": 9e9, "text": "press enter after selecting the option"},
            {"ts": 9e9, "text": "no need to scroll to submit, press enter"},
            {"ts": 9e9, "text": "go as fast as possible, max speed"},
            {"ts": 9e9, "text": "please work faster on these trials"},
        ]}
    }
    study_memory._fresh = lambda d: d
    try:
        out = study_memory.user_hints_for("https://example.com/")
        enter_idx = out.find("MUST press Enter")
        speed_idx = out.find("fast clicking rhythm")
        assert enter_idx >= 0 and speed_idx >= 0, out
        assert enter_idx < speed_idx, (
            "Enter DIRECTIVE must come BEFORE Speed DIRECTIVE "
            f"(enter_idx={enter_idx}, speed_idx={speed_idx}):\n{out}")
    finally:
        study_memory._fresh = orig_fresh
        study_memory._read = orig_read


@_t("mem:speed_directive_uses_enter_rhythm_when_both_fire")
def _():
    """When Enter DIRECTIVE is active, the Speed DIRECTIVE must adopt the
    click-then-Enter rhythm (and must NOT say 'scroll to submit → click
    submit', which contradicts the Enter block right next to it)."""
    import study_memory
    orig_fresh = study_memory._fresh
    orig_read = study_memory._read
    study_memory._read = lambda: {
        "example.com": {"user_hints": [
            {"ts": 9e9, "text": "press enter after selecting the option"},
            {"ts": 9e9, "text": "no need to scroll to submit, press enter"},
            {"ts": 9e9, "text": "go as fast as possible, max speed"},
            {"ts": 9e9, "text": "please work faster on these trials"},
        ]}
    }
    study_memory._fresh = lambda d: d
    try:
        out = study_memory.user_hints_for("https://example.com/")
        # Speed block must contain the Enter rhythm string
        assert "click an option → press Enter" in out, out
        # And must NOT contain the old contradictory rhythm
        assert "scroll to submit → click submit" not in out, (
            "Speed directive still has the contradictory submit path:\n" + out)
    finally:
        study_memory._fresh = orig_fresh
        study_memory._read = orig_read


# --- Fix #1 / #3 companion: enter_hint_count API ----------------------------

@_t("mem:enter_hint_count_api")
def _():
    """study_memory.enter_hint_count returns an int; 2 enter hints → 2,
    1 → 1, zero → 0, missing domain → 0. Used by agent_core._should_auto_chain_enter."""
    import study_memory
    orig_fresh = study_memory._fresh
    orig_read = study_memory._read
    study_memory._read = lambda: {
        "a.com": {"user_hints": [
            {"ts": 9e9, "text": "press enter please"},
            {"ts": 9e9, "text": "no scroll, just press enter"},
            {"ts": 9e9, "text": "unrelated hint"},
        ]},
        "b.com": {"user_hints": [
            {"ts": 9e9, "text": "press enter please"},
        ]},
        "c.com": {"user_hints": []},
    }
    study_memory._fresh = lambda d: d
    try:
        assert study_memory.enter_hint_count("https://a.com/x") == 2
        assert study_memory.enter_hint_count("https://b.com/x") == 1
        assert study_memory.enter_hint_count("https://c.com/x") == 0
        assert study_memory.enter_hint_count("https://missing.com/x") == 0
        assert study_memory.enter_hint_count("") == 0
    finally:
        study_memory._fresh = orig_fresh
        study_memory._read = orig_read


# --- Fix #2: pre-screenshot auto-scroll JS ----------------------------------

@_t("autoscroll:js_has_narrow_selectors")
def _():
    """The auto-scroll JS exists, targets ONLY unselected answer controls
    (not generic buttons), and runs scrollIntoView with instant behaviour
    so the next scrot capture lands on the shifted viewport, not mid-paint."""
    js = agent_core._AUTOSCROLL_TO_CONTROLS_JS
    # Targeted selectors — narrow on purpose. If one of these is dropped
    # the fix stops covering a class of surveys, so the test guards each.
    for needle in (
        ".likert-btn",
        'button[class*="choice"]',
        'button[class*="option"]',
        'button[class*="answer"]',
        'input[type="radio"]:not(:checked)',
        '[role="radio"][aria-checked="false"]',
    ):
        assert needle in js, f"auto-scroll JS missing selector: {needle!r}"
    # Must call scrollIntoView and must specify instant behaviour, else
    # the smooth animation lets scrot catch a mid-paint frame.
    assert "scrollIntoView" in js, js
    assert "behavior: 'instant'" in js, (
        "auto-scroll must be instant; otherwise scrot can tear:\n" + js)
    # Must return one of the three expected tags so the caller can gate
    # the post-scroll sleep.
    for tag in ("'scrolled'", "'in-view'", "'nothing'"):
        assert tag in js, f"auto-scroll JS missing return tag: {tag}"


@_t("autoscroll:helper_swallows_bridge_errors")
def _():
    """_prescroll_to_controls must never raise -- a bridge failure
    (container restart, tab closed) has to fall back to the old model-
    driven scroll, not kill the step."""
    import pw_bridge
    orig = pw_bridge.evaluate
    def _boom(js):
        raise RuntimeError("bridge gone")
    pw_bridge.evaluate = _boom
    try:
        got = agent_core._prescroll_to_controls()
        assert got == "", f"expected '' on error, got {got!r}"
    finally:
        pw_bridge.evaluate = orig


# --- Fix #3: auto-chain Enter after click -----------------------------------

@_t("autochain:fires_when_sf_and_two_enter_hints")
def _():
    """_should_auto_chain_enter is True iff speed mode != normal AND the
    URL's domain has >= 2 Enter hints. This is what gates the extra
    docker_exec('xdotool','key','Return') after a left_click."""
    import study_memory
    orig_fresh = study_memory._fresh
    orig_read = study_memory._read
    orig_mode = agent_core._speed_mode
    study_memory._read = lambda: {
        "eval.services": {"user_hints": [
            {"ts": 9e9, "text": "press enter after selecting"},
            {"ts": 9e9, "text": "no scroll to submit, press enter"},
        ]}
    }
    study_memory._fresh = lambda d: d
    try:
        agent_core._speed_mode = "super_fast"
        assert agent_core._should_auto_chain_enter("https://eval.services/x") is True
        agent_core._speed_mode = "hurry"
        assert agent_core._should_auto_chain_enter("https://eval.services/x") is True
    finally:
        agent_core._speed_mode = orig_mode
        study_memory._fresh = orig_fresh
        study_memory._read = orig_read


@_t("autochain:silent_in_normal_mode")
def _():
    """Normal mode never auto-chains Enter. The gate is deliberate:
    'normal' is the slow, defensive mode where the model does every
    key press itself."""
    import study_memory
    orig_fresh = study_memory._fresh
    orig_read = study_memory._read
    orig_mode = agent_core._speed_mode
    study_memory._read = lambda: {
        "eval.services": {"user_hints": [
            {"ts": 9e9, "text": "press enter after selecting"},
            {"ts": 9e9, "text": "no scroll to submit, press enter"},
        ]}
    }
    study_memory._fresh = lambda d: d
    try:
        agent_core._speed_mode = "normal"
        assert agent_core._should_auto_chain_enter("https://eval.services/x") is False
    finally:
        agent_core._speed_mode = orig_mode
        study_memory._fresh = orig_fresh
        study_memory._read = orig_read


@_t("autochain:silent_when_fewer_than_two_hints")
def _():
    """One Enter hint is NOT enough: the Enter DIRECTIVE itself only
    fires at >=2 hints (deliberate low-pass filter against one-off
    remarks), and auto-chain follows the same threshold."""
    import study_memory
    orig_fresh = study_memory._fresh
    orig_read = study_memory._read
    orig_mode = agent_core._speed_mode
    study_memory._read = lambda: {
        "eval.services": {"user_hints": [
            {"ts": 9e9, "text": "press enter after selecting"},
        ]}
    }
    study_memory._fresh = lambda d: d
    try:
        agent_core._speed_mode = "super_fast"
        assert agent_core._should_auto_chain_enter("https://eval.services/x") is False
        # Zero hints → False
        study_memory._read = lambda: {}
        assert agent_core._should_auto_chain_enter("https://eval.services/x") is False
    finally:
        agent_core._speed_mode = orig_mode
        study_memory._fresh = orig_fresh
        study_memory._read = orig_read


@_t("autochain:safe_when_study_memory_raises")
def _():
    """A broken study_memory read must not break the main loop; the
    helper swallows the exception and returns False, so the step runs
    exactly as before the fix landed."""
    import study_memory
    orig_count = study_memory.enter_hint_count
    orig_mode = agent_core._speed_mode
    def _boom(url):
        raise RuntimeError("mem corrupt")
    study_memory.enter_hint_count = _boom
    try:
        agent_core._speed_mode = "super_fast"
        assert agent_core._should_auto_chain_enter("https://eval.services/x") is False
    finally:
        agent_core._speed_mode = orig_mode
        study_memory.enter_hint_count = orig_count


@_t("parse:valid_json_tool_call")
def _():
    msg = 'thinking\n<tool_call>\n{"name":"computer_use","arguments":{"action":"left_click","coordinate":[415,613]}}\n</tool_call>'
    th, act = agent_core.parse_thoughts_and_action(msg)
    assert act["arguments"]["action"] == "left_click"
    assert act["arguments"]["coordinate"] == [415, 613]


@_t("parse:malformed_terminate_status_does_not_crash")
def _():
    # Real failure shape: unescaped inner quotes + markdown + newline in a
    # long terminate status broke json.loads AND ast.literal_eval and took
    # the whole cycle down. The lenient fallback must still recover the action.
    bad = ('thinking\n<tool_call>\n{"name":"computer_use","arguments":'
           '{"action":"terminate","status":"Study “Romantic Copy”** '
           'with status **Approved**.\n- I clicked the "study row" repeatedly"}}'
           '\n</tool_call>')
    th, act = agent_core.parse_thoughts_and_action(bad)
    assert act["arguments"]["action"] == "terminate"
    assert "Romantic" in act["arguments"].get("status", "")


@_t("parse:malformed_ask_question_with_inner_quotes")
def _():
    bad = ('t\n<tool_call>\n{"name":"computer_use","arguments":'
           '{"action":"ask_user_question","question":"Click "Take part" or wait?"}}'
           '\n</tool_call>')
    th, act = agent_core.parse_thoughts_and_action(bad)
    assert act["arguments"]["action"] == "ask_user_question"


@_t("parse:lenient_extracts_numeric_and_coordinate")
def _():
    assert agent_core._lenient_parse_action(
        '{"arguments":{"action":"scroll","pixels":-552}}')["arguments"]["pixels"] == -552
    c = agent_core._lenient_parse_action(
        '{"arguments":{"action":"left_click","coordinate":[90,357]}}')["arguments"]["coordinate"]
    assert c == [90.0, 357.0]


@_t("parse:lenient_raises_when_no_action")
def _():
    try:
        agent_core._lenient_parse_action('{"arguments":{"foo":"bar"}}')
        assert False, "should have raised"
    except ValueError:
        pass


@_t("formfill:option_match_exact_and_normalized")
def _():
    import form_fill_pw as ff
    opts = ["🅏 I agree", "🅑 Disagree", "Yes"]
    assert ff._match_option("I agree", opts) == "🅏 I agree"       # clean answer vs decorated option
    assert ff._match_option("🅏 I agree", opts) == "🅏 I agree"    # exact
    assert ff._match_option("disagree", opts) == "🅑 Disagree"     # case + symbol
    assert ff._match_option("maybe", opts) is None                # genuinely absent


@_t("formfill:norm_opt_strips_leading_decoration")
def _():
    import form_fill_pw as ff
    assert ff._norm_opt("🅏 I agree") == "i agree"
    assert ff._norm_opt("●  Strongly  Agree ") == "strongly agree"
    assert ff._norm_opt("Yes") == "yes"


@_t("formfill:nav_error_detection")
def _():
    import form_fill_pw as ff
    assert ff._is_nav_error(Exception(
        "Execution context was destroyed, most likely because of a navigation"))
    assert ff._is_nav_error(Exception("Frame was detached"))
    assert not ff._is_nav_error(Exception("option not found: x"))


@_t("formfill:read_delay_bounded_and_handles_none")
def _():
    import form_fill_pw as ff
    # never negative / absurd, and tolerant of a missing field dict
    for f in (None, {"type": "radio"}, {"type": "text", "text": "x" * 400}):
        d = ff._read_delay(f)
        assert 0.3 <= d <= 15, (f, d)


class _FakeBackend:
    """Records the real-input (xdotool) calls batch-fill makes."""
    def __init__(self):
        self.calls = []
    def as_dict(self):
        return {
            "move": lambda x, y: self.calls.append(("move", x, y)),
            "click": lambda: self.calls.append(("click",)),
            "wheel": lambda down, ticks: self.calls.append(("wheel", down, ticks)),
            "type": lambda text: self.calls.append(("type", text)),
            "key": lambda keys, repeat=1: self.calls.append(("key", keys, repeat)),
        }


class _FakeFrame:
    """Stands in for a Playwright frame: returns scripted values for the aim /
    is-on / value JS probes, and has no parent (so _frame_offset is 0,0)."""
    def __init__(self, aim_seq, is_on_seq=None, value=None):
        self.aim_seq = list(aim_seq)
        self.is_on_seq = list(is_on_seq or [])
        self.value = value
        self.parent_frame = None
    def evaluate(self, js, tag=None):
        import form_fill_pw as ff
        if js == ff._AIM_JS:
            return self.aim_seq.pop(0) if self.aim_seq else None
        if js == ff._IS_ON_JS:
            return self.is_on_seq.pop(0) if self.is_on_seq else False
        if js == ff._VALUE_JS:
            return self.value
        return None


def _with_real(be, fn):
    """Run fn() with form_fill_pw's real-input backend stubbed + sleep nulled."""
    import form_fill_pw as ff
    old_real, old_sleep = ff._REAL, ff.time.sleep
    try:
        ff._REAL = be.as_dict()
        ff.time.sleep = lambda s: None
        return fn()
    finally:
        ff._REAL, ff.time.sleep = old_real, old_sleep


@_t("formfill:set_checked_real_click_no_wheel_when_in_view")
def _():
    import form_fill_pw as ff
    be = _FakeBackend()
    def go():
        fr = _FakeFrame(aim_seq=[{"x": 100, "y": 200, "cy": 200, "vh": 900, "ok": True}],
                        is_on_seq=[False, True])   # off before, on after the real click
        return ff._set_checked(fr, "f|0", True)
    assert _with_real(be, go) is True
    kinds = [c[0] for c in be.calls]
    assert "move" in kinds and "click" in kinds, be.calls   # real cursor + click
    assert "wheel" not in kinds, be.calls                   # already in view


@_t("formfill:scroll_into_view_uses_real_wheel")
def _():
    import form_fill_pw as ff
    be = _FakeBackend()
    def go():
        fr = _FakeFrame(aim_seq=[{"x": 100, "y": 1200, "cy": 1200, "vh": 900, "ok": False},
                                 {"x": 100, "y": 400, "cy": 400, "vh": 900, "ok": True}])
        return ff._scroll_into_view_real(fr, "f|0")
    aim = _with_real(be, go)
    assert aim and aim["ok"], aim
    wheels = [c for c in be.calls if c[0] == "wheel"]
    assert wheels and wheels[0][1] is True, be.calls        # real wheel, scrolled DOWN


@_t("formfill:type_text_real_keyboard")
def _():
    import form_fill_pw as ff
    be = _FakeBackend()
    def go():
        fr = _FakeFrame(aim_seq=[{"x": 50, "y": 100, "cy": 100, "vh": 900, "ok": True}],
                        value="hello")
        return ff._type_text(fr, "f", "hello")
    assert _with_real(be, go) is True
    assert ("key", "ctrl+a", 1) in be.calls, be.calls       # select-all, then real typing
    assert ("type", "hello") in be.calls, be.calls


@_t("formfill:real_input_failure_returns_false_no_dom")
def _():
    import form_fill_pw as ff
    be = _FakeBackend()
    def go():
        # never hittable: scroll gives up, _set_checked returns False (left for the
        # vision path) instead of writing the DOM -- nothing flaggable
        fr = _FakeFrame(aim_seq=[{"x": 1, "y": 2000, "cy": 2000, "vh": 900, "ok": False}] * 13,
                        is_on_seq=[False])
        return ff._set_checked(fr, "f|0", True)
    assert _with_real(be, go) is False


@_t("complete:pushes_back_on_survey_host_thankyou")
def _():
    msg = agent_core.completion_pushback(
        "Completed the study successfully. Thank you for your participation!",
        "https://eval.services/?evaluation_id=abc&study_id=xyz",
        [], [], False)
    assert msg, "free-text done on survey host with no code must push back"
    assert "eval.services" in msg, msg          # names the host it is stuck on
    assert "Prolific" in msg


@_t("complete:accepts_code_answer")
def _():
    msg = agent_core.completion_pushback(
        "CODE: CSA95SMN", "https://surreyfbel.qualtrics.com/jfe/form/SV_x",
        [], [], False)
    assert msg == "", "a CODE: answer is a real outcome, not a false stop"


@_t("complete:accepts_nocode_answer")
def _():
    assert agent_core.completion_pushback(
        "NOCODE", "https://eval.services/x", [], [], False) == ""


@_t("complete:no_pushback_when_back_on_prolific")
def _():
    msg = agent_core.completion_pushback(
        "Completed the study", "https://app.prolific.com/submissions", [], [], False)
    assert msg == "", "already back on Prolific: nothing to nudge"


@_t("complete:no_pushback_when_a_code_was_seen")
def _():
    assert agent_core.completion_pushback(
        "Completed the study", "https://eval.services/x", ["C1A2B3"], [], False) == "", \
        "a code seen earlier in the run counts as completion"
    assert agent_core.completion_pushback(
        "Completed the study", "https://eval.services/x", [], ["C1A2B3"], False) == "", \
        "a code on the end page counts as completion"


@_t("complete:fires_at_most_once_per_study")
def _():
    assert agent_core.completion_pushback(
        "Completed the study", "https://eval.services/x", [], [], True) == "", \
        "already pushed back once: must not loop"


@_t("click:checkbox_miss_retries_at_dom_centre")
def _():
    orig = (agent_core._human_move, agent_core.docker_exec, agent_core._nearest_checkbox)
    moves, clicks = [], []
    try:
        agent_core._human_move = lambda x, y: moves.append((x, y))
        agent_core.docker_exec = lambda *a, **k: clicks.append(a)
        # a checkbox sits at (509,747); the click at (482,747) misses it, so its
        # state is unchanged before/after -> a retry at the real centre is expected
        agent_core._nearest_checkbox = lambda x, y, *a, **k: {"cx": 509, "cy": 747, "checked": False}
        agent_core.dispatch_action("left_click", {"coordinate": [482, 747]})
    finally:
        agent_core._human_move, agent_core.docker_exec, agent_core._nearest_checkbox = orig
    assert len(clicks) == 2, f"a missed checkbox click must retry once, got {len(clicks)}"
    assert abs(moves[-1][0] - 509) <= 2 and abs(moves[-1][1] - 747) <= 2, \
        f"retry must aim at the checkbox centre (509,747), not the missed point: {moves[-1]}"


@_t("click:checkbox_toggled_no_retry")
def _():
    orig = (agent_core._human_move, agent_core.docker_exec, agent_core._nearest_checkbox)
    clicks = []
    states = iter([{"cx": 509, "cy": 747, "checked": False},    # before the click
                   {"cx": 509, "cy": 747, "checked": True}])     # after: it toggled
    try:
        agent_core._human_move = lambda x, y: None
        agent_core.docker_exec = lambda *a, **k: clicks.append(a)
        agent_core._nearest_checkbox = lambda x, y, *a, **k: next(states)
        agent_core.dispatch_action("left_click", {"coordinate": [482, 747]})
    finally:
        agent_core._human_move, agent_core.docker_exec, agent_core._nearest_checkbox = orig
    assert len(clicks) == 1, f"a checkbox that toggled must not be re-clicked, got {len(clicks)}"


@_t("click:no_checkbox_single_click")
def _():
    orig = (agent_core._human_move, agent_core.docker_exec, agent_core._nearest_checkbox)
    clicks = []
    try:
        agent_core._human_move = lambda x, y: None
        agent_core.docker_exec = lambda *a, **k: clicks.append(a)
        agent_core._nearest_checkbox = lambda x, y, *a, **k: None
        agent_core.dispatch_action("left_click", {"coordinate": [100, 100]})
    finally:
        agent_core._human_move, agent_core.docker_exec, agent_core._nearest_checkbox = orig
    assert len(clicks) == 1, f"a normal click (no checkbox near) must not retry, got {len(clicks)}"


@_t("loophome:keeps_prolific_studies_list")
def _():
    # already on the Prolific list: kept as-is
    assert agent_core.loop_home_for("https://app.prolific.com/studies") == \
        "https://app.prolific.com/studies"


@_t("loophome:study_detail_maps_to_list")
def _():
    assert agent_core.loop_home_for(
        "https://app.prolific.com/studies/6adf5f7c60e9893e06c36664") == agent_core.studies_url()


@_t("loophome:external_survey_maps_to_prolific_list")
def _():
    # a finished eval.services completion page must NOT become the loop home,
    # otherwise the loop re-opens it and re-reads the same code every cycle
    out = agent_core.loop_home_for(
        "https://eval.services/?evaluation_id=x&prolific_pid=y&study_id=z&session_id=w")
    assert out == agent_core.studies_url(), out


@_t("loophome:submission_complete_maps_to_list")
def _():
    assert agent_core.loop_home_for(
        "https://app.prolific.com/submissions/complete?cc=C7737BD52") == agent_core.studies_url()


@_t("progress:counter_extracted")
def _():
    assert agent_core._progress_counter("4/35 Time: 6:06 Select (1)") == (4, 35)
    assert agent_core._progress_counter("no counter here") is None
    assert agent_core._progress_counter("ratio 40/35 is invalid") is None   # n > tot


@_t("pagesignal:counter_move_reads_as_advance")
def _():
    prev = ("4/35 Time: 6:06 Select (1) Select (2) It's a Draw Ready to Submit? "
            "Submit Vote Press Enter to submit D for draw")
    curr = ("5/35 Time: 6:20 Select (1) Select (2) It's a Draw Ready to Submit? "
            "Submit Vote Press Enter to submit D for draw")
    out = agent_core._page_change_hint(prev, "key", curr)
    assert "ADVANCED" in out and "5/35" in out and "5 of 35" in out, out
    # the two trials are near-identical text, so without the counter check the
    # similarity tier would NOT classify this as a clear navigation
    assert agent_core._text_similarity(prev, curr) < 0.92


@_t("pagesignal:same_trial_selection_not_advance")
def _():
    prev = "4/35 Time: 6:06 Select (1) Select (2) It's a Draw Ready to Submit? Submit Vote"
    curr = ("4/35 Time: 6:08 Choice 1 selected Select (1) Select (2) It's a Draw "
            "Ready to Submit? Submit Vote")
    out = agent_core._page_change_hint(prev, "left_click", curr)
    assert "ADVANCED" not in out, out      # same trial counter: must not claim an advance


@_t("pagesignal:image_swap_lowers_similarity")
def _():
    # the fingerprint now includes image srcs, so swapping the trial's images
    # (same layout text, same counter) no longer reads as "mostly unchanged"
    base = "4/35 Select (1) Select (2) It's a Draw Ready to Submit? Submit Vote"
    prev = base + " https://eval.services/i/a1.png https://eval.services/i/b1.png"
    curr = base + " https://eval.services/i/a2.png https://eval.services/i/b2.png"
    out = agent_core._page_change_hint(prev, "left_click", curr)
    assert "MOSTLY unchanged" not in out, out
    assert agent_core._text_similarity(prev, curr) < 0.92


@_t("identity:hint_includes_id_and_email")
def _():
    out = agent_core._identity_hint_text(
        {"prolific_id": "6577185f506eacce0e8de1ed", "email": "a@b.co"})
    assert "6577185f506eacce0e8de1ed" in out and "a@b.co" in out, out
    assert "exactly" in out.lower()          # tells the model to copy, not invent
    assert "never" in out.lower()            # and never fabricate / refuse


@_t("identity:hint_empty_without_values")
def _():
    assert agent_core._identity_hint_text({}) == ""
    assert agent_core._identity_hint_text({"other": "x"}) == ""
    assert agent_core._identity_hint_text(None) == ""


if __name__ == "__main__":
    passed = sum(1 for _, s, _ in _RESULTS if s == "PASS")
    failed = sum(1 for _, s, _ in _RESULTS if s != "PASS")
    print(f"\n== SUMMARY ==  {passed}/{len(_RESULTS)} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
