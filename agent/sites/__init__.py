"""Site profiles: everything the agent knows about one particular website.

The agent loop (agent_core) is a generic vision-driven browser agent. When it
works on a task platform -- a site that lists units of work, launches them and
takes a completion back -- that platform's own knowledge (its URLs, the words
and controls on its pages, its rules, its account history) lives in a profile
in this package, never in the loop. The "generic" profile knows nothing and
switches every platform-specific branch off.

    import sites
    sites.use("prolific")        # what FARA_SITE=prolific does at startup
    sites.current.home_url()

`sites.current` is the active profile. Read it at call time (never copy it
into a module-level name), so switching profiles takes effect everywhere.

Which profile is active
-----------------------
FARA_SITE names it explicitly ("generic", "prolific", ...). Unset or "auto",
each run picks it with resolve(): the run's own setting, then the profile
that owns the start URL, then the one that owns the page the browser is on,
then the profile the previous run used (remembered in var/last_site), and
finally generic.

Adding a platform: subclass base.Site in a new module (sites/<name>/), fill in
what the platform needs, and register() it below.
"""
import os
from pathlib import Path
from urllib.parse import urlparse

from .base import Site

_REGISTRY: dict = {}


def register(cls):
    """Make a Site subclass selectable by its `name`."""
    _REGISTRY[cls.name] = cls
    return cls


register(Site)

from .prolific import ProlificSite  # noqa: E402  (registers after base exists)

register(ProlificSite)


def names() -> list:
    return sorted(_REGISTRY)


def get(name: str) -> Site:
    """A fresh instance of the profile called `name`."""
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(f"unknown site profile {name!r} (known: {', '.join(names())})")


# --- choosing one -------------------------------------------------------------

_LAST_SITE = Path(os.environ.get(
    "FARA_LAST_SITE_FILE", Path(__file__).resolve().parents[2] / "var" / "last_site"))


def owner_of(url: str) -> str:
    """Name of the registered profile whose hosts include `url`'s host, or ""."""
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return ""
    for name, cls in _REGISTRY.items():
        if name == "generic":
            continue
        for h in get(name).hosts:
            if host == h or host.endswith("." + h):
                return name
    return ""


def last_used() -> str:
    try:
        name = _LAST_SITE.read_text(encoding="utf-8").strip()
        return name if name in _REGISTRY else ""
    except OSError:
        return ""


def _remember(name: str) -> None:
    try:
        _LAST_SITE.parent.mkdir(parents=True, exist_ok=True)
        _LAST_SITE.write_text(name, encoding="utf-8")
    except OSError:
        pass


def configured() -> str:
    """The profile named by FARA_SITE, or "" when it says auto (or nothing)."""
    name = (os.environ.get("FARA_SITE") or "").strip().lower()
    return "" if name in ("", "auto") else name


def resolve(explicit: str = "", start_url: str = "", current_url: str = "") -> str:
    """The profile a run should use (see the module docstring for the order)."""
    explicit = (explicit or "").strip().lower()
    if explicit == "auto":
        explicit = ""
    for name in (explicit, configured(), owner_of(start_url), owner_of(current_url), last_used()):
        if name:
            if name not in _REGISTRY:
                raise ValueError(f"unknown site profile {name!r} (known: {', '.join(names())})")
            return name
    return "generic"


def use(name: str, remember: bool = False) -> Site:
    """Make `name` the active profile and return it. `remember` records it as
    the last one used, which is what an "auto" run falls back to (pointless,
    and skipped, when FARA_SITE names the profile anyway)."""
    global current
    current = get(name)
    if remember and not configured():
        _remember(name)
    return current


current: Site = get(configured() or last_used() or "generic")
