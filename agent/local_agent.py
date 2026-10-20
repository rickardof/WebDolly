"""Local Agent: text-based chat with internet access via tool-use.

Uses whichever model is loaded on llama-server (typically Qwen3.8-27B).
No vision — uses Playwright to navigate and reads page text.

Tools:
  - web_search(query): search DuckDuckGo and return results
  - browse_url(url):   navigate the browser and return page text
"""

import html as html_mod
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

import cancel
import pw_bridge

log = logging.getLogger(__name__)

MODEL_URL = os.environ.get("FARA_URL", "http://127.0.0.1:8090/v1/chat/completions")

SYSTEM_PROMPT = """\
You are a helpful assistant with access to the internet through tools.

Available tools:

1. **web_search(query)** — Search the web and get a list of results with titles, URLs and snippets.
2. **browse_url(url)** — Navigate the browser to a URL and read the page content as text.

To use a tool, write exactly:
<tool_call>
{"name": "tool_name", "arguments": {"param": "value"}}
</tool_call>

Rules:
- After a tool call, STOP and wait for the result. Do NOT continue writing after </tool_call>.
- You can make multiple tool calls across turns.
- Always cite your sources when answering from web results.
- When you have enough information, answer the user directly without further tool calls.
- Keep answers concise and well-structured.
"""

MAX_PAGE_CHARS = 12000
MAX_SEARCH_RESULTS = 8
MAX_TOOL_ROUNDS = 10


# ── Tools ────────────────────────────────────────────────────────────

def web_search(query: str) -> str:
    """Search Google via the browser and return results as text."""
    try:
        q = urllib.parse.quote_plus(query)
        search_url = f"https://www.google.com/search?q={q}&hl=en"

        def _search(b):
            page = b.active_page()
            page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)
            # Handle Google consent page
            try:
                consent = page.query_selector('button:has-text("Accept all")')
                if not consent:
                    consent = page.query_selector('button:has-text("Accetta tutto")')
                if consent:
                    consent.click()
                    page.wait_for_timeout(2000)
            except Exception:
                pass
            # Extract search results: find every h3 that is inside an <a>
            return page.evaluate(r"""() => {
                const results = [];
                for (const h3 of document.querySelectorAll('h3')) {
                    const a = h3.closest('a') || h3.querySelector('a') ||
                              h3.parentElement?.querySelector('a[href^="http"]');
                    if (!a) continue;
                    const href = a.href;
                    if (!href || href.includes('google.com/search') ||
                        href.includes('accounts.google')) continue;
                    const title = h3.textContent.trim();
                    if (!title || results.some(r => r.href === href)) continue;
                    // Walk up to find a snippet nearby
                    let snippet = '';
                    const block = h3.closest('[data-hveid]') || h3.closest('div')?.parentElement;
                    if (block) {
                        const spans = block.querySelectorAll('span, div');
                        for (const s of spans) {
                            const t = s.textContent.trim();
                            if (t.length > 50 && t !== title && !t.includes(title)) {
                                snippet = t.slice(0, 200); break;
                            }
                        }
                    }
                    results.push({title, href, snippet});
                    if (results.length >= 8) break;
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


def browse_url(url: str) -> str:
    """Navigate the browser to a URL and return the page text."""
    try:
        def _nav(b):
            page = b.active_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1500)
            text = page.inner_text("body")
            return text
        text = pw_bridge.run(_nav, timeout=30)
    except Exception as e:
        return f"Failed to load {url}: {e}"

    if not text or not text.strip():
        return f"Page loaded ({url}) but no readable text content found."

    text = re.sub(r'\n{3,}', '\n\n', text.strip())
    if len(text) > MAX_PAGE_CHARS:
        text = text[:MAX_PAGE_CHARS] + f"\n\n[... truncated, {len(text)} chars total]"
    return f"Page content ({url}):\n\n{text}"


TOOL_DISPATCH = {
    "web_search": lambda args: web_search(args.get("query", "")),
    "browse_url": lambda args: browse_url(args.get("url", "")),
}


# ── Agent loop ───────────────────────────────────────────────────────

def _parse_tool_call(text: str):
    """Extract a <tool_call> block from model output. Returns (before, call, after) or None."""
    m = re.search(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', text, re.DOTALL)
    if not m:
        return None
    try:
        call = json.loads(m.group(1))
        before = text[:m.start()].strip()
        after = text[m.end():].strip()
        return before, call, after
    except (json.JSONDecodeError, KeyError):
        return None


def run_chat(user_message: str, history: list, on_event=None):
    """Run a chat turn, handling tool calls iteratively.

    Args:
        user_message: the user's message
        history: list of {"role": ..., "content": ...} messages (mutable, appended to)
        on_event: callback for real-time events

    Yields event dicts:
        {"type": "thinking"}
        {"type": "token", "text": "..."}
        {"type": "tool_call", "name": "...", "args": {...}}
        {"type": "tool_result", "name": "...", "result": "..."}
        {"type": "browsing", "url": "..."}
        {"type": "response", "content": "..."}
        {"type": "error", "message": "..."}

    Returns the assistant's final text response.
    """
    if not history:
        history.append({"role": "system", "content": SYSTEM_PROMPT})

    history.append({"role": "user", "content": user_message})

    for round_n in range(MAX_TOOL_ROUNDS):
        yield {"type": "thinking"}

        try:
            payload = {
                "model": "local",
                "messages": history,
                "temperature": 0.6,
                "max_tokens": 2048,
            }
            result = cancel.post_chat(MODEL_URL, payload, timeout=120,
                                      on_delta=lambda t: (on_event or (lambda e: None))(
                                          {"type": "token", "text": t}))
            content = result["choices"][0]["message"]["content"] or ""
        except cancel.Cancelled:
            yield {"type": "error", "message": "Stopped"}
            return
        except Exception as e:
            yield {"type": "error", "message": str(e)}
            return

        # Check for tool call
        parsed = _parse_tool_call(content)
        if parsed is None:
            # No tool call — this is the final response
            history.append({"role": "assistant", "content": content})
            yield {"type": "response", "content": content}
            return

        before, call, after = parsed
        tool_name = call.get("name", "")
        tool_args = call.get("arguments", {})

        # Add assistant message with the tool call
        history.append({"role": "assistant", "content": content})

        yield {"type": "tool_call", "name": tool_name, "args": tool_args}

        if tool_name == "browse_url":
            yield {"type": "browsing", "url": tool_args.get("url", "")}

        # Execute tool
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

        # Add tool result as a user message (the model sees it as context)
        history.append({"role": "user",
                        "content": f"[Tool result for {tool_name}]:\n{tool_result}"})

    yield {"type": "error", "message": "Too many tool calls, stopping."}
