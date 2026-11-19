"""TaskPlatform: the loop's rules for a site that hands out timed tasks.

A task platform lists units of work, launches one (often on another host),
times it, and takes it back with a completion code. Everything the agent
loop does because of that lives here; the site profile supplies the URLs,
page patterns and texts (`sites.current`), this class supplies what to do
with them:

  * the launch surface (list, task page, account pages) and the moment a
    task starts, which starts the task timer;
  * the guards: forbidden sections, lingering off the list, the marketing
    site, a completion code in the URL;
  * tasks that lead nowhere (and, opt-in, audio/video ones) are skipped and
    remembered;
  * completion codes seen on the task's pages, the platform's submit/cancel
    confirmations, the researcher's quality-check verdict;
  * the minimum time: a submit is held until it is up, the model is told
    how long is left, the speed is raised when the task runs long;
  * the reserved-slot guard and the deterministic launcher;
  * the terminate guards: no NOCODE before the task was even opened, the
    "I didn't receive a code" control pressed instead of argued about, the
    survey's own return control clicked, a free-text "done" pushed back once;
  * the end of a task: the code check, notifications, study_memory.

Run options it reads (keyword arguments of run_agent):

  wander_seconds  how long the agent may linger on the platform's pages off
                  the task list before it is taken back to it (0 = never);
  skip_av         abandon a study whose page needs audio, video, a webcam or
                  a microphone, instead of relying on the task text to tell
                  the model to avoid them. Either way, a study ruled out is
                  remembered in study_cache so later cycles do not open it.

Helpers it calls are agent_core's (`core.X`), looked up at call time.
"""

import random
from urllib.parse import urlparse

import agent_core as core
import sites
import study_cache

from . import Behavior


class TaskPlatform(Behavior):
    name = "task_platform"

    # --------------------------------------------------------------- state
    def start(self, run, start_url):
        if start_url:
            core._learn_app_host(start_url)
        self.wander = core.WanderGuard(run.options.get("wander_seconds", 0))
        self.study_timer = core.StudyTimer()
        run.timer = self.study_timer
        self.skip_av = bool(run.options.get("skip_av"))
        self._last_guarded_duration = None  # persists across clicks within a study entry
        # the platform's remaining-time countdown, read off the detail page
        # before the launch click. See _extract_time_remaining.
        self._last_guarded_deadline = None
        self._prev_was_guarded = False  # tracks previous step's site so ANY action triggers timer
        # Prev step was on the platform's launch surface (list/detail/chrome).
        # Leaving it is what starts the task timer.
        self._prev_on_launch = bool(sites.current.hosts)
        # Page object the launch surface was last seen in, and the chrome
        # URLs we have already explained once (see _reached_in_launch_tab).
        self._launch_page_id = None
        self._cur_page_id = None
        self._chrome_guard_noticed = set()
        self._current_study_id = None  # id of the study being looked at / taken
        self._current_study_title = ""  # its name, read on the platform while we can still see it
        self._seen_codes = []  # completion codes spotted on the study's own pages
        self._survey_url = ""  # URL of the external survey (for study_memory)
        # Completion guard: nudge a free-text "I'm done" that never got back to
        # the platform / a code, at most once per study so a re-terminate is obeyed.
        self._completion_pushed_back = False
        # One shot per study at clicking the survey's own return-to-the-platform
        # control ourselves (see the profile's completion_return_js).
        self._completion_return_tried = False
        # NOCODE guard: refuse a terminate NOCODE emitted while still on the
        # the platform detail page without having visited the external survey or
        # seen a platform-side cancel/submit confirmation. See
        # guarded_nocode_refusal for the full rationale.
        self._saw_external_this_study = False
        self._saw_cancel_confirm = False
        self._nocode_guard_attempts = 0
        # Sibling guard, platform side: a no-code terminate while the
        # 'Confirm submission' panel is still open. See _nocode_link_box.
        self._nocode_link_attempts = 0
        self._nocode_link_tried = False
        # Reservation-loop guard: once a slot is reserved on the platform the URL
        # becomes /studies/{id} and stays there — the only legitimate exits
        # are 'Start study' or 'Cancel participation'. The model sometimes
        # tries to escape with history_back / visit_url instead (seen in
        # run_20261103_194847 after an older user_hint was misread as a
        # standing rule to cancel). Count those abort attempts on the same
        # detail URL; after RESERVED_LOOP_THRESHOLD we inject a nudge that
        # names the only two valid exits.
        self._reserved_loop_url = ""
        self._reserved_loop_count = 0
        # Any step that ends still on /studies/{id} with no URL change from
        # the previous step counts as 'stuck'. The model in run_20261104_073003
        # wasted 13 steps tab-switching and pressing Win/Ctrl+L while the
        # URL never moved -- none counted as abort attempts, so the original
        # history_back-only guard never fired. Fallback threshold is lower
        # than the abort-attempt one because 'stuck' already means no useful
        # browser transition is happening.
        self._reserved_stuck_count = 0
        # One-shot per reservation. Gates BOTH the deterministic launcher (when
        # the agent is stuck on a fresh, un-launched reservation the harness
        # opens the external survey itself via pw_bridge.launch_reserved_study,
        # using stable data-testid selectors, instead of leaving a fragile
        # pixel-click + below-the-fold confirm to the vision model) AND the
        # text nudge that is its fallback.
        self._reserved_loop_nudged = False
        # Orphan-reservation guard: see _orphan_reservation_visible. On the
        # reserved detail page the model has been seen to hallucinate a
        # 'Take part in this study' button that does not exist (the panel
        # actually shows 'I'm finished' + 'Cancel participation'), then
        # click into the Confirm-submission flow with no code to enter.
        # Nudged once per visit to the detail page, reset on return to
        # /studies so re-entry re-fires it after context compaction.
        self._orphan_nudged = False
        # First "your submission failed our checks" wording seen this run,
        # read off the fingerprint the change signal already computes, so
        # noticing it costs no extra page read. Kept per run: it goes into
        # the step's diag line, and from there into summary.json.
        self._qc_flag = ""
        # Per step.
        self.here_guarded = False
        self.here_on_launch = False
        self.on_list = False
        self.m_study = None
        self.skip_hint = ""
        self.qc_hit = ""
        self._timer_started_this_step = False
        self.pre_click_duration = None

    # ------------------------------------------------------------ features
    def allows(self, run, feature):
        if feature == "batch_fill":
            # Gate on the LAUNCH SURFACE, not on the host. Batch-fill must
            # never touch the platform's own chrome (the studies list, a detail
            # page, submissions/account) -- but a platform-HOSTED task
            # (/data-collection-tool/...) is a running questionnaire like any
            # other, and `here_guarded` locked it out of batch-fill purely
            # because it shares the domain. run_20261106_154221 answered a
            # 3-minute dropdown questionnaire with 139 separate vision clicks
            # at 8.3s each, took 22m40s against the platform's ~15m window, and
            # the submission was auto-returned. Same confusion the wander
            # guard had in 1ffb26d.
            return not self.here_on_launch
        if feature == "page_hints":
            # Nothing to hint on the studies list.
            return not self.on_list
        if feature == "reading_dwell":
            # Gated on the launch surface, not the host. Skipping the dwell
            # on a platform-HOSTED questionnaire made the agent answer real
            # questions with no reading time at all -- the exact behavioural
            # signature the humanised input path exists to avoid -- purely
            # because the task shares the platform's domain. `on_list` stays:
            # there is nothing to read on the studies list.
            # ...but never at the cost of the slot: reading dwell is the
            # first thing to drop once the platform's countdown is inside its
            # last quarter. A page read like a human but submitted after the
            # deadline is auto-returned and pays nothing.
            _dl_pressure = self.study_timer.deadline_pressure()
            _dwell_ok = _dl_pressure is None or _dl_pressure < 0.75
            return not self.on_list and not self.here_on_launch and _dwell_ok
        if feature == "remember_page":
            # Only pages with no fillable form reach this far (batch-fill
            # returns before it), which is exactly the scenario / instruction
            # / stimulus set a later question can refer back to. Never the
            # platform's own pages.
            return not self.here_guarded
        return True

    def wants(self, run, feature):
        if feature == "prescroll":
            # Pre-screenshot viewport nudge: in speed modes, inside a study,
            # on non-guarded pages (the platform dashboard is excluded).
            return self.study_timer.is_active() and not self.here_guarded
        if feature == "auto_enter":
            # Only while a study is running.
            return self.study_timer.is_active()
        return False

    # -------------------------------------------------------------- arrive
    def arrive(self, run):
        here = run.here
        study_timer = self.study_timer
        if core.is_forbidden_url(here):
            back = core.safe_page_for(here)
            core.pw_bridge.goto(back)
            core.pw_bridge.wait_for_content()
            run.pending = sites.current.forbidden_notice()
            yield {"type": "notice",
                   "text": sites.current.forbidden_live_notice(),
                   "messages": run.messages}
        elif self.wander.check(here):
            core.pw_bridge.goto(core.safe_page_for(here))
            core.pw_bridge.wait_for_content()
            n = int(self.wander.seconds)
            run.pending = sites.current.wander_notice(n)
            yield {"type": "notice",
                   "text": sites.current.wander_live_notice(n),
                   "messages": run.messages}

        # Back on the platform with the completion code in the URL: the
        # submission is already made, so finish here instead of asking the
        # model to read the code off the screen.
        cc = core.completion_code(here)
        if cc:
            core.capture_page("complete")
            answer = f"CODE: {cc}"
            # Backstop: the submit guards above hold the final submit until
            # the timer clears, so reaching the completion URL early should
            # be impossible. If it happens anyway (e.g. a survey whose only
            # end control is an unlabeled arrow that the label classifier
            # cannot recognise as a submit), the study is already returned
            # and cannot be undone -- but flag it loudly instead of logging
            # a silent success, so the slipping survey can be handled.
            early = (study_timer.is_active()
                     and study_timer.remaining() > core.TERMINATE_GRACE_SECONDS)
            run.write({
                "step": run.step, "action_type": "terminate", "args": {"answer": answer},
                "code_source": "url", "url": here,
                "early_return": bool(early),
                "timer_remaining": round(study_timer.remaining(), 1) if early else None,
            })
            core._restore_user_speed()
            core._close_extra_tabs()
            if early:
                rem = int(study_timer.remaining())
                core._live({"type": "notice",
                            "text": f"⚠️ Studio restituito {rem}s SOTTO il tempo minimo "
                                    f"(min {study_timer.min_seconds:.0f}s) via URL: submit sfuggito alle gate."})
                core._notify_telegram(f"⚠️ Fara: studio restituito TROPPO PRESTO\n{answer}\n"
                                      f"{rem}s sotto il minimo ({study_timer.min_seconds:.0f}s)\n{here}")
            else:
                core._notify_telegram(f"Fara studio completato ✅\n{answer} (dall'URL)\n"
                                      f"Ciclo: {run.run_dir.name if run.run_dir else '?'}")
            yield {"type": "notice", "text": f"Codice di completamento letto dall'URL: {cc}",
                   "messages": run.messages}
            yield {"type": "done", "answer": answer, "messages": run.messages}
            return True

        # Stateful transition check: if previous step was on the platform and now we're
        # external, start the timer regardless of HOW the navigation happened
        # (left_click, key press, Enter in a modal, JS redirect, etc.).
        # Done BEFORE the batch-fill block so the timer is already active when
        # batch-fill decides whether to hold a final submit on the very first
        # external page (a one-page fill-and-submit survey would otherwise be
        # submitted before the timer ever started).
        here_guarded = core._on_guarded_site(here)
        # Stranded on the marketing site (a click on its logo/nav, or a
        # redirect, not just a mistyped visit_url): walk straight back to
        # the app. Nothing on the platform's marketing site can advance a study, and
        # the model does not recognise it as the wrong site -- it treated
        # /ai-task-builder as a broken studies list for 23 steps
        # (run_20261106_142347). Never fires inside a study: an external
        # survey is not on the guarded domain at all.
        if core._off_app_host(here):
            back = sites.current.app_home_url()
            core._live({"type": "notice",
                        "text": f"[guard] off the app host ({urlparse(here).hostname}) "
                                f"-> back to {back}"})
            try:
                core.pw_bridge.goto(back)
                core.pw_bridge.wait_for_content()
                here = core.pw_bridge.current_url() or back
            except Exception:
                here = back
            here_guarded = core._on_guarded_site(here)
            if not run.pending:
                run.pending = sites.current.off_host_notice(back, sites.current.app_host)
        run.here = here
        self.here_guarded = here_guarded
        self._timer_started_this_step = False
        # Proactive, cache-aware compaction at natural checkpoints: when
        # we cross the platform/external boundary (either direction),
        # most of the history for the OTHER side (list browsing, old
        # studies, prior survey pages) is no longer useful. Collapse it
        # once BEFORE this step grows the context further, so the model
        # starts the new page with a small stable prefix and llama's
        # prompt cache can warm cheaply for the next 10+ steps.
        if self._prev_was_guarded != here_guarded and len(run.messages) > 6:
            core._hard_compact_context(run.messages, keep_recent=2)
        here_on_launch = core._on_launch_surface(here)
        try:
            self._cur_page_id = core.pw_bridge.active_page_id()
        except Exception:
            self._cur_page_id = None
        if not here_on_launch and core._reached_in_launch_tab(
                here, self._cur_page_id, self._launch_page_id):
            here_on_launch = True
            if here not in self._chrome_guard_noticed:
                self._chrome_guard_noticed.add(here)
                core._live({"type": "notice", "text":
                            sites.current.chrome_guard_live_notice(here)})
        self.here_on_launch = here_on_launch
        # "Inside a task" = off the launch surface: an external survey host
        # OR a platform-hosted task tool (e.g. /data-collection-tool/...).
        # The study timer and in-task framing key on THIS transition, not on
        # "left the platform's domain", so first-party platform surveys are treated as
        # the running task they are instead of looking like the reservation
        # page (which made the model cancel completable studies).
        entered_task = self._prev_on_launch and not here_on_launch
        if self._prev_was_guarded and not here_guarded:
            # NOCODE-guard signal: strictly "reached an EXTERNAL survey host"
            # (unchanged on purpose -- see guarded_nocode_refusal).
            self._saw_external_this_study = True
        if entered_task and not study_timer.is_active():
            effective_duration = self._last_guarded_duration
            if effective_duration is not None:
                study_timer.start(effective_duration * 60,
                                  deadline_seconds=self._last_guarded_deadline or 0)
                core._live({"type": "timer_start", "min_seconds": study_timer.min_seconds,
                            "min_minutes": effective_duration})
            else:
                study_timer.start(0, deadline_seconds=self._last_guarded_deadline or 0)
                core._live({"type": "timer_start", "min_seconds": 0, "min_minutes": 0})
            self._timer_started_this_step = True
            self._survey_url = here
            dest = "external" if not here_guarded else f"{sites.current.name}-hosted task"
            core._live({"type": "notice", "text": f"[timer] transition detected: launch→{dest} | duration={effective_duration}min"})
            dur_str = f" (~{effective_duration:.0f} min)" if effective_duration else ""
            core._notify_telegram(f"📝 Studio iniziato{dur_str}\n{here}")
        self._prev_was_guarded = here_guarded
        self._prev_on_launch = here_on_launch
        if here_on_launch and self._cur_page_id is not None:
            self._launch_page_id = self._cur_page_id
        return False

    # -------------------------------------------------------------- settle
    def settle(self, run):
        here = run.here
        here_guarded = self.here_guarded
        study_timer = self.study_timer
        # Reset timer when back at the studies list (timer re-arms for next study).
        on_list = here_guarded and bool(sites.current.home_path.match(urlparse(here).path or ""))
        self.on_list = on_list
        if on_list:
            study_timer.reset()
            core._restore_user_speed()
            self._last_guarded_duration = None
            self._last_guarded_deadline = None
            self._prev_was_guarded = False
            self._current_study_id = None
            self._current_study_title = ""
            self._seen_codes = []
            self._completion_pushed_back = False
            self._completion_return_tried = False
            self._saw_external_this_study = False
            self._saw_cancel_confirm = False
            self._nocode_guard_attempts = 0
            self._nocode_link_attempts = 0
            self._nocode_link_tried = False
            run.study_pages = []
            self._reset_reserved()
            # Back on the studies list: everything from the previous
            # study is history that will never be relevant again --
            # collapse it in one shot so the next cycle starts with a
            # tiny cache-stable context.
            if len(run.messages) > 6:
                core._hard_compact_context(run.messages, keep_recent=1)

        # --- studies that lead nowhere (and, opt-in, audio/video ones) ---
        # A study that is listed but not doable gets opened again on every
        # cycle: the model has no memory across cycles, so without this it
        # re-discovers the same dead end for as long as it stays listed.
        # The wording is only read on the study page, where it is a
        # description; on an external questionnaire only real media
        # elements count, because aborting a survey halfway on a false
        # positive costs much more than one wasted click.
        m_study = sites.current.task_path.match(urlparse(here).path or "") if here_guarded else None
        self.m_study = m_study
        if m_study:
            self._current_study_id = m_study.group(1)
            core.capture_page("study")
            # Read the name here, on the platform: once the agent is inside the
            # external questionnaire the page title is the survey tool's,
            # which is no use for telling the model what to skip.
            self._current_study_title = core._study_title() or self._current_study_title

        skip_reason = ""
        if m_study:
            known = study_cache.entries().get(self._current_study_id)
            if known:
                # Opened again anyway (the title hint is only a hint): bounce
                # out before the model spends steps rediscovering it.
                skip_reason = known.get("reason", "scartato in un ciclo precedente")
            else:
                page_text = core._page_text()
                skip_reason = core._detect_dead_end(page_text)
                if not skip_reason and self.skip_av:
                    av = core._detect_av(page_text, check_text=True)
                    if av:
                        skip_reason = f"audio/video: {av}"
        # Gated on the launch surface, not the host: a platform-HOSTED
        # task (/data-collection-tool/...) that turns out to need a
        # microphone or webcam has to be abandoned exactly like an
        # external one. `here_guarded` excluded it for sharing the
        # domain. Same confusion as 1ffb26d and 88c15c4.
        elif self.skip_av and not self.here_on_launch and study_timer.is_active():
            av = core._detect_av(check_text=False)
            if av:
                skip_reason = f"audio/video: {av}"

        if skip_reason:
            title = self._current_study_title or core._study_title()
            key = self._current_study_id or title or here
            study_cache.mark(key, skip_reason, title=title, url=here)
            run.write({"step": run.step, "type": "skip", "reason": skip_reason,
                       "study": key, "title": title, "url": here,
                       "matched": core._last_dead_end_sentence})
            abandoned = study_timer.is_active()
            study_timer.reset()
            core._restore_user_speed()
            self._last_guarded_duration = None
            self._last_guarded_deadline = None
            self._current_study_id = None
            self._current_study_title = ""
            try:
                core.pw_bridge.goto(core.studies_url())
                core.pw_bridge.wait_for_content()
                self._prev_was_guarded = True
                self._prev_on_launch = True   # back on the studies list (a launch surface)
            except Exception:
                pass
            run.pending = (
                f"That study was ruled out automatically ({skip_reason}) and you were "
                "taken back to the list of studies. Do not open it again: pick another "
                "study, or follow your task instructions if none is eligible.")
            label = f"«{title}»" if title else "lo studio aperto"
            link = (f"\n{sites.current.task_url(key)}"
                    if sites.current.task_url(key) else "")
            core._notify_telegram(f"⏭ Studio saltato: {label}\n{skip_reason}{link}\n"
                                  "Se ti interessa, puoi farlo a mano.")
            injected = yield {"type": "notice",
                              "text": (f"Studio saltato ({skip_reason}): {label}"
                                       + (", survey abbandonata" if abandoned else "")
                                       + ". Non verrà riaperto nei prossimi cicli."),
                              "messages": run.messages}
            if injected:   # an instruction typed while this was happening
                run.pending += "\n\n" + injected

        # Codes shown on the study's own pages, collected as they appear:
        # by the time the model reports one it is back on the platform, where
        # the original page can no longer be checked.
        if study_timer.is_active() and not here_guarded:
            for c in core.codes_in_url(here) + core.codes_on_page(core._end_page_text()):
                if c not in self._seen_codes:
                    self._seen_codes.append(c)

        # Flag platform-side submit/cancel confirmations so the NOCODE
        # guard knows this terminate came after a legitimate close
        # ("I didn't receive a code" confirmation OR Cancel participation
        # confirmation), not from clicking "I'm finished" on the detail
        # page without ever submitting.
        if here_guarded and not self._saw_cancel_confirm:
            try:
                if core.saw_submission_confirmation(core._end_page_text()):
                    self._saw_cancel_confirm = True
            except Exception:
                pass

        self.skip_hint = study_cache.prompt_line() if on_list else ""

    def _reset_reserved(self):
        self._reserved_loop_url = ""
        self._reserved_loop_count = 0
        self._reserved_stuck_count = 0
        self._reserved_loop_nudged = False
        self._orphan_nudged = False

    # ---------------------------------------------------------- the page
    def after_screenshot(self, run, page_fp):
        # the platform's own pages list past rejections with their reasons; reading
        # one there would stamp this run with someone else's verdict. The
        # researcher's end page is never on a guarded host.
        qc_hit = "" if (self._qc_flag or self.here_guarded) else core.qc_failure_in(page_fp)
        if qc_hit:
            self._qc_flag = qc_hit
        self.qc_hit = qc_hit

    # ------------------------------------------------------------- prompts
    def first_prompt(self, run):
        resume_hint = ""
        # Off the task list at step 1 means a cycle is resuming work a
        # previous one started -- on a platform.
        if not self.on_list and sites.current.home_url():
            resume_hint = f"\n\nCurrent URL: {run.here}\n"
            if self.here_guarded and self.m_study:
                resume_hint += sites.current.resume_on_task_page()
            else:
                resume_hint += sites.current.resume_in_task()
        return (f"\n\n{self.skip_hint}" if self.skip_hint else "") + resume_hint

    def prompt_hints(self, run):
        return self.skip_hint

    def prompt_suffix(self, run):
        study_timer = self.study_timer
        out = ""
        # platform-hosted task surface (e.g. the data-collection-tool):
        # it lives on the app host, so the model keeps mistaking a
        # trial it is actively doing for the studies list / reservation
        # page and cancels a study it is completing. State plainly where
        # it is. (Guarded + off the launch surface == a first-party task.)
        if self.here_guarded and not self.here_on_launch:
            out += sites.current.in_task_framing()
        timer_line = study_timer.status_line()
        if timer_line:
            out += timer_line + "\n"
            core._live({"type": "timer", "elapsed": study_timer.elapsed(),
                        "remaining": study_timer.remaining(),
                        "min_seconds": study_timer.min_seconds})
            if study_timer.min_seconds > 0:
                over = study_timer.elapsed() - study_timer.min_seconds
                if over > 120:
                    core._auto_escalate("super_fast")
                elif over > 0:
                    core._auto_escalate("hurry")
        return out

    # ------------------------------------------------------------- actions
    def before_action(self, run):
        here = run.here
        here_guarded = self.here_guarded
        study_timer = self.study_timer
        action_type, args = run.action_type, run.args
        # Read study duration from the platform page BEFORE dispatch so we
        # capture the exact study about to be joined (not generic browsing).
        # Pass click coordinates so elementFromPoint targets that specific card.
        self.pre_click_duration = None
        if action_type == "left_click" and core._on_guarded_site(here):
            coord = args.get("coordinate")
            cx, cy = (coord[0], coord[1]) if coord else (None, None)
            self.pre_click_duration = core._extract_study_duration(cx, cy)
            if self.pre_click_duration is not None:
                self._last_guarded_duration = self.pre_click_duration
            # the platform's countdown is only on the detail page, so it has
            # to be read before the click that leaves it.
            _pcd = core._extract_time_remaining()
            if _pcd is not None:
                self._last_guarded_deadline = _pcd

        # Minimum-time guard, model-driven path: on an external survey, while
        # the study timer is still below its minimum, a left_click that lands
        # on a submit/finish control is held and turned into a wait instead of
        # being dispatched. Once submitted the survey redirects to the platform
        # with the completion code and the study is returned irreversibly, so
        # the hold has to happen here, before dispatch. Next/Continue clicks
        # (and every click off a submit control) are not affected. Setting
        # the pending reply also suppresses the auto-chain-Enter after the
        # action, which would otherwise submit the page the click did not.
        # Near-miss on the control that ends the study: re-aim, don't
        # waste the click. Runs BEFORE the minimum-time guard below so the
        # corrected coordinate is what that guard judges -- a re-aimed
        # click can still be held back if the study minimum is not up.
        if action_type == "left_click" and not here_guarded:
            _coord = args.get("coordinate")
            if _coord:
                _sbox = core._completion_return_near_miss(_coord[0], _coord[1])
                if _sbox:
                    _nx = _sbox["x"] + _sbox["width"] * random.uniform(0.3, 0.7)
                    _ny = _sbox["y"] + _sbox["height"] * random.uniform(0.35, 0.65)
                    run.action_note = (f" [re-aimed {_coord[1]:.0f}->{_ny:.0f}y onto "
                                       f"the return control]")
                    args["coordinate"] = [_nx, _ny]
                    core._live({"type": "notice", "text":
                                "[completion] click missed the survey's return control "
                                f"({_sbox.get('text')!r}) by {_sbox.get('miss')}px: "
                                "re-aimed onto it"})

        submit_held = ""
        if (action_type == "left_click"
                and not here_guarded
                and study_timer.is_active()
                and study_timer.remaining() > core.TERMINATE_GRACE_SECONDS):
            coord = args.get("coordinate")
            if coord:
                submit_held = core._click_hits_submit(coord[0], coord[1])

        if submit_held:
            rem = int(study_timer.remaining())
            result_desc = (f"SUBMIT HELD (study timer): '{submit_held}' not clicked, "
                           f"~{rem}s below the study minimum")
            core._live({"type": "notice",
                        "text": f"[timer] submit '{submit_held}' trattenuto: {rem}s sotto il minimo"})
            try:
                core.cancel.sleep(min(30.0, max(5.0, study_timer.remaining())))
            except core.cancel.Cancelled:
                raise
            except Exception:
                pass
            run.pending = (study_timer.status_line() or
                           "Do NOT submit yet; wait for the study minimum time.")
            return result_desc

        # Reading dwell: before clicking a Next/Continue/>> button on
        # an external survey, pause proportionally to the visible text
        # so the page isn't traversed at inhuman speed.
        if (action_type == "left_click" and not here_guarded
                and study_timer.is_active()):
            coord = args.get("coordinate")
            if coord:
                nav_label = core._click_hits_nav(coord[0], coord[1])
                if nav_label:
                    try:
                        page_text = core._page_text(12000)
                        dwell = core._reading_dwell(page_text)
                        if dwell > 0:
                            core._live({"type": "notice",
                                        "text": f"[dwell] reading pause {dwell:.1f}s before '{nav_label}'"})
                            core.cancel.sleep(dwell)
                    except core.cancel.Cancelled:
                        raise
                    except Exception:
                        pass
        return None

    def after_action(self, run):
        here = run.here
        action_type, args = run.action_type, run.args
        # Reservation-loop guard: on a platform /studies/{id} detail page
        # where the orphan banner is visible, the slot is held and the only
        # way out is 'I'm finished' (with a real code) or 'Cancel
        # participation'. Two independent signals feed one nudge:
        #   (A) orphan-detector: on first entry with the banner visible,
        #       inject a one-shot clarification of what the buttons mean
        #       (the model was observed hallucinating a non-existent 'Take
        #       part' button over the actual 'I'm finished' in
        #       run_20261104_073003).
        #   (B) loop-detector: count explicit abort attempts PLUS any step
        #       that ends on the same detail URL without progress; fire the
        #       nudge after 2 abort attempts or 6 stuck steps.
        m_resv = sites.current.task_path.match(urlparse(here).path or "") if self.here_guarded else None
        if not m_resv:
            # Off the detail page: counters reset.
            self._reset_reserved()
            return
        detail_id = m_resv.group(1)
        # A visit to a different detail page resets everything for it.
        if self._reserved_loop_url != detail_id:
            self._reset_reserved()
            self._reserved_loop_url = detail_id

        # Launch state of this reserved slot. A study whose survey was
        # never opened this run (_saw_external_this_study False) and
        # that STILL OFFERS a way in ('Take part', 'Open study in new
        # window', or an open 'Start study' dialog) is mid-launch, not
        # a dead slot: both nudges must push the LAUNCH, never 'Cancel
        # participation', because the platform's dialog hides its confirm
        # link below the fold and abandoning there returns a
        # completable study (run_20261105_181001 lost a GBP 4 eval,
        # run_20261106_124620 a GBP 2,60 one, exactly this way).
        #
        # The orphan BANNER cannot carry that distinction, in either
        # direction. Captured: PRESENT on a never-launched
        # reservation that still offered 'Open study in new window'
        # (run_20261106_124620 screenshot_03), ABSENT on a reserved
        # study with its Start dialog open (fixture study 6aec5848,
        # the captured fixtures). Keying fresh_unlaunched off it made
        # the flag permanently False on the pages it was meant to
        # protect, which silently disabled the auto-launcher and
        # routed every stuck launch to the cancel steer. The DOM
        # launch control is the honest signal; the banner only says
        # whether the slot is already held.
        orphan_visible = core._orphan_reservation_visible()
        fresh_unlaunched = (core._launch_control_visible()
                            and not self._saw_external_this_study)

        # (A) Held-slot one-shot informational nudge, branched on the
        #     same launch state so a held-but-launchable study is told
        #     to start, not to cancel.
        if not run.pending and not self._orphan_nudged:
            if orphan_visible and fresh_unlaunched:
                self._orphan_nudged = True
                run.pending = core._reservation_nudge(True, detail_id)
            elif orphan_visible:
                self._orphan_nudged = True
                run.pending = sites.current.held_slot_notice()

        # (B) Loop detector. Normalise keys to a lowered '+'-joined
        # string so patterns like 'alt+left' match regardless of how
        # the model spelt the modifiers.
        keys_joined = "+".join(
            (k or "").lower() for k in (args.get("keys") or [])
        )
        is_abort_attempt = (
            action_type in ("history_back", "visit_url")
            or (action_type == "key" and keys_joined in (
                "backspace", "alt+left", "alt+right", "ctrl+l",
                "ctrl+tab", "ctrl+shift+tab", "ctrl+next",
                "ctrl+prior", "ctrl+pagedown", "ctrl+pageup",
            ))
        )
        # Clicks on the top the platform nav header (the Studies /
        # Submissions / About you / Messages tabs all sit within the
        # first ~50px) are escape attempts too. The reserved banner is
        # at y<=30 so the primary nav is roughly y 30..100 depending on
        # layout; y<=110 keeps a safety margin without catching the
        # right-hand panel buttons (which start at y>=550).
        if action_type == "left_click":
            coord = args.get("coordinate") or ()
            if len(coord) >= 2 and coord[1] <= 110:
                is_abort_attempt = True

        if is_abort_attempt:
            self._reserved_loop_count += 1
        # Stuck: current URL matches previous URL and both are this
        # detail page. prev_here is "" on the very first iteration,
        # so no false positive at run start.
        if (run.prev_here
                and urlparse(run.prev_here).path == urlparse(here).path):
            self._reserved_stuck_count += 1

        # A fresh un-launched reservation gets a LOWER stuck threshold:
        # the harness steps in to launch the survey before the vision
        # model fumbles the dialog and drifts toward 'Cancel'
        # (run_20261105_181001 clicked Cancel at step 12; stuck hit 4 at
        # step 8). An orphan/held slot keeps the original 6.
        stuck_limit = 4 if fresh_unlaunched else 6
        if (not self._reserved_loop_nudged
                and not run.pending
                and (self._reserved_loop_count >= 2
                     or self._reserved_stuck_count >= stuck_limit)):
            self._reserved_loop_nudged = True
            launched = False
            if fresh_unlaunched:
                # Deterministic humanised launch grounded on stable
                # data-testid selectors: (Take part ->) Open study in
                # new window -> tick checkbox(es) -> Start study, then
                # follow the survey tab. Real xdotool cursor, not the
                # fragile pixel-click + below-the-fold confirm that lost
                # the study.
                res = core._humanized_launch_reserved_study(include_take_part=True)
                status = (res or {}).get("status")
                launched = status == "launched"
                core._live({"type": "notice",
                            "text": f"[launch] humanised launch: {status}"})
                if launched:
                    self._survey_url = (res or {}).get("survey_url") or self._survey_url
                    run.pending = sites.current.launched_notice()
                    core._notify_telegram("🚀 Survey aperta automaticamente\n" + str(self._survey_url))
            if not launched:
                # Launch not possible (still un-reserved, control
                # missing, or click blocked): fall back to guidance.
                run.pending = core._reservation_nudge(fresh_unlaunched, detail_id)

    def diag(self, run):
        study_timer = self.study_timer
        active = study_timer.is_active()
        d = {
            "guarded": self.here_guarded,
            "prev_guarded": self._prev_was_guarded,  # value BEFORE this step updated it
            "timer_active": active,
            "timer_elapsed": round(study_timer.elapsed(), 1) if active else None,
            "timer_remaining": round(study_timer.remaining(), 1) if active else None,
            "timer_min_sec": study_timer.min_seconds if active else None,
            "last_guarded_duration": self._last_guarded_duration,
        }
        if run.action_type == "left_click" and self.here_guarded:
            d["pre_click_duration"] = self.pre_click_duration
        if self.qc_hit:
            d["qc_flag"] = self.qc_hit
        d["timer_started"] = self._timer_started_this_step
        return d

    # ----------------------------------------------------------- terminate
    def terminate(self, run, answer):
        here = run.here
        refusal = core.terminate_refusal(self.study_timer)
        if refusal:
            return refusal
        # NOCODE emitted while still on the platform without ever visiting
        # the external survey or seeing a cancel/submit confirmation
        # means the study is still "assigned" on the platform. Letting
        # that NOCODE through wedges the next cycle in
        # _wait_for_studies (empty list, slot still held). Narrow
        # conjunctive guard; see guarded_nocode_refusal.
        nocode_block = core.guarded_nocode_refusal(
            answer, here, self._saw_external_this_study,
            self._saw_cancel_confirm, self._nocode_guard_attempts)
        if nocode_block:
            self._nocode_guard_attempts += 1
            return nocode_block
        # The other half of the same mistake, one page later: the
        # survey WAS done, we are back on the platform, and the model
        # gives up with the 'Confirm submission' panel still open and
        # 'I didn't receive a completion code' one click away. The
        # link's presence is proof the study is unsubmitted; press it
        # rather than argue. See _nocode_link_box.
        if (core._is_nocode_answer(answer) and core._on_guarded_site(here)
                and not self._saw_cancel_confirm
                and self._nocode_link_attempts < 2):
            _lbox_seen = core._nocode_link_box()
            _codes_now = core.codes_on_page(core._end_page_text())
            _nc = core.nocode_link_decision(
                answer, here, _lbox_seen is not None, self._saw_cancel_confirm,
                self._seen_codes, _codes_now, self._nocode_link_attempts,
                self._nocode_link_tried)
            if _nc:
                self._nocode_link_attempts += 1
            if _nc == "codes":
                return core.nocode_codes_refusal(self._seen_codes + _codes_now)
            if _nc == "click":
                _lbox = core._scroll_box_into_view(core._nocode_link_box)
                if _lbox and core._humanized_click_box(_lbox):
                    self._nocode_link_tried = True
                    core.cancel.sleep(random.uniform(1.2, 2.2))
                    try:
                        _after = core.pw_bridge.current_url()
                    except Exception:
                        _after = here
                    core._live({"type": "notice", "text":
                                sites.current.nocode_link_live_notice(_lbox.get('text'), _after)})
                    return sites.current.nocode_link_clicked(_lbox.get('text'))
                _nc = "retry"
            if _nc == "retry":
                return sites.current.nocode_link_refusal
        # Ending on the survey host's own thank-you/validation page
        # with the final "submit & return to the platform" control
        # skipped. Before saying anything to the model, try to finish
        # the job: find that control and click it with the real
        # cursor. Prose alone was not enough: run_20261109_102733
        # emitted NOCODE with that control in the middle of the screen.
        _page_codes_now = core.codes_on_page(core._end_page_text())
        if (not self._completion_return_tried
                and not answer.startswith("CODE:")
                and not core._on_guarded_site(here)
                and not self._seen_codes and not _page_codes_now):
            _rbox = core._scroll_box_into_view(core._completion_return_box)
            if _rbox:
                self._completion_return_tried = True
                if core._humanized_click_box(_rbox):
                    core.cancel.sleep(random.uniform(1.2, 2.2))
                    try:
                        _after = core.pw_bridge.current_url()
                    except Exception:
                        _after = here
                    core._live({"type": "notice", "text":
                                "[completion] the study was not submitted: clicked "
                                f"the survey's return control ({_rbox.get('why')}, "
                                f"{_rbox.get('text')!r}) -> {_after}"})
                    return sites.current.completion_return_clicked(_rbox.get('text'), _after)
        # Prose fallback: no such control was found, or clicking it
        # failed. Nudge once.
        nudge = core.completion_pushback(
            answer, here, self._seen_codes,
            _page_codes_now, self._completion_pushed_back)
        if nudge:
            self._completion_pushed_back = True
            return nudge
        return None

    def finish_record(self, run, answer):
        check = None
        if answer.startswith("CODE:") or answer == "NOCODE":
            # Compare what the model reported with every code seen on
            # the study's pages, plus the one the platform shows after the
            # submission ("Completion code: PLKXLQ"). Never overrides
            # the model: it only flags what needs a human look.
            seen = self._seen_codes + [c for c in core.codes_on_page(core._end_page_text())
                                       if c not in self._seen_codes]
            check = core.check_code(answer, seen)
            if check in ("mismatch", "missed"):
                what = ("ha chiuso senza codice" if check == "missed"
                        else f"ha inserito {answer.split(':', 1)[1].strip()}")
                core._notify_telegram(f"⚠️ Codice da verificare: il modello {what}, "
                                      f"ma sulle pagine dello studio c'era "
                                      f"{', '.join(seen[:3])}.\n{run.here}")
        return {"code_source": "model", "code_check": check,
                "codes_seen": self._seen_codes[:5]}

    def finished(self, run, answer):
        if answer.startswith("CODE:") or answer == "NOCODE":
            core._notify_telegram(f"Fara studio completato ✅\n{answer}\n"
                                  f"Ciclo: {run.run_dir.name if run.run_dir else '?'}")
        if self._survey_url:
            core.study_memory.record(self._survey_url,
                                     steps=run.step, outcome=answer[:20])
