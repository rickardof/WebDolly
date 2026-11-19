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

    # --- the platform's pages, as the agent sees them -----------------------
    #: Extra words that introduce a completion code on a task's end page
    #: ("Your <platform> code is ..."), besides completion/confirmation/survey/study.
    code_keywords: tuple = ()
    #: JS returning the task's name on its own page ("" elsewhere), and
    #: lower-cased strings that are never a task's name.
    task_title_js = ""
    task_title_junk: tuple = ()
    #: Banner shown while a task slot is held but not finished.
    held_slot_banner_re = NEVER
    #: (regex, short reason) pairs: a task page saying the task leads nowhere.
    dead_end_markers: tuple = ()
    #: The platform's own countdown for a held slot ("Time remaining 13 Mins").
    deadline_re = NEVER
    #: The page the platform shows once a task was submitted or cancelled.
    submission_confirmation_re = NEVER
    #: JS returning the box of a task's "return to the platform" control, or null.
    completion_return_js = ""
    #: JS returning the box of the platform's "I got no completion code" control.
    nocode_link_js = ""
    #: What the model is told while that control is still on screen.
    nocode_link_refusal = ""
    #: CSS selectors of the platform's task-launch flow: reserve the task, open
    #: the launch dialog, the dialog itself, and its start control.
    launch_reserve_sel = ""
    launch_open_sel = ""
    launch_dialog_sel = ""
    launch_start_sel = ""

    def reservation_nudge(self, fresh_unlaunched: bool, task_id: str) -> str:
        """Guidance for a model stuck on a held task's detail page."""
        return ""

    # --- the participant's identity on the platform ------------------------
    #: A form field label/id that asks for the participant's platform ID.
    participant_id_label_re = NEVER
    #: URL query parameters the platform uses to pass that ID to a task.
    participant_id_params: tuple = ()
    #: What a valid ID looks like (fullmatch), and how to spot one in text.
    participant_id_format = NEVER
    participant_id_in_text = NEVER
    #: identity.json keys older versions used for the ID.
    participant_id_aliases: tuple = ()

    # --- the account's history (read between cycles by the GUI server) ------
    #: First page of the account's submission history ("" = no history).
    history_url = ""
    history_per_page = 20
    #: JS that is true once a history page has rendered its rows.
    history_ready_js = ""
    #: A row's status badge, and the statuses the platform pays for.
    history_status_re = NEVER
    history_paid_statuses: tuple = ()

    def parse_history_rows(self, raw_rows: list) -> list:
        """One dict per submission on a history page (its text lines)."""
        return []

    def parse_history_header(self, rows: list) -> dict:
        """The page's own account totals (balance, earned, counts)."""
        return {}

    def parse_history_last_page(self, rows: list) -> int:
        return 1

    def money(self, line: str):
        """("£", 1.5) from a bare amount line, or None."""
        return None

    # --- what the model is told -------------------------------------------
    def standing_rules(self, compact: bool = False) -> str:
        """Rules appended to every run's task on this site ("" = none)."""
        return ""

    def identity_hint(self, idv: dict) -> str:
        """The account identifiers the model may type when a page asks."""
        if not (idv or {}).get("email"):
            return ""
        return ("\n\nYOUR ACCOUNT DETAILS (known, from file -- use the EXACT value, never alter or "
                f"invent): email is exactly {idv['email']}. When a page asks for your email, "
                "type exactly this value.")

    # Every text the loop shows the model (or the GUI) about the platform.
    # The generic wording below is used by any profile that does not supply
    # its own; texts that only make sense on a platform with a completion
    # contract return "", which the loop reads as "nothing to say".

    def unattended_answer(self) -> str:
        """Sent instead of a human answer when the model asks a question
        during an unattended loop run."""
        return ("You are running UNATTENDED in a 24/7 loop — no human will answer, so stop "
                "asking questions (this is a standing rule). Decide for yourself and keep "
                "going; if you genuinely cannot continue the current task, terminate and "
                "move on. Never stop to ask again.")

    def nocode_refusal(self) -> str:
        """A NOCODE terminate on a task page whose task was never opened."""
        return ""

    def nocode_codes_refusal_text(self, code: str) -> str:
        return (f"NOCODE REFUSED: you are ending this task as 'no completion code', but "
                f"{code} was shown on the task's own pages. Enter {code} where the site "
                f"asks for the completion code and terminate with CODE:{code}.")

    def completion_pushback_text(self, host: str) -> str:
        """A free-text terminate that never got the task back to the platform."""
        return ""

    def refuse_forbidden_visit(self) -> str:
        return "visit_url REFUSED: that page is off limits."

    def refuse_unknown_path(self) -> str:
        return ("visit_url REFUSED: there is no such page on this site. Use the site's own "
                "links instead of typing an address.")

    def corrected_unknown_path(self, home: str, target: str, app_host: str) -> str:
        return (f"visit_url {home} (CORRECTED: {target} is not a real page -- wrong host AND "
                f"no such path. You are back on the site's main page.)")

    def corrected_host(self, fixed: str, target: str, app_host: str) -> str:
        return (f"visit_url {fixed} (CORRECTED: you asked for {target}, which is a different "
                f"site on the same domain. The work happens on {app_host} -- always use that host.)")

    def history_back_refusal(self) -> str:
        return ("history_back REFUSED: going back would leave this site (you would be logged "
                "out). Use the page's own navigation instead.")

    def forbidden_notice(self) -> str:
        return ("You ended up in a section of the site that is off limits, so you were taken "
                "back to the main page. Never open it.")

    def forbidden_live_notice(self) -> str:
        return "Aperta una sezione vietata: riportato alla pagina principale."

    def wander_notice(self, n) -> str:
        return (f"You spent more than {n} seconds away from the site's main page without "
                "working on a task, so you were taken back to it.")

    def wander_live_notice(self, n) -> str:
        return f"Più di {n} s fuori dalla pagina principale senza un compito: riportato all'inizio."

    def off_host_notice(self, back: str, app_host: str) -> str:
        return (f"You had navigated to a different website that only shares this site's "
                f"domain. You have been returned to {back}. The work only ever happens on "
                f"{app_host} -- stay on it.")

    def chrome_guard_live_notice(self, here: str) -> str:
        return (f"[guard] {here} is account chrome reached in the launch tab, not a task: "
                f"no timer started")

    def resume_on_task_page(self) -> str:
        return ("You are resuming on a task's page. A previous run was working on this task. "
                "Continue it.")

    def resume_in_task(self) -> str:
        return ("You are resuming in the middle of a task. A previous run was working "
                "through it. Continue where you left off.")

    def in_task_framing(self) -> str:
        """Said on every step while a platform-hosted task is running."""
        return ""

    def held_slot_notice(self) -> str:
        return ""

    def launched_notice(self) -> str:
        return ("The task has just been opened for you in a new window. You are now IN it: "
                "read the current page and continue.")

    def nocode_link_live_notice(self, label: str, after: str) -> str:
        return f"[completion] the task was not submitted: clicked {label!r} -> {after}"

    def nocode_link_clicked(self, label: str) -> str:
        return (f"HARNESS ACTION: the task was not submitted yet, so the control {label!r} "
                "has been clicked for you. Look at the screenshot and confirm the submission "
                "in the dialog that opened before you terminate.")

    def completion_return_clicked(self, label: str, after: str) -> str:
        return (f"HARNESS ACTION: the control that returns this task ({label!r}) has been "
                f"clicked for you and the page is now {after}. Look at the screenshot and "
                "terminate with the code if one is shown.")

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
