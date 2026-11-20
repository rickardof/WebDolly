"""The panel refuses requests that do not come from its own page.

Needs no browser and no model: the handler is started on a free port with its
real routes replaced by recorders, so what is checked is only whether a request
gets as far as running a command.

    PYTHONUTF8=1 venv_pw/Scripts/python.exe tests/panel_guard_test.py
"""
import http.client
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

os.environ["FARA_CDP_URL"] = "http://127.0.0.1:9"      # no real browser
_TMP = tempfile.mkdtemp()
os.environ.setdefault("FARA_LOG_ROOT", os.path.join(_TMP, "logs"))
os.environ["FARA_PRESETS"] = os.path.join(_TMP, "presets.json")
os.environ["FARA_STUDY_MEMORY"] = os.path.join(_TMP, "study_memory.json")
os.environ["FARA_STUDY_CACHE"] = os.path.join(_TMP, "study_cache.json")
os.environ["FARA_LAST_SITE_FILE"] = os.path.join(_TMP, "last_site")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "agent"))

import gui_server  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail and not ok else ""))


ran = []


def _stub(kind):
    def run(self):
        ran.append((kind, self.path))
        self._send_json({"ok": True})
    return run


gui_server.Handler._do_GET = _stub("GET")
gui_server.Handler._do_POST = _stub("POST")
server = ThreadingHTTPServer(("127.0.0.1", 0), gui_server.Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()


def req(method, path, headers=None, body=None):
    """Status code, with the Host header exactly as given (None = the default)."""
    ran.clear()
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    h = dict(headers or {})
    c.putrequest(method, path, skip_host="Host" in h)
    for k, v in h.items():
        c.putheader(k, v)
    if body is not None:
        c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    code = c.getresponse().status
    c.close()
    return code, bool(ran)


own = f"http://127.0.0.1:{PORT}"
code, executed = req("GET", "/agent/status")
check("a plain request (a script, curl) is served", code == 200 and executed)
code, executed = req("GET", "/agent/status", {"Host": f"localhost:{PORT}"})
check("so is one addressed to localhost", code == 200 and executed)
code, executed = req("POST", "/agent/stop", {"Origin": own, "Content-Type": "application/json"}, b"{}")
check("the panel's own page can send commands", code == 200 and executed)

code, executed = req("GET", "/agent/status", {"Host": f"host.docker.internal:{PORT}"})
check("a request that reaches it as host.docker.internal is refused (the agent's browser)",
      code == 403 and not executed)
code, executed = req("GET", "/agent/status", {"Host": f"rebind.example:{PORT}"})
check("so is a DNS-rebinding name", code == 403 and not executed)
code, executed = req("POST", "/start_agent", {"Origin": "https://evil.example", "Content-Type": "text/plain"}, b"{}")
check("a command from another website is refused before it runs", code == 403 and not executed)
code, executed = req("POST", "/agent/stop", {"Origin": "null", "Content-Type": "text/plain"}, b"{}")
check("so is one from a sandboxed page (Origin: null)", code == 403 and not executed)
code, executed = req("GET", "/screen.png", {"Origin": "https://evil.example"})
check("reads from another website are refused too", code == 403 and not executed)
code, executed = req("POST", "/agent/stop", {"Origin": f"http://127.0.0.1:{PORT + 1}"})
check("a different local port is another origin", code == 403 and not executed)

print(f"\n== SUMMARY ==  {sum(RESULTS)}/{len(RESULTS)} passed")
sys.exit(0 if all(RESULTS) else 1)
