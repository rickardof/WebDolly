"""Cooperative + forced cancellation for the agent's slow blocking calls.

Before this, "Stop" only set a flag that the agent looked at between steps,
so stopping took as long as whatever it was blocked on: measured 7.5s during
batch-fill decisions, 4.3s during a vision step, 26.8s during a `wait`
action. Now Stop (1) sets a global flag, (2) closes the socket of every
in-flight model request -- the blocked recv() fails at once, and
llama-server drops the generation when the client disconnects, so the GPU
is freed too -- and (3) interruptible sleeps wake immediately.

Cancelled derives from BaseException on purpose: the agent code has many
`except Exception` guards (batch-fill fallbacks, action-failure handling)
that would otherwise swallow a stop and carry on.
"""

import http.client
import json
import socket
import threading
from urllib.parse import urlparse


class Cancelled(BaseException):
    """The user pressed Stop."""


_cancel = threading.Event()
_lock = threading.Lock()
_conns = set()


def reset():
    _cancel.clear()


def is_cancelled() -> bool:
    return _cancel.is_set()


def check():
    if _cancel.is_set():
        raise Cancelled()


def sleep(seconds: float):
    """time.sleep that wakes up (and raises Cancelled) the moment Stop is pressed."""
    if _cancel.wait(seconds):
        raise Cancelled()


def cancel_all():
    _cancel.set()
    with _lock:
        conns = list(_conns)
    for c in conns:
        try:
            if c.sock is not None:
                c.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            c.close()
        except Exception:
            pass


def post_chat(url: str, payload: dict, timeout: float = 120, on_delta=None,
              extra_headers: dict | None = None) -> dict:
    """Chat-completions request that can be aborted ON THE SERVER too.

    A plain (non-streaming) request keeps generating after the client
    hangs up: measured, cutting the connection mid-generation left the
    model slot busy for another 9.6s (the rest of the answer), eating the
    GPU and slowing whatever came next. With stream=true llama-server
    notices the dead socket on the very next token it tries to send and
    cancels the task, so Stop frees the model within ~one token. The
    streamed reply is reassembled into the same shape as a normal one
    ({"choices":[{"message":{"content":..}}], "usage":..}).

    `on_delta(text)`, if given, is called with every piece of generated
    text as it arrives (the GUI shows the model's reasoning live). A
    failing callback never breaks the request."""
    check()
    u = urlparse(url)
    body = dict(payload, stream=True, stream_options={"include_usage": True})
    if u.scheme == "https":
        import ssl
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443,
                                           context=ssl.create_default_context(),
                                           timeout=timeout)
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
    with _lock:
        _conns.add(conn)
    req_headers = {"Content-Type": "application/json"}
    if extra_headers:
        req_headers.update(extra_headers)
    try:
        conn.request("POST", u.path or "/", json.dumps(body).encode(), req_headers)
        resp = conn.getresponse()
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} from {u.path}: "
                               f"{resp.read()[:300].decode(errors='replace')}")
        parts, usage, finish, timings = [], {}, None, {}
        tool_calls_accum = {}  # id -> {id, type, function: {name, arguments}}
        for raw in resp:
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("usage"):
                usage = chunk["usage"]
            if chunk.get("timings"):          # llama-server only
                timings = chunk["timings"]
            for ch in chunk.get("choices", []):
                delta = ch.get("delta", {})
                if delta.get("content"):
                    parts.append(delta["content"])
                    if on_delta is not None:
                        try:
                            on_delta(delta["content"])
                        except Exception:
                            pass
                for tc_delta in delta.get("tool_calls", []):
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_calls_accum:
                        tool_calls_accum[idx] = {
                            "id": tc_delta.get("id", ""),
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    tc = tool_calls_accum[idx]
                    fn = tc_delta.get("function", {})
                    if fn.get("name"):
                        tc["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        tc["function"]["arguments"] += fn["arguments"]
                    if tc_delta.get("id"):
                        tc["id"] = tc_delta["id"]
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
        msg = {"role": "assistant", "content": "".join(parts)}
        if tool_calls_accum:
            msg["tool_calls"] = [tool_calls_accum[k]
                                 for k in sorted(tool_calls_accum)]
        return {"choices": [{"message": msg,
                             "finish_reason": finish}], "usage": usage, "timings": timings}
    except BaseException:
        if _cancel.is_set():
            raise Cancelled() from None
        raise
    finally:
        with _lock:
            _conns.discard(conn)
        try:
            conn.close()
        except Exception:
            pass


def post_json(url: str, payload: dict, timeout: float = 120) -> dict:
    """POST JSON and return the parsed reply; abortable by cancel_all()."""
    check()
    u = urlparse(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
    with _lock:
        _conns.add(conn)
    try:
        conn.request("POST", u.path or "/", json.dumps(payload).encode(),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} from {u.path}: {data[:300].decode(errors='replace')}")
        return json.loads(data)
    except BaseException:
        if _cancel.is_set():
            raise Cancelled() from None
        raise
    finally:
        with _lock:
            _conns.discard(conn)
        try:
            conn.close()
        except Exception:
            pass
