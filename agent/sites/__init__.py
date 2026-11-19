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
"""
import os

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


current: Site = get(os.environ.get("FARA_SITE") or "prolific")


def use(name: str) -> Site:
    """Make `name` the active profile and return it."""
    global current
    current = get(name)
    return current
