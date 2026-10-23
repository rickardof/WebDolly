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
    # 8 assistant turns, max=6, trim_to=3 → strip 5 oldest
    msgs = [{"role": "user", "content": "hi"}]
    for i in range(8):
        msgs.append(_mk_turn(f"reasoning {i}"))
    agent_core._strip_old_thoughts(msgs)
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    stripped = [m for m in assistant_msgs if m["content"].startswith("<tool_call>")]
    kept     = [m for m in assistant_msgs if not m["content"].startswith("<tool_call>")]
    assert len(stripped) == 5, f"expected 5 stripped, got {len(stripped)}"
    assert len(kept) == 3, f"expected 3 kept intact (trim_to), got {len(kept)}"


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
    # 5 turns, max=6 → should NOT strip
    msgs = [{"role": "user", "content": "hi"}] + [_mk_turn(f"r{i}") for i in range(5)]
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


@_t("hash:whitespace_normalised")
def _():
    # The helper hashes txt not the browser, so call it indirectly via the
    # private pathway: build the normalised string ourselves and MD5 it.
    import hashlib
    def norm(s): return agent_core._WS_RE.sub(" ", s).strip()
    assert hashlib.md5(norm("a   b\n\n c").encode()).hexdigest() \
        == hashlib.md5(norm("a b c").encode()).hexdigest()
    assert hashlib.md5(norm("a b c").encode()).hexdigest() \
        != hashlib.md5(norm("a b d").encode()).hexdigest()


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


@_t("hint:change_signal_on_advance")
def _():
    out = agent_core._page_change_hint("aaa", "left_click", "bbb")
    assert "CHANGED" in out, f"expected CHANGED signal, got {out!r}"
    assert "left_click" in out


@_t("hint:no_change_signal_for_key_enter")
def _():
    # The exact case the user flagged: Enter pressed, page unchanged → model
    # must learn it had no effect, not assume success.
    out = agent_core._page_change_hint("aaa", "key", "aaa")
    assert "NOT change" in out, f"expected NOT change signal, got {out!r}"
    assert "key" in out


@_t("hint:change_signal_for_key_enter_when_advanced")
def _():
    # And the opposite: Enter pressed, page content DID change → model
    # must learn it submitted and advanced, not treat new trial as failure.
    out = agent_core._page_change_hint("aaa", "key", "bbb")
    assert "CHANGED" in out, f"expected CHANGED signal, got {out!r}"


if __name__ == "__main__":
    passed = sum(1 for _, s, _ in _RESULTS if s == "PASS")
    failed = sum(1 for _, s, _ in _RESULTS if s != "PASS")
    print(f"\n== SUMMARY ==  {passed}/{len(_RESULTS)} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
