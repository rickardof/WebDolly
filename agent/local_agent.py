"""Local Agent: text-based chat with internet + machine access via native function calling.

Uses whichever model is loaded on llama-server (typically Qwen3-27B).
No vision — uses Playwright to navigate and reads page text.

Tools:
  - web_search(query):        search DuckDuckGo and return results
  - browse_url(url):          navigate the browser and return page text
  - read_file(path):          read a local file
  - write_file(path, content): write a local file
  - list_dir(path):           list a directory's contents
  - run_shell(command):       run a shell command
  - run_python(code):         execute Python code

NOTE: the shell/python/file tools run on the host as the server user, with
no sandbox. This agent can read, write and execute anything that user can.
"""

import json
import logging
import os
import re
import subprocess
import urllib.parse
from pathlib import Path

import cancel
import pw_bridge

log = logging.getLogger(__name__)

MODEL_URL = os.environ.get("FARA_URL", "http://127.0.0.1:8090/v1/chat/completions")

# Working directory for shell/python/relative file paths.
WORK_DIR = os.environ.get("FARA_LOCAL_WORKDIR", str(Path.home()))

SYSTEM_PROMPT = (
    "You are a capable assistant with internet access and full access to the "
    "local machine (files, shell, Python). Use the provided tools when needed:\n"
    "- web_search / browse_url for the web\n"
    "- read_file / write_file / list_dir for files\n"
    "- run_shell for terminal commands, run_python for Python code\n"
    f"The working directory is {WORK_DIR}. Relative paths resolve from there.\n"
    "Be efficient: after you have what you need, answer concisely. "
    "For web facts, cite sources. Do not run destructive commands unless the user clearly asks."
)

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web and get a list of results with titles, URLs and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse_url",
            "description": "Navigate the browser to a URL and return the page text content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to visit"}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a local file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (absolute or relative to the working dir)"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write (create or overwrite) a local file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (absolute or relative to the working dir)"},
                    "content": {"type": "string", "description": "The full file content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the files and subdirectories of a local directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (defaults to the working dir)"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command and return its stdout, stderr and exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute Python 3 code and return its stdout, stderr and exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "The Python code to execute"}
                },
                "required": ["code"],
            },
        },
    },
]

MAX_PAGE_CHARS = 6000
MAX_FILE_CHARS = 20000
MAX_OUTPUT_CHARS = 8000
EXEC_TIMEOUT = 60
MAX_TOOL_ROUNDS = 8
MAX_HISTORY_TOKENS = 22000
MAX_TOOL_HISTORY_CHARS = 6000
MAX_RESPONSE_TOKENS = 8192
MAX_RETRY_RESPONSE_TOKENS = 4096
ENABLE_THINKING = os.environ.get("FARA_LOCAL_ENABLE_THINKING", "1") == "1"
# This Qwen variant's chat template only accepts {low, medium, xhigh}; map the
# conventional "high" label to xhigh so the UI can stay generic.
_EFFORT_ALIASES = {"high": "xhigh"}
REASONING_EFFORT = _EFFORT_ALIASES.get(
    os.environ.get("FARA_LOCAL_REASONING_EFFORT", "medium"),
    os.environ.get("FARA_LOCAL_REASONING_EFFORT", "medium"))


# ── Tools ────────────────────────────────────────────────────────────

def _web_search(query: str) -> str:
    """Search DuckDuckGo via the browser and return results as text."""
    try:
        q = urllib.parse.quote_plus(query)
        search_url = f"https://html.duckduckgo.com/html/?q={q}"

        def _search(b):
            page = b.active_page()
            page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)
            return page.evaluate(r"""() => {
                function cleanDdg(raw) {
                    try {
                        const u = new URL(raw);
                        if (u.hostname.includes('duckduckgo.com') && u.searchParams.has('uddg'))
                            return u.searchParams.get('uddg');
                    } catch(e) {}
                    return raw;
                }
                const results = [];
                for (const a of document.querySelectorAll('a.result__a')) {
                    const href = cleanDdg(a.href);
                    if (!href || !href.startsWith('http')) continue;
                    const title = a.textContent.trim();
                    if (!title) continue;
                    let snippet = '';
                    const block = a.closest('.result');
                    if (block) {
                        const snipEl = block.querySelector('.result__snippet');
                        if (snipEl) snippet = snipEl.textContent.trim().slice(0, 200);
                    }
                    if (!results.some(r => r.href === href))
                        results.push({title, href, snippet});
                    if (results.length >= 6) break;
                }
                return results;
            }""")
        results = pw_bridge.run(_search, timeout=25)
    except Exception as e:
        return f"Search failed: {e}"

    if not results:
        return f"No results found for: {query}"

    lines = [f'Search results for "{query}":\n']
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}\n    {r['href']}\n    {r.get('snippet', '')}")
    return "\n\n".join(lines)


def _browse_url(url: str) -> str:
    """Navigate the browser to a URL and return the page text."""
    try:
        def _nav(b):
            page = b.active_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1500)
            return page.inner_text("body")
        text = pw_bridge.run(_nav, timeout=30)
    except Exception as e:
        return f"Failed to load {url}: {e}"

    if not text or not text.strip():
        return f"Page loaded ({url}) but no readable text content found."

    text = re.sub(r'\n{3,}', '\n\n', text.strip())
    if len(text) > MAX_PAGE_CHARS:
        text = text[:MAX_PAGE_CHARS] + f"\n\n[... truncated, {len(text)} chars total]"
    return f"Page content ({url}):\n\n{text}"


def _resolve(path: str) -> Path:
    """Resolve a path relative to the working dir, expanding ~."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(WORK_DIR) / p
    return p


def _read_file(path: str) -> str:
    try:
        p = _resolve(path)
        if not p.exists():
            return f"File not found: {path}"
        if p.is_dir():
            return f"{path} is a directory — use list_dir instead."
        text = p.read_text(errors="replace")
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + f"\n\n[... truncated, {len(text)} chars total]"
        return f"Content of {p}:\n\n{text}"
    except Exception as e:
        return f"Failed to read {path}: {e}"


def _write_file(path: str, content: str) -> str:
    try:
        p = _resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Wrote {len(content)} chars to {p}"
    except Exception as e:
        return f"Failed to write {path}: {e}"


def _list_dir(path: str) -> str:
    try:
        p = _resolve(path) if path else Path(WORK_DIR)
        if not p.exists():
            return f"Directory not found: {path}"
        if not p.is_dir():
            return f"{path} is not a directory."
        entries = []
        for e in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if e.is_dir():
                entries.append(f"  [dir]  {e.name}/")
            else:
                try:
                    size = e.stat().st_size
                except Exception:
                    size = 0
                entries.append(f"  [file] {e.name} ({size}b)")
        body = "\n".join(entries) if entries else "  (empty)"
        return f"Contents of {p}:\n{body}"
    except Exception as e:
        return f"Failed to list {path}: {e}"


def _format_exec(returncode: int, stdout: str, stderr: str) -> str:
    combined = (stdout or "").strip()
    if stderr and stderr.strip():
        combined += ("\n" if combined else "") + f"[stderr]:\n{stderr.strip()}"
    combined = combined or "(no output)"
    if len(combined) > MAX_OUTPUT_CHARS:
        combined = combined[:MAX_OUTPUT_CHARS] + "\n[... output truncated]"
    return f"Exit code: {returncode}\n{combined}"


def _run_shell(command: str) -> str:
    if not command.strip():
        return "No command provided."
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True,
                           timeout=EXEC_TIMEOUT, cwd=WORK_DIR)
        return _format_exec(r.returncode, r.stdout, r.stderr)
    except subprocess.TimeoutExpired:
        return f"Command timed out after {EXEC_TIMEOUT}s."
    except Exception as e:
        return f"Command failed: {e}"


def _run_python(code: str) -> str:
    if not code.strip():
        return "No code provided."
    try:
        r = subprocess.run(["python3", "-c", code], capture_output=True, text=True,
                           timeout=EXEC_TIMEOUT, cwd=WORK_DIR)
        return _format_exec(r.returncode, r.stdout, r.stderr)
    except subprocess.TimeoutExpired:
        return f"Python execution timed out after {EXEC_TIMEOUT}s."
    except Exception as e:
        return f"Python execution failed: {e}"


TOOL_DISPATCH = {
    "web_search": lambda args: _web_search(args.get("query", "")),
    "browse_url": lambda args: _browse_url(args.get("url", "")),
    "read_file": lambda args: _read_file(args.get("path", "")),
    "write_file": lambda args: _write_file(args.get("path", ""), args.get("content", "")),
    "list_dir": lambda args: _list_dir(args.get("path", "")),
    "run_shell": lambda args: _run_shell(args.get("command", "")),
    "run_python": lambda args: _run_python(args.get("code", "")),
}


def _clean_response(text: str) -> str:
    """Remove residual tool-call markup from final response text."""
    text = re.sub(r'</?tool_call>.*', '', text, flags=re.DOTALL).strip()
    text = re.sub(r'<function=\w+>.*', '', text, flags=re.DOTALL).strip()
    return text


def _split_think(text: str) -> tuple[str, str]:
    """Return (thinking, visible). Keeps the reasoning so the UI can show it
    collapsed, instead of discarding it like _strip_think used to."""
    parts = []
    def _grab(m):
        parts.append(m.group(1))
        return ''
    visible = re.sub(r'<think>(.*?)</think>', _grab, text, flags=re.DOTALL)
    m = re.search(r'<think>(.*)$', visible, flags=re.DOTALL)
    if m:
        parts.append(m.group(1))
        visible = visible[:m.start()]
    visible = visible.strip()
    thinking = "\n".join(p.strip() for p in parts if p.strip()).strip()
    # Fallback: if everything was inside <think>, show the raw content minus
    # tags rather than return nothing (matches the old _strip_think behaviour).
    if not visible and thinking:
        visible = re.sub(r'</?think>', '', text).strip()
    return thinking, visible


def _strip_think(text: str) -> str:
    return _split_think(text)[1]


def _is_empty_or_garbage(text: str) -> bool:
    """Detect an unfinished reasoning stream instead of showing it as an answer."""
    return not text or not re.search(r"[A-Za-z0-9À-ÿ]", text)


def _message_tokens(message: dict) -> int:
    """Conservatively estimate serialized message tokens before sending."""
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    return max(1, (len(body) + 3) // 4)


# Reserve this fraction of the budget for the recent-turn window; the rest
# can be used by the system prompt + rolling compact summary.
COMPACT_KEEP_FRACTION = float(os.environ.get("FARA_LOCAL_COMPACT_KEEP", "0.5"))
COMPACT_SUMMARY_MARKER = "[Earlier conversation summary]"


def _units(messages: list) -> list:
    """Group messages into turn-units (assistant+tool_calls stay together)."""
    units = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            unit = [m]; i += 1
            while i < len(messages) and messages[i].get("role") == "tool":
                unit.append(messages[i]); i += 1
        else:
            unit = [m]; i += 1
        units.append(unit)
    return units


def _summarize_units(units: list, prior_summary: str = "") -> str:
    """Ask the model itself to compress these turns into a short recap.

    Called synchronously inside _trim_history when history would overflow.
    Falls back to a mechanical bullet list if the model call fails, so a
    transient summary error never breaks the chat path.
    """
    transcript_lines = []
    for u in units:
        for m in u:
            role = m.get("role", "?")
            if role == "tool":
                name = m.get("name", "tool")
                body = (m.get("content") or "")[:400]
                transcript_lines.append(f"[{name} result] {body}")
            elif role == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn = (tc.get("function") or {})
                    args = (fn.get("arguments") or "")[:200]
                    transcript_lines.append(f"[assistant→{fn.get('name','tool')}] {args}")
                extra = _strip_think(m.get("content") or "")
                if extra: transcript_lines.append(f"assistant: {extra[:400]}")
            else:
                content = m.get("content") or ""
                if role == "assistant": content = _strip_think(content)
                if content:
                    transcript_lines.append(f"{role}: {content[:600]}")
    transcript = "\n".join(transcript_lines)[:12000]
    prior = f"\n\nPrior summary so far:\n{prior_summary}\n" if prior_summary else ""
    prompt = (
        "Summarize the following conversation turns into a compact recap for "
        "future reference. Preserve: facts the user shared, decisions, "
        "ongoing tasks, and anything the assistant must remember. Omit "
        "small talk. 10 bullet points maximum, each ≤30 words. Output "
        "only the bullet list, no preamble.\n\n"
        f"Turns to summarize:\n{transcript}{prior}"
    )
    payload = {
        "model": "local",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    if MODEL_URL.startswith(("http://127.0.0.1:", "http://localhost:")):
        payload["chat_template_kwargs"] = {"enable_thinking": False,
                                           "reasoning_effort": "low"}
    try:
        result = cancel.post_chat(MODEL_URL, payload, timeout=120)
        text = _strip_think((result["choices"][0]["message"].get("content") or "")).strip()
        if text:
            return text
    except Exception as e:
        log.warning("Compact summarization failed: %s", e)
    # Mechanical fallback
    bullets = []
    for line in transcript_lines[-20:]:
        bullets.append(f"- {line[:140]}")
    return "\n".join(bullets) or "- (no summary available)"


def _split_system_summary(content: str) -> tuple[str, str]:
    """Separate the base system prompt from any embedded compact summary.

    The summary is folded into the single system message so Qwen's template,
    which forbids a second system message after the first, still accepts it."""
    if not content:
        return "", ""
    marker = f"\n\n{COMPACT_SUMMARY_MARKER}\n"
    idx = content.find(marker)
    if idx < 0:
        return content, ""
    base = content[:idx]
    summary = content[idx + len(marker):].strip()
    return base, summary


def _trim_history(history: list) -> None:
    """Keep recent complete turns, summarizing older ones into a compact note
    merged into the system message so the model retains the gist of prior
    conversation without needing a second system turn."""
    if not history:
        return

    if history[0].get("role") == "system":
        system_base, prior_summary = _split_system_summary(history[0].get("content") or "")
        system = [{"role": "system", "content": system_base}]
        rest = history[1:]
    else:
        system = []
        system_base = ""
        prior_summary = ""
        rest = history[:]

    units = _units(rest)

    sys_tokens = sum(_message_tokens(m) for m in system)
    prior_tokens = (len(prior_summary) + 3) // 4 if prior_summary else 0

    kept = []
    kept_tokens = 0
    budget_keep = int(MAX_HISTORY_TOKENS * COMPACT_KEEP_FRACTION)
    have_user = False
    for unit in reversed(units):
        ut = sum(_message_tokens(m) for m in unit)
        unit_has_user = any(m.get("role") == "user" for m in unit)
        over_budget = kept and kept_tokens + ut > budget_keep
        # Qwen's chat template requires at least one real user query in the
        # messages passed to it. Keep walking back past the budget until we
        # have one in `kept`, otherwise the next model call raises
        # "No user query found in messages."
        if over_budget and have_user:
            break
        kept[0:0] = [unit]; kept_tokens += ut
        if unit_has_user: have_user = True

    dropped_units = units[: len(units) - len(kept)] if kept else units
    total = sys_tokens + prior_tokens + kept_tokens

    if not dropped_units and total <= MAX_HISTORY_TOKENS:
        # Rewrite with the existing summary folded back into the system msg.
        if system and prior_summary:
            system[0] = {"role": "system",
                         "content": f"{system_base}\n\n{COMPACT_SUMMARY_MARKER}\n{prior_summary}"}
        history[:] = system + [m for u in units for m in u]
        return

    if dropped_units:
        new_summary = _summarize_units(dropped_units, prior_summary=prior_summary)
    else:
        new_summary = prior_summary

    if system and new_summary:
        system[0] = {"role": "system",
                     "content": f"{system_base}\n\n{COMPACT_SUMMARY_MARKER}\n{new_summary}"}
    rebuilt = system + [m for u in kept for m in u]

    log.info(
        "Compacted local chat: %d msgs -> %d (summary %d chars, kept %d turns)",
        len(history), len(rebuilt), len(new_summary), len(kept),
    )
    history[:] = rebuilt


# ── Agent loop ───────────────────────────────────────────────────────

def run_chat(user_message: str, history: list, on_event=None):
    """Run a chat turn with native function calling.

    Args:
        user_message: the user's message
        history: list of message dicts (mutable, appended to)
        on_event: callback for real-time events

    Yields event dicts and returns via the final "response" event.
    """
    if not history:
        history.append({"role": "system", "content": SYSTEM_PROMPT})

    history.append({"role": "user", "content": user_message})
    _trim_history(history)
    seen_calls = set()
    # Track where the user turn started so we can roll it back on failure —
    # a dangling user message with no assistant reply poisons the next turn.
    _user_turn_start = len(history) - 1

    for round_n in range(MAX_TOOL_ROUNDS):
        yield {"type": "thinking"}

        try:
            _trim_history(history)
            payload = {
                "model": "local",
                "messages": history,
                "tools": TOOLS_SCHEMA,
                "temperature": 0.6,
                "max_tokens": MAX_RESPONSE_TOKENS,
            }
            if MODEL_URL.startswith(("http://127.0.0.1:", "http://localhost:")):
                payload["chat_template_kwargs"] = {
                    "enable_thinking": ENABLE_THINKING,
                    "reasoning_effort": REASONING_EFFORT,
                }
            result = cancel.post_chat(MODEL_URL, payload, timeout=120,
                                      on_delta=lambda t: (on_event or (lambda e: None))(
                                          {"type": "token", "text": t}))
            msg = result["choices"][0]["message"]
            thinking, content = _split_think(msg.get("content") or "")
            tool_calls = msg.get("tool_calls") or []
            finish = result["choices"][0].get("finish_reason", "")

            # Retry when the model ran out of budget. Two cases:
            # - all budget spent inside <think>, nothing visible left
            # - visible answer got cut mid-sentence (finish=="length" with content)
            # In both, disable thinking and give it room for a plain answer.
            needs_retry = (
                finish == "length" and not tool_calls and ENABLE_THINKING
                and "chat_template_kwargs" in payload
                and (_is_empty_or_garbage(content) or thinking))
            if needs_retry:
                retry_payload = dict(payload)
                retry_payload["max_tokens"] = MAX_RETRY_RESPONSE_TOKENS
                retry_payload["chat_template_kwargs"] = {
                    "enable_thinking": False,
                    "reasoning_effort": "low",
                }
                result = cancel.post_chat(MODEL_URL, retry_payload, timeout=120)
                msg = result["choices"][0]["message"]
                thinking2, content = _split_think(msg.get("content") or "")
                thinking = thinking or thinking2
                tool_calls = msg.get("tool_calls") or []
                finish = result["choices"][0].get("finish_reason", "")

            if thinking:
                yield {"type": "think", "text": thinking}
        except cancel.Cancelled:
            # Drop the dangling user turn so the next message starts clean.
            del history[_user_turn_start:]
            yield {"type": "error", "message": "Stopped"}
            return
        except Exception as e:
            del history[_user_turn_start:]
            yield {"type": "error", "message": str(e)}
            return

        # Tool calls take precedence over finish_reason. Some llama-server
        # versions omit or vary the reason while still returning valid calls.
        if not tool_calls:
            content = _clean_response(content)
            if not _is_empty_or_garbage(content):
                history.append({"role": "assistant", "content": content})
                yield {"type": "response", "content": content}
            else:
                del history[_user_turn_start:]
                log.warning(
                    "Local model returned no answer: finish_reason=%r, "
                    "message_keys=%s",
                    finish,
                    sorted(msg),
                )
                yield {
                    "type": "error",
                    "message": (
                        "The local model returned no answer "
                        f"(finish_reason={finish or 'unknown'})."
                    ),
                }
            return

        # Process tool calls
        history.append({"role": "assistant", "content": content,
                        "tool_calls": tool_calls})

        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            try:
                tool_args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                tool_args = {}
            tc_id = tc.get("id", "")

            # Loop detection
            call_key = json.dumps({"n": tool_name, "a": tool_args}, sort_keys=True)
            if call_key in seen_calls:
                history.append({"role": "tool", "tool_call_id": tc_id,
                                "content": "Already called with same arguments. Answer the user now."})
                continue
            seen_calls.add(call_key)

            yield {"type": "tool_call", "name": tool_name, "args": tool_args}

            if tool_name == "browse_url":
                yield {"type": "browsing", "url": tool_args.get("url", "")}

            handler = TOOL_DISPATCH.get(tool_name)
            if handler is None:
                tool_result = f"Unknown tool: {tool_name}"
            else:
                try:
                    tool_result = handler(tool_args)
                except Exception as e:
                    tool_result = f"Tool error: {e}"

            yield {"type": "tool_result", "name": tool_name,
                   "result": tool_result[:500] + ("..." if len(tool_result) > 500 else "")}

            history.append({"role": "tool", "tool_call_id": tc_id,
                            "content": tool_result[:MAX_TOOL_HISTORY_CHARS]})

    # Force answer after exhausting rounds. Build a compact, text-only context:
    # some servers reject a new user message after a long tool-call chain, and
    # retaining every tool-call envelope wastes the context needed for the answer.
    final_history = []
    if history and history[0].get("role") == "system":
        final_history.append(history[0])
    for message in history[1:]:
        if message.get("role") == "user":
            final_history.append(message)
        elif message.get("role") == "tool":
            final_history.append({
                "role": "user",
                "content": f"[Tool result]\n{message.get('content', '')}",
            })
        elif message.get("role") == "assistant" and not message.get("tool_calls"):
            final_history.append({
                "role": "assistant",
                "content": message.get("content", ""),
            })
    final_history.append({
        "role": "user",
        "content": "Answer the user's question now using the information above. "
                   "Do not call tools or describe tool use.",
    })
    _trim_history(final_history)
    try:
        payload = {
            "model": "local",
            "messages": final_history,
            "temperature": 0.4,
            "max_tokens": MAX_RESPONSE_TOKENS,
        }
        if MODEL_URL.startswith(("http://127.0.0.1:", "http://localhost:")):
            payload["chat_template_kwargs"] = {
                "enable_thinking": ENABLE_THINKING,
                "reasoning_effort": REASONING_EFFORT,
            }
        result = cancel.post_chat(MODEL_URL, payload, timeout=120)
        msg = result["choices"][0]["message"]
        thinking, raw = _split_think(msg.get("content") or "")
        content = _clean_response(raw)
        finish = result["choices"][0].get("finish_reason", "")

        if ((_is_empty_or_garbage(content) or (finish == "length" and thinking))
                and ENABLE_THINKING
                and "chat_template_kwargs" in payload):
            retry_payload = dict(payload)
            retry_payload["max_tokens"] = MAX_RETRY_RESPONSE_TOKENS
            retry_payload["chat_template_kwargs"] = {
                "enable_thinking": False,
                "reasoning_effort": "low",
            }
            result = cancel.post_chat(MODEL_URL, retry_payload, timeout=120)
            msg = result["choices"][0]["message"]
            thinking2, raw = _split_think(msg.get("content") or "")
            thinking = thinking or thinking2
            content = _clean_response(raw)
            finish = result["choices"][0].get("finish_reason", "")

        if thinking:
            yield {"type": "think", "text": thinking}
        if not _is_empty_or_garbage(content):
            history.append({"role": "assistant", "content": content})
            yield {"type": "response", "content": content}
            return
        del history[_user_turn_start:]
        log.warning("Forced final answer was empty: finish_reason=%r message_keys=%s",
                    finish, sorted(msg))
        yield {"type": "error",
               "message": f"Could not produce an answer (finish_reason={finish or 'unknown'})."}
    except cancel.Cancelled:
        del history[_user_turn_start:]
        yield {"type": "error", "message": "Stopped"}
    except Exception as e:
        del history[_user_turn_start:]
        log.exception("Forced final answer failed")
        yield {"type": "error", "message": f"Could not produce an answer: {e}"}
