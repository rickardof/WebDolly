# Site profiles

The agent loop (`agent/agent_core.py`) is a generic browser agent: it takes a
screenshot, asks the model what to do, and does it with a human-like mouse and
keyboard. It knows nothing about any particular website, nor about any kind of
website.

Two things sit on top of it:

- a **site profile** (`agent/sites/`) says what one website looks like: its
  hosts, its pages, its buttons, the texts the model is given there;
- a **behaviour** (`agent/behaviors/`) is a set of rules the loop runs with,
  hooked into fixed points of every step. A profile lists the behaviours its
  site needs.

The behaviour that ships is `TaskPlatform`
([agent/behaviors/task_platform.py](../agent/behaviors/task_platform.py)), for a
*task platform*: a site that lists units of work, opens one (often on another
host), times it and takes a completion back. It asks the active profile every
question that depends on the site, for example:

- is this URL part of the platform, and which section of it?
- what does an empty task list look like?
- which control launches a task, and which one returns it?
- what must the model be told on this platform (its rules, its refusals)?
- how is the account's history read?

Two profiles ship with the code:

| Profile | Behaviours | What it is |
|---|---|---|
| `generic` | none | no platform. The loop runs as a plain browser agent and nothing platform-specific reaches the model. Use it for any ordinary website. |
| the platform profile | `TaskPlatform` | the participant side of an online research-study platform: its URLs, launch dialog, completion codes, minimum and maximum time per study, account history, and the rules the model follows there. |

## Which profile is used

`FARA_SITE` names the profile explicitly (`generic`, or the name of another
profile as the panel's menu lists it). When it
is unset or `auto`, each run picks one, in this order:

1. the run's own setting (the panel's *Profilo del sito* menu, saved in presets;
   `--site` on the command line);
2. the profile that owns the run's start URL;
3. the profile that owns the page the browser is on when the run starts;
4. the profile the previous run used (remembered in `var/last_site`);
5. `generic`.

The panel shows which profile a run is using in its activity feed
("Profilo sito: ...").

## What the generic profile changes

With `generic` the loop runs with no behaviour, so it is a plain browser agent:

- no task timer, no minimum time hold on submit buttons, no deadline;
- no task list: a loop run goes back to the page it started from and waits
  the pause between cycles;
- no forbidden sections, host corrections or invented-URL refusals;
- no completion-code handling beyond what the task itself asks for;
- no platform rules appended to the task: the model sees your task and
  context only.

Batch form filling, human-like input, stuck detection and the page-change
hints work the same on every site.

## Customising an included profile

The included platform profile has its own directory under `agent/sites/`.
Everything specific to that platform is in two files there: `__init__.py`
(pages, rules, texts) and `submissions.py` (reading the submission history).

**Without touching code**, set environment variables before starting:

| Variable | What it changes |
|---|---|
| `FARA_GUARDED_HOSTS` | the domains the profile owns, comma-separated |
| `FARA_APP_HOST` | the host the studies live on (also learnt from the start URL) |
| `FARA_STUDIES_URL` | the list each cycle returns to |

The defaults are the platform's own addresses, set in the profile's
`__init__.py`.

**The rules the model follows** on the platform are appended to every run's
task. They are two texts in the profile's `__init__.py`:

- `_STANDING_RULES_FULL`, used at normal speed;
- `_STANDING_RULES_COMPACT`, used in the faster speed modes.

Edit them there, for example to change which studies to prefer or skip. Keep
the two versions consistent, and keep them short: rules buried in a long text
were measured to be ignored. Your own instructions for one kind of run belong
in the task or the context in the panel (and in a preset), not here.

**Other texts and patterns** in the same file, each with a comment explaining
why it exists:

| Name | What it is |
|---|---|
| `_DEAD_END_MARKERS` | page wording that means a study cannot be done (not eligible, full, already done, screened out) |
| `_SEL_RESERVE`, `_SEL_OPEN_DIALOG`, `_SEL_DIALOG`, `_SEL_START` | the buttons of the study launch dialog |
| `_COMPLETION_RETURN_JS`, `_NOCODE_LINK_JS` | how the "return to the platform" and "I didn't receive a completion code" controls are found |
| `_TIME_REMAINING_RE` | the platform's countdown for a reserved study |
| `reservation_nudge()`, `nocode_refusal()`, `completion_pushback_text()`, ... | what the model is told in each situation |
| `_UNATTENDED_ANSWER` | the answer given in place of a human during loop runs |

If the platform changes its pages, these are the places to update. After a change,
run the offline tests (see the README); `scripts/tools/test_harness_guards.py`
covers most of this file.

**What the agent does** on a task platform (when the timer starts, when a
submit is held, how a reserved slot is launched, when a terminate is refused)
is not in the profile but in the `TaskPlatform` behaviour,
[agent/behaviors/task_platform.py](../agent/behaviors/task_platform.py). It is
shared by every task-platform profile, so change it only for a rule that holds
on all of them.

## Adding a platform

1. Create `agent/sites/<name>/__init__.py` with a subclass of `sites.base.Site`.
   Every attribute and method of `Site` has a generic default, so set only what
   the platform needs. The included platform profile is a complete example.

   ```python
   import re
   from ..base import Site

   class ExampleSite(Site):
       name = "example"                       # value of FARA_SITE / the menu
       title = "Example"                      # used in texts ("back on Example")
       hosts = ("example.com",)               # the platform's domains
       app_host = "app.example.com"           # where the work happens
       home_path = re.compile(r"^/tasks/?$")  # the task list
       home_path_str = "/tasks"
       task_path = re.compile(r"^/tasks/(\d+)/?$")      # group(1) = task id

       def home_url(self):
           return "https://app.example.com/tasks"

       def standing_rules(self, compact=False):
           return "\n\nRULES: ..."            # appended to every run's task
   ```

   If the site is a task platform (a list of tasks, each launched, timed and
   handed back), have the profile run the
   task-platform rules:

   ```python
       def behaviors(self):
           from behaviors.task_platform import TaskPlatform
           return (TaskPlatform,)
   ```

   Leave `behaviors()` out for a site that only needs its pages described.

2. Register it in `agent/sites/__init__.py`:

   ```python
   from .example import ExampleSite
   register(ExampleSite)
   ```

3. Add tests. `tests/site_profiles_test.py` shows how to check that a profile
   is picked from its URLs and how to drive a whole run with the browser and the
   model stubbed.

The main groups of attributes in `Site`:

| Group | Examples |
|---|---|
| URL model | `hosts`, `app_host`, `forbidden_path`, `home_path`, `task_path`, `complete_path`, `launch_chrome_prefixes`, `hosted_task_path` |
| Page knowledge | `empty_list_markers`, `dead_end_markers`, `deadline_re`, `held_slot_banner_re`, `submission_confirmation_re`, `task_title_js`, `completion_return_js`, `nocode_link_js`, the `launch_*_sel` selectors |
| Identity | `participant_id_label_re`, `participant_id_params`, `participant_id_format` |
| Account history | `history_url`, `parse_history_rows()`, `parse_history_header()`, `history_paid_statuses` |
| Texts for the model | `standing_rules()`, `identity_hint()`, `unattended_answer()`, the refusal and notice methods |
| Rules the loop runs with | `behaviors()` |

## Writing a behaviour

When a site needs the loop to *do* something different, not just to know its
pages, write a behaviour: a subclass of `behaviors.Behavior` in
`agent/behaviors/`, overriding only the hooks it needs. The module docstring of
[agent/behaviors/\_\_init\_\_.py](../agent/behaviors/__init__.py) lists the
hooks in the order the loop calls them. In short:

| Hook | When | Can |
|---|---|---|
| `start` | before the start URL is opened | set up per-run state |
| `arrive` | the step's page is known | move the browser, end the run, yield notices |
| `settle` | after batch fill | per-step bookkeeping, yield notices |
| `after_screenshot` | with the page's text | read it |
| `first_prompt`, `prompt_hints`, `prompt_suffix` | building the model's prompt | add text |
| `before_action` | before the model's action runs | hold it back, re-aim it |
| `after_action` | after it ran | set a reply for the next step |
| `diag` | the step's diagnostic line | add fields |
| `terminate` | the model wants to stop | refuse, with a reply |
| `finish_record`, `finished` | the run is over | add fields, notify |

Everything a hook needs is on the `run` object it receives: the URL
(`run.here`), the reply for the model's next step (`run.pending`), the action
(`run.action_type`, `run.args`), the transcript (`run.write`) and the run's
options. Generic parts of the loop that a behaviour may switch off or on are
named features (`batch_fill`, `reading_dwell`, `prescroll`, ...): override
`allows()` to veto one, `wants()` to ask for one.

## Tests

The suites under `tests/` and `scripts/tools/` that test platform behaviour pin
`FARA_SITE` to the platform profile at the top; `tests/site_profiles_test.py` covers profile
selection, the generic profile and the behaviour hooks, and needs no browser or
model.

`tests/loop_scenarios.py` drives whole runs with the browser, the model, the
clock and the notifications replaced at their boundaries, and records
everything the loop sends out (prompts, events, navigations, input, the
transcript). Record it before a change to the loop or to a behaviour and
compare after:

```
python tests/loop_scenarios.py --record before.json
python tests/loop_scenarios.py --compare before.json
```
