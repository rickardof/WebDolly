# Site profiles

The agent loop (`agent/agent_core.py`) is a generic browser agent: it takes a
screenshot, asks the model what to do, and does it with a human-like mouse and
keyboard. It knows nothing about any particular website.

Everything specific to one website lives in a **site profile** under
`agent/sites/`. A profile describes a *task platform*: a site that lists units
of work, opens one (often on another host), and takes a completion back. The
loop asks the active profile every question that depends on the site, for
example:

- is this URL part of the platform, and which section of it?
- what does an empty task list look like?
- which control launches a task, and which one returns it?
- what must the model be told on this platform (its rules, its refusals)?
- how is the account's history read?

Two profiles ship with the code:

| Profile | What it is |
|---|---|
| `generic` | no platform. Every platform rule is switched off and nothing platform-specific reaches the model. Use it for any ordinary website. |
| `prolific` | the participant side of [Prolific](https://www.prolific.com): its URLs, launch dialog, completion codes, minimum and maximum time per study, account history, and the rules the model follows there. |

## Which profile is used

`FARA_SITE` names the profile explicitly (`generic`, `prolific`, ...). When it
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

With `generic` the loop is a plain browser agent:

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

The included platform profile is `prolific`.


Everything Prolific-specific is in two files:
[agent/sites/prolific/\_\_init\_\_.py](../agent/sites/prolific/__init__.py) (pages,
rules, texts) and
[agent/sites/prolific/submissions.py](../agent/sites/prolific/submissions.py)
(reading the submission history).

**Without touching code**, set environment variables before starting:

| Variable | Default | What it changes |
|---|---|---|
| `FARA_GUARDED_HOSTS` | `prolific.com,prolific.co` | the domains the profile owns |
| `FARA_APP_HOST` | `app.prolific.com` | the host the studies live on (also learnt from the start URL) |
| `FARA_STUDIES_URL` | `https://app.prolific.com/studies` | the list each cycle returns to |

**The rules the model follows** on Prolific are appended to every run's task.
They are two texts in `agent/sites/prolific/__init__.py`:

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
| `_COMPLETION_RETURN_JS`, `_NOCODE_LINK_JS` | how the "return to Prolific" and "I didn't receive a completion code" controls are found |
| `_TIME_REMAINING_RE` | Prolific's countdown for a reserved study |
| `reservation_nudge()`, `nocode_refusal()`, `completion_pushback_text()`, ... | what the model is told in each situation |
| `_UNATTENDED_ANSWER` | the answer given in place of a human during loop runs |

If Prolific changes its pages, these are the places to update. After a change,
run the offline tests (see the README); `scripts/tools/test_harness_guards.py`
covers most of this file.

## Adding a platform

1. Create `agent/sites/<name>/__init__.py` with a subclass of `sites.base.Site`.
   Every attribute and method of `Site` has a generic default, so set only what
   the platform needs. `agent/sites/prolific/__init__.py` is a complete example.

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

## Tests

The suites under `tests/` and `scripts/tools/` that test platform behaviour pin
`FARA_SITE=prolific` at the top; `tests/site_profiles_test.py` covers profile
selection and the generic profile, and needs no browser or model.
