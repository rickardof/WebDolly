"""Behaviours: rule sets that plug into the agent loop.

`agent_core.run_agent` is a plain browser agent: screenshot, ask the model,
act, repeat, with human-like input, batch form filling, stuck detection and
page-change hints. Anything that only makes sense on a certain kind of site
-- a platform that hands out tasks with a time limit, a completion code and
a launch dialog, say -- is a Behavior. A site profile lists the behaviours it
needs (`Site.behaviors()`); the generic profile needs none.

The loop calls every hook of every active behaviour at fixed points of a
step, in this order:

    start             once, before the start URL is opened
    arrive            the page the step starts on is known (generator)
      -- batch form fill, if enabled and allowed --
    settle            per-step bookkeeping on the page (generator)
      -- page hints, reading pause, screenshot --
    after_screenshot  with the page's text fingerprint
    first_prompt      text appended to the task on step 1
    prompt_hints      lines added to every later step's prompt...
    prompt_suffix     ...and after the generic hints
      -- model call --
    before_action     may hold the action back (returns the step's result)
      -- the action --
    after_action
    diag              fields for the step's diagnostic line
    terminate         may refuse the model's terminate (returns the reply)
    finish_record     fields for the final diagnostic line
    finished          the run is over

`arrive` and `settle` are generators: what they yield is passed to the
loop's caller as an event, and what the caller sends back comes back from
the yield. `arrive` returns True when the run is over (it has yielded the
`done` event itself).

Generic parts of the loop that a behaviour may need to switch off or on ask
`run.allow(feature)` (no behaviour vetoes it) or `run.want(feature)` (some
behaviour asks for it). Features:

    batch_fill      batch form filling on this page          (allow)
    page_hints      DOM navigation hints and user hints       (allow)
    reading_dwell   the human reading pause before acting     (allow)
    remember_page   keeping the page as context for later     (allow)
    prescroll       scroll the first open question into view  (want)
    auto_enter      press Enter after a click on known sites  (want)
"""


class Run:
    """What one call of run_agent shares with its behaviours."""

    def __init__(self, task, run_dir, transcript, messages, behaviors, options):
        self.task = task
        self.run_dir = run_dir
        self.transcript = transcript
        self.messages = messages
        self.behaviors = list(behaviors)
        #: The run's settings (wander_seconds, skip_av, batch_fill).
        self.options = dict(options)
        self.step = 0
        #: URL of the page this step is on (a behaviour may move it).
        self.here = ""
        #: URL the previous step ended on ("" before the first one).
        self.prev_here = ""
        #: Text the model gets with its next screenshot (a correction, a
        #: refusal, a notice); "" when there is none.
        self.pending = ""
        #: True while `pending` holds a user's mid-run instruction.
        self.user_inject_pending = False
        #: Text-heavy pages of the current task (scenario, instructions), for
        #: batch fill to answer later questions that refer back to them.
        self.study_pages = []
        #: An object with is_active() / remaining() that batch fill consults
        #: before it presses a final submit; None when nothing times the task.
        self.timer = None
        #: The action being carried out (set before before_action).
        self.action_type = ""
        self.args = {}
        #: Appended to the action's result line (set by before_action).
        self.action_note = ""

    def allow(self, feature: str) -> bool:
        return all(b.allows(self, feature) for b in self.behaviors)

    def want(self, feature: str) -> bool:
        return any(b.wants(self, feature) for b in self.behaviors)

    def write(self, record: dict) -> None:
        """One line in the run's transcript."""
        import json
        self.transcript.write(json.dumps(record) + "\n")
        self.transcript.flush()


class Behavior:
    """No-op hooks; subclasses override what they need. See the module
    docstring for when each one runs."""

    name = "behavior"

    def allows(self, run: Run, feature: str) -> bool:
        return True

    def wants(self, run: Run, feature: str) -> bool:
        return False

    def start(self, run: Run, start_url: str) -> None:
        pass

    def arrive(self, run: Run):
        if False:
            yield
        return False

    def settle(self, run: Run):
        if False:
            yield

    def after_screenshot(self, run: Run, page_fp: str) -> None:
        pass

    def first_prompt(self, run: Run) -> str:
        return ""

    def prompt_hints(self, run: Run) -> str:
        return ""

    def prompt_suffix(self, run: Run) -> str:
        return ""

    def before_action(self, run: Run):
        """Return a result line to hold the action back, or None."""
        return None

    def after_action(self, run: Run) -> None:
        pass

    def diag(self, run: Run) -> dict:
        return {}

    def terminate(self, run: Run, answer: str):
        """Return the reply that refuses this terminate, or None."""
        return None

    def finish_record(self, run: Run, answer: str) -> dict:
        return {}

    def finished(self, run: Run, answer: str) -> None:
        pass


def for_site(site) -> list:
    """Fresh instances of the behaviours `site` asks for."""
    return [cls() for cls in site.behaviors()]
