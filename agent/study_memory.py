"""Cross-cycle study memory: remembers what worked on previous surveys.

When the model uses `run_js` to batch-complete repetitive trials on a survey
domain (e.g. boltai.com, qualtrics.com), this module remembers it.  On future
cycles, when the model enters a survey on a known domain, a hint is injected
so it can apply the efficient strategy immediately instead of discovering it
the hard way (50+ slow steps before someone notices).

Data lives in a small JSON file alongside study_cache.json, keyed by domain.
"""
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
MEMORY_FILE = Path(os.environ.get("FARA_STUDY_MEMORY",
                                   HERE / "study_memory.json"))
TTL_SECONDS = float(os.environ.get("FARA_STUDY_MEMORY_DAYS", "60")) * 86400
MAX_ENTRIES = 200

_lock = threading.Lock()


def _read() -> dict:
    try:
        data = json.loads(MEMORY_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write(data: dict) -> None:
    try:
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = MEMORY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, MEMORY_FILE)
    except Exception:
        pass


def _fresh(data: dict) -> dict:
    cutoff = time.time() - TTL_SECONDS
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and v.get("ts", 0) >= cutoff}


def domain_of(url: str) -> str:
    """Extract the survey domain from a URL, stripping common prefixes."""
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def record(url: str, *, run_js_used: bool = False, steps: int = 0,
           outcome: str = "") -> None:
    """Record what happened on a survey domain."""
    domain = domain_of(url)
    if not domain:
        return
    with _lock:
        data = _fresh(_read())
        prev = data.get(domain, {})
        entry = {
            "ts": time.time(),
            "seen": int(prev.get("seen", 0)) + 1,
            "outcome": outcome or prev.get("outcome", ""),
        }
        if run_js_used:
            entry["run_js_used"] = True
            entry["run_js_count"] = int(prev.get("run_js_count", 0)) + 1
        elif prev.get("run_js_used"):
            entry["run_js_used"] = True
            entry["run_js_count"] = int(prev.get("run_js_count", 0))
        if steps:
            entry["last_steps"] = steps
        if len(data) > MAX_ENTRIES:
            oldest = sorted(data.items(), key=lambda kv: kv[1].get("ts", 0))
            for k, _ in oldest[:len(data) - MAX_ENTRIES]:
                data.pop(k, None)
        data[domain] = entry
        _write(data)


def hint_for(url: str) -> str:
    """Return a hint for the model based on past experience with this domain."""
    domain = domain_of(url)
    if not domain:
        return ""
    with _lock:
        data = _fresh(_read())
    entry = data.get(domain)
    if not entry:
        return ""
    if entry.get("run_js_used"):
        n = entry.get("run_js_count", 1)
        return (f'[Surveys on {domain} previously had many identical repeated trials '
                f'({n}x confirmed) — use run_js to batch-complete them when you see '
                f'the same select-and-submit pattern repeating.]\n')
    return ""


def entries() -> dict:
    with _lock:
        return _fresh(_read())
