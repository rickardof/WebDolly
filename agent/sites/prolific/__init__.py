"""Prolific (app.prolific.com): the research-participation platform this agent
was first built for.

Everything here is knowledge about Prolific itself -- its URLs, the wording and
controls of its pages, the rules a participant follows on it -- kept out of the
agent loop so the loop stays a generic browser agent. The comments carry the
incidents each rule came from (run_YYYYMMDD_HHMMSS names a transcript under
var/logs/pw/).
"""
import os
import re

from ..base import Site


class ProlificSite(Site):
    name = "prolific"
    title = "Prolific"

    def __init__(self):
        # Only on the site that has such a section: a third-party survey that
        # happens to have an /about-you page must not be hijacked.
        self.hosts = tuple(
            h.strip().lower()
            for h in os.environ.get("FARA_GUARDED_HOSTS", "prolific.com,prolific.co").split(",")
            if h.strip())
        # The agent's workspace is the APP host. Other hosts on the same
        # domain -- www.prolific.com and its marketing pages -- are a different
        # site with no studies on it, yet every guard treats them as "on
        # Prolific" because they share the domain. Wandering onto one burns a
        # whole cycle: in run_20261106_142347 the model typed www instead of
        # app at step 1, landed on /ai-task-builder, and spent 23 steps there
        # and on /login before finding its way back. Learnt from start_url at
        # run start, so a different deployment does not need a code change.
        self.app_host = (os.environ.get("FARA_APP_HOST") or "app.prolific.com").lower()

    # Sections of the site the agent must never be in. The agent's whole job
    # is to take part in studies and, when none fits, wait for new ones; the
    # profile ("About you") questionnaire is not part of that, whatever the
    # page nudges it towards. Enforced in code -- every step, every visit_url,
    # every cycle restart -- not left to the model's judgement or to prompt
    # text.
    forbidden_path = re.compile(r"^/(about-you|logout|sign-out|signout)(/|$)", re.IGNORECASE)

    # A typed navigation to the app host that goes nowhere real is refused.
    # Clicking is unaffected -- this only rejects addresses the model composes
    # itself. run_20261108_154719 invented /study/<uuid> from a study it had
    # just been looking at, landed on Prolific's 404, and spent the rest of
    # the cycle trying to work out where the list had gone. Prolific's study
    # ids are 24 hex characters under /studies/, never a UUID under /study/,
    # so a made-up address is recognisable before it is loaded.
    app_visitable_prefixes = ("/submissions", "/account", "/settings",
                              "/data-collection-tool")

    home_path = re.compile(r"^/studies/?$", re.IGNORECASE)
    home_path_str = "/studies"
    task_path = re.compile(r"^/studies/([0-9a-fA-F]{24})/?$", re.IGNORECASE)
    complete_path = re.compile(r"^/submissions/complete/?$", re.IGNORECASE)
    complete_code_param = "cc"

    # Prolific "chrome" prefixes: pages where no study TASK is running yet --
    # the account/submissions surfaces (the studies list and the per-study
    # reservation page are handled separately via home_path / task_path).
    # Read off the live app's own navigation (2026-11-08): the in-app links
    # are / /balance-hub /studies /submissions /about-you /messages/unread
    # /desktop-app /referrals /account/general /account/preferences. Five of
    # those ten were missing here, so clicking any of them from the studies
    # list made the launch-surface test answer "a task is running":
    # run_20261108_181732 step 42 clicked "About you" in the top nav and the
    # harness started a study timer on /about-you, told the model it was
    # inside a running survey and pushed a "Studio iniziato" notification.
    # The page-identity guard (agent_core._reached_in_launch_tab) covers the
    # paths this tuple still does not name.
    launch_chrome_prefixes = ("/submissions", "/account", "/settings",
                              "/about-you", "/balance-hub", "/messages",
                              "/desktop-app", "/referrals")

    # Prolific's own error pages. They need naming explicitly because the
    # launch-surface test answers "unknown path => a task is running": that
    # default is deliberate (it is what stops the wander guard cutting short
    # a first-party task at a path we have not catalogued, 1ffb26d), but it
    # also made a 404 look like a running study. run_20261108_154719 step 19
    # invented the URL /study/<uuid> (singular, UUID -- not Prolific's
    # /studies/<24hex>), got redirected to /404, and the harness started a
    # study timer on it, switched the wander guard off and turned batch-fill,
    # reading dwell and the audio/video check loose on an error page.
    error_path = re.compile(
        r"^/(40[0-9]|41[0-9]|50[0-9]|error|errors|not[-_]?found|maintenance)(/|$)",
        re.IGNORECASE)

    # The only Prolific-HOSTED task surface we have ever seen. Named so the
    # page-identity guard keeps an explicit escape hatch for it.
    hosted_task_path = re.compile(r"^/data-collection-tool(/|$)", re.IGNORECASE)

    empty_list_markers = ("waiting for studies",)
    # "showing: 0/0 studies" (or "0 / 0") -- Prolific's counter when the list
    # is empty.
    empty_list_re = re.compile(r"showing\s*:?\s*0\s*/\s*0\s*stud", re.IGNORECASE)

    def home_url(self) -> str:
        return os.environ.get("FARA_STUDIES_URL", "https://app.prolific.com/studies")

    def task_url(self, task_id: str) -> str:
        if not task_id or not self.task_path.match(f"/studies/{task_id}"):
            return ""
        return f"https://app.prolific.com/studies/{task_id}"
