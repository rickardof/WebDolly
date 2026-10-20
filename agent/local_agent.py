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
ENABLE_THINKING = os.environ.get("FARA_LOCAL_ENABLE_THINKING", "1") == "1"
REASONING_EFFORT = os.environ.get("FARA_LOCAL_REASONING_EFFORT", "medium")


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


def _strip_think(text: str) -> str:
    """Remove <think> tags from model output, keeping content if nothing else remains."""
    stripped = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    stripped = re.sub(r'<think>.*', '', stripped, flags=re.DOTALL)
    stripped = stripped.strip()
    if stripped:
        return stripped
    return text.replace('<think>', '').replace('</think>', '').strip()


def _is_empty_or_garbage(text: str) -> bool:
    """Detect an unfinished reasoning stream instead of showing it as an answer."""
    return not text or not re.search(r"[A-Za-z0-9À-ÿ]", text)


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
    seen_calls = set()

    for round_n in range(MAX_TOOL_ROUNDS):
        yield {"type": "thinking"}

        try:
            payload = {
                "model": "local",
                "messages": history,
                "tools": TOOLS_SCHEMA,
                "temperature": 0.6,
                "max_tokens": 2048,
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
            content = _strip_think(msg.get("content") or "")
            tool_calls = msg.get("tool_calls") or []
            finish = result["choices"][0].get("finish_reason", "")

            # Keep thinking enabled normally, but do not leave the user with
            # an empty answer when the model spends its entire budget thinking.
            if (_is_empty_or_garbage(content) and not tool_calls and finish == "length"
                    and ENABLE_THINKING
                    and "chat_template_kwargs" in payload):
                retry_payload = dict(payload)
                retry_payload["max_tokens"] = 1024
                retry_payload["chat_template_kwargs"] = {
                    "enable_thinking": False,
                    "reasoning_effort": "low",
                }
                result = cancel.post_chat(MODEL_URL, retry_payload, timeout=120)
                msg = result["choices"][0]["message"]
                content = _strip_think(msg.get("content") or "")
                tool_calls = msg.get("tool_calls") or []
                finish = result["choices"][0].get("finish_reason", "")
        except cancel.Cancelled:
            yield {"type": "error", "message": "Stopped"}
            return
        except Exception as e:
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
                            "content": tool_result})

    # Force answer after exhausting rounds (no tools → model can only produce text)
    history.append({"role": "user",
                    "content": "Answer the question now with the information you have. "
                               "Do not search again."})
    try:
        payload = {
            "model": "local",
            "messages": history,
            "temperature": 0.4,
            "max_tokens": 1024,
        }
        result = cancel.post_chat(MODEL_URL, payload, timeout=60)
        content = _strip_think(result["choices"][0]["message"].get("content") or "")
        content = _clean_response(content)
        if content:
            history.append({"role": "assistant", "content": content})
            yield {"type": "response", "content": content}
            return
    except Exception:
        pass
    yield {"type": "error", "message": "Could not produce an answer."}
