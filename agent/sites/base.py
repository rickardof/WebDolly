"""The generic site profile: what the agent knows about a website it has no
profile for, which is nothing.

A profile describes a *task platform*: a site that lists units of work (the
"home" list), opens one (a "task" page, usually launching it on another host),
and takes a completion back (a return URL carrying a code, or a code typed on
the platform). The agent loop asks the active profile every question that
depends on the site -- is this URL part of the platform, is this the task list,
which task is this page about, what does an empty list look like, what does the
launch dialog look like -- and the generic answers below make every one of
those branches inert. With the generic profile the agent is a plain
vision-driven browser agent: it sees, clicks, types, fills forms and follows
the task it was given, with no platform rules on top.

A new platform subclasses Site in its own module under sites/ and fills in the
attributes and methods it needs; everything left alone keeps the generic,
switched-off behaviour.
"""
import re
from urllib.parse import urlparse

# A pattern that never matches, so callers can always write
# `profile.home_path.match(path)` without checking for None first.
NEVER = re.compile(r"(?!)")


class Site:
    #: Registry key, also the value of FARA_SITE / the run's `site` setting.
    name = "generic"
    #: Human-readable name, used in prompts and notices ("back on {title}").
    title = "the website"

    # --- where the platform lives ------------------------------------------
    #: Registrable domains the platform owns. A URL on one of them (or on a
    #: subdomain) is "on the platform"; everything else is a third-party site.
    hosts: tuple = ()
    #: The host the work happens on, when the platform also runs other hosts
    #: on the same domain (a marketing site, a help centre). "" = none.
    app_host: str = ""
    #: App paths a typed URL may lead to besides home and task pages.
    app_visitable_prefixes: tuple = ()
    #: Sections the agent must never be in.
    forbidden_path = NEVER

    # --- the platform's own pages ------------------------------------------
    #: The list of available tasks, the page each loop cycle returns to.
    home_path = NEVER
    #: The path used when building the list URL on a given host.
    home_path_str = "/"
    #: A task's own page. group(1) must be the task id.
    task_path = NEVER
    #: The endpoint a finished task returns to, carrying the completion code.
    complete_path = NEVER
    complete_code_param = "cc"
    #: Platform chrome where no task is running (account, history, settings).
    launch_chrome_prefixes: tuple = ()
    #: The platform's own error pages.
    error_path = NEVER
    #: A task the platform hosts itself (as opposed to one on another host).
    hosted_task_path = NEVER

    #: Lower-cased phrases that mean "the task list is empty right now".
    empty_list_markers: tuple = ()
    empty_list_re = NEVER

    def home_url(self) -> str:
        """Where to go back to between tasks, or "" when there is no list."""
        return ""

    def home_on(self, url: str) -> str:
        """The task list on the same host as `url`."""
        pr = urlparse(url or "")
        return f"{pr.scheme}://{pr.netloc}{self.home_path_str}"

    def app_home_url(self) -> str:
        """The task list on the app host."""
        return f"https://{self.app_host}{self.home_path_str}" if self.app_host else self.home_url()

    def task_url(self, task_id: str) -> str:
        """A link to the task's own page, or "" if `task_id` is not one."""
        return ""

    def __repr__(self):
        return f"<site {self.name}>"
