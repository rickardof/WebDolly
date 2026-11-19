"""Studies the agent must not open again.

The platform drops a *completed* study from the list by itself, but one the
agent was screened out of -- or that turned out to be full, closed, or (with
`skip_av`) to need audio/video -- stays listed and gets clicked again on the
next cycle. At several steps per dead end, a 24/7 loop spends a good part of
its day rediscovering the same ones.

So they go in a small JSON file, keyed by the 24-hex study id from the URL
when there is one and by title otherwise, and `prompt_line()` hands
`run_agent` a line naming them so the model skips them on the list page.

Entries expire after FARA_STUDY_CACHE_DAYS days: a study that is full today
is not necessarily full next week, and the file must not grow forever.
"""
import json
import os
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
CACHE_FILE = Path(os.environ.get("FARA_STUDY_CACHE", HERE / "study_cache.json"))
TTL_SECONDS = float(os.environ.get("FARA_STUDY_CACHE_DAYS", "14")) * 86400
MAX_ENTRIES = 500

_lock = threading.Lock()


def _read() -> dict:
    try:
        data = json.loads(CACHE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # A truncated or hand-edited file must never stop a run.
        return {}


def _write(data: dict) -> None:
    """Replace the file atomically: the GUI thread may read it at any time."""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def _fresh(data: dict) -> dict:
    cutoff = time.time() - TTL_SECONDS
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and v.get("ts", 0) >= cutoff}


def entries() -> dict:
    """Every non-expired entry, newest first."""
    with _lock:
        fresh = _fresh(_read())
    return dict(sorted(fresh.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True))


def is_marked(key: str) -> bool:
    if not key:
        return False
    return key in entries()


def mark(key: str, reason: str, title: str = "", url: str = "") -> bool:
    """Record a study to skip. Returns True if it was not already there.

    The entry is refreshed (new timestamp, and a title if we have one now)
    even when it was already known, so a study that keeps coming up stays in
    the file instead of ageing out while it is still being offered.
    """
    if not key:
        return False
    with _lock:
        data = _fresh(_read())
        existed = key in data
        prev = data.get(key, {})
        data[key] = {
            "reason": reason or prev.get("reason", "scartato"),
            "title": title or prev.get("title", ""),
            "url": url or prev.get("url", ""),
            "ts": time.time(),
            "hits": int(prev.get("hits", 0)) + 1,
        }
        if len(data) > MAX_ENTRIES:
            oldest = sorted(data.items(), key=lambda kv: kv[1].get("ts", 0))
            for k, _ in oldest[:len(data) - MAX_ENTRIES]:
                data.pop(k, None)
        _write(data)
    return not existed


def forget(key: str) -> bool:
    with _lock:
        data = _fresh(_read())
        if key not in data:
            return False
        data.pop(key)
        _write(data)
    return True


def clear() -> int:
    with _lock:
        n = len(_read())
        _write({})
    return n


def titles() -> list:
    """Titles of the skipped studies — the only key the model can match,
    since the list page shows names, not ids."""
    out = []
    for v in entries().values():
        t = (v.get("title") or "").strip()
        if t and t not in out:
            out.append(t)
    return out


# A screen-out belongs to one reservation, not to the study. The same study
# comes back reposted under a new id -- this file has held two ids for
# "Quality of 4K Videos" at once -- and the account has been paid for the
# repost ("Text-to-Image Evaluation: Image Quality": Approved 6 Nov, blocked
# by title since 5 Nov). The model can only match the title, so a screen-out
# must not become a standing refusal; the exact id is still bounced by the
# caller. Reasons that are a property of the study itself (full, closed,
# already done, audio/video) keep their line.
_NO_TITLE_HINT = ("screened out",)


def prompt_line() -> str:
    """One line for the agent's observation on the studies list page, or ""."""
    items = []
    for v in entries().values():
        t = (v.get("title") or "").strip()
        if t and v.get("reason", "") not in _NO_TITLE_HINT:
            items.append(f'"{t}" [{v.get("reason", "scartato")}]')
        if len(items) >= 15:
            break
    if not items:
        return ""
    return ("Studies already ruled out on an earlier cycle — do NOT open these "
            "again: " + "; ".join(items) + ". If the list contains nothing but "
            "those, treat it as having no eligible study.\n")
