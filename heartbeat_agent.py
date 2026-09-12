"""
heartbeat_agent.py
--------------------
Autonomous background loop. Wakes up every HEARTBEAT_INTERVAL_SECONDS,
reads TASK_BOARD.md, and asks the local Ollama 'hermes3' model whether
any background automation task is due right now. Runs entirely on its
own daemon thread -- completely independent of the wake-word listener
and the voice talk-flow in main.py.
"""

import os
import json
import threading
import time
import datetime
import calendar
import logging

import ollama

import jarvis_actions
import cf_tracker
import jarvis_memory

try:
    import winsound
except ImportError:
    winsound = None


def _play_alert_sound():
    """Plain system beep for the T-15 contest warning -- no new UI, no
    extra audio assets, just winsound (stdlib on Windows)."""
    if winsound:
        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass

logger = logging.getLogger("jarvis.heartbeat")

OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:8b"

HEARTBEAT_INTERVAL_SECONDS = 30 * 60  # configurable: 15 or 30 minutes

TASK_BOARD_PATH = os.path.join(os.path.dirname(__file__), "TASK_BOARD.md")

_TASK_BOARD_TEMPLATE = """# Jarvis Task Board

List background automation routines or state checks you want Jarvis to
consider on every heartbeat. Plain language is fine -- the model reads
this file and decides if anything is due.

Examples (uncomment / edit to activate):
<!-- - Every day at 9 PM, remind me to back up the Documents folder. -->
<!-- - If notepad.exe has crashed/closed unexpectedly, relaunch it. -->
"""

SYSTEM_PROMPT = (
    "You are Jarvis's autonomous background scheduler. You will be given "
    "the current time, system status, and a task board written in plain "
    "language. Decide whether any task is due to run RIGHT NOW. "
    "Respond with raw JSON only, no markdown fences, no commentary, in "
    "this exact schema: "
    '{"task_due": true|false, "summary": "<short description or null>"}'
)


OPINION_PROMPT = (
    "You are JARVIS from Iron Man -- dry, loyal, quietly witty -- running "
    "as a background presence on the user's PC. Every 30 minutes you get "
    "a snapshot of their day: time, gym/run/Codeforces activity, mood, "
    "recent commands, system state. Decide if you have ONE remark that is "
    "genuinely worth interrupting them for.\n\n"
    "The bar is HIGH. Speak only for cross-signal observations a good "
    "friend would notice: a worrying combination (late night + contest "
    "tomorrow + poor sleep reported), a trend (lifts declining two weeks "
    "running), something earned (long solve streak, big day), or "
    "something genuinely off. NEVER speak for: routine states, generic "
    "encouragement, restating a single fact they already know, anything "
    "another reminder already covers (workout-logging reminders and "
    "contest T-60/T-15 alerts already exist -- do not duplicate them). "
    "When in doubt, stay silent. Most snapshots deserve silence.\n\n"
    "If you do speak: 1-2 sentences, plain text (it is read aloud), "
    "specific to the data, in character -- observant, a little wry, "
    "never preachy, 'sir' is optional.\n\n"
    "You may also receive VAULT MEMORY -- excerpts retrieved from the "
    "user's own notes (goals, past decisions, journal history). Use it "
    "for longer-arc observations a day snapshot can't see ('this is the "
    "third week bench has slipped', 'you wrote that finished means "
    "public'). Only claim patterns the notes actually support.\n\n"
    "You may receive YOUR OWN STATE (disposition + a craving). Let the "
    "disposition color your tone. If a craving is high you may voice it "
    "once, briefly and wistfully ('the inbox has been quiet, sir') -- "
    "NEVER as guilt, leverage, or a condition for helping.\n\n"
    "Respond with raw JSON only, exactly: "
    '{"speak": true|false, "remark": "<the remark, or null>"}'
)

# At most one unprompted remark per this many seconds, persisted across
# restarts via jarvis_memory -- the rate limit is what keeps this
# charming instead of Clippy.
OPINION_MIN_GAP_SECONDS = 3 * 3600
OPINION_QUIET_START = 23   # no unprompted remarks 11 PM..
OPINION_QUIET_END = 9      # ..through 9 AM


def _ensure_task_board():
    if not os.path.exists(TASK_BOARD_PATH):
        with open(TASK_BOARD_PATH, "w", encoding="utf-8") as f:
            f.write(_TASK_BOARD_TEMPLATE)


PROGRESS_REMINDER_START_HOUR = 21  # 9 PM
PROGRESS_REMINDER_END_HOUR = 24    # midnight


class HeartbeatAgent:
    """on_state_change(state) is called when a background task starts
    ("background_processing") and again when it finishes ("idle") --
    main.py wires this to the orb's state so the rings reflect autonomous
    work. on_state_change may be None to run headless without UI ties.

    on_notify(text) is called for things that should actually be spoken
    out loud (e.g. the daily progress-log reminder), as opposed to silent
    background task summaries.

    on_checkin_trigger() is called to run the full interactive mood
    check-in flow (3 spoken questions + mic listening) -- this needs
    main.py's real speak/listen capability, so the heartbeat just fires
    the callback rather than running the flow itself."""

    def __init__(self, on_state_change=None, on_notify=None, on_checkin_trigger=None,
                 interval=HEARTBEAT_INTERVAL_SECONDS):
        self.on_state_change = on_state_change
        self.on_notify = on_notify
        self.on_checkin_trigger = on_checkin_trigger
        self.interval = interval
        self._stop_flag = False
        self._last_progress_reminder_key = None
        self._last_monthly_chart_date = None
        self._last_morning_checkin_date = None
        self._last_distillation_date = None
        self._client = ollama.Client(host=OLLAMA_HOST)
        _ensure_task_board()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop_flag = True

    def _loop(self):
        while not self._stop_flag:
            time.sleep(self.interval)
            if self._stop_flag:
                break
            try:
                self._tick()
            except Exception as e:
                logger.error(f"[heartbeat error] {e}")

    def _set_state(self, state):
        if self.on_state_change:
            try:
                self.on_state_change(state)
            except Exception as e:
                logger.error(f"[heartbeat UI callback error] {e}")

    def _check_progress_reminder(self):
        """Deterministic, not LLM-decided -- whether you've logged today's
        weights is a plain fact-check, not something worth risking a
        model misjudging. Reminds at most once per hour, within the
        9 PM-midnight window, and goes silent once logged. CF is no
        longer part of this -- it's auto-tracked via cf_tracker now
        (daily fetch + heartbeat contest monitor), so there's nothing
        left to manually log; the CF-specific "go solve one" nudge in
        jarvis_memory.check_nudges covers that instead."""
        now = datetime.datetime.now()
        if not (PROGRESS_REMINDER_START_HOUR <= now.hour < PROGRESS_REMINDER_END_HOUR):
            return
        hour_key = f"{now.date().isoformat()}-{now.hour}"
        if self._last_progress_reminder_key == hour_key:
            return

        missing = []
        if not jarvis_actions.has_logged_weights_today():
            missing.append("today's weights")
        if not missing:
            return

        self._last_progress_reminder_key = hour_key
        if self.on_notify:
            try:
                self.on_notify(f"Sir, you still haven't logged {' and '.join(missing)}.")
            except Exception as e:
                logger.error(f"[heartbeat notify error] {e}")

    def _check_monthly_charts(self):
        """Deterministic, not LLM-decided -- generates this month's
        progress charts once, on the last calendar day of the month.
        Guarded so it only fires once per day even though the heartbeat
        ticks every 30 min."""
        today = datetime.date.today()
        last_day_of_month = calendar.monthrange(today.year, today.month)[1]
        if today.day != last_day_of_month:
            return
        if self._last_monthly_chart_date == today.isoformat():
            return

        self._last_monthly_chart_date = today.isoformat()
        logger.info(f"[heartbeat] generating monthly progress charts for {today.year}-{today.month:02d}")
        self._set_state("background_processing")
        try:
            result = jarvis_actions.generate_progress_charts(month=today.month, year=today.year)
            logger.info(f"[heartbeat] {result}")
        except Exception as e:
            logger.error(f"[heartbeat monthly chart error] {e}")
        finally:
            self._set_state("idle")

    def _check_cf_contests(self):
        """Contest monitor -- T-60/T-15 reminders, auto-open at start,
        post-contest analysis. One contest.list call per tick covers all
        three (see cf_tracker.run_heartbeat_check), staying well within
        CF's rate limit. Fails silently (logged, not raised) on any API
        hiccup -- next tick just tries again."""
        try:
            cf_tracker.run_heartbeat_check(on_notify=self.on_notify, play_alert=_play_alert_sound)
        except Exception as e:
            logger.error(f"[heartbeat cf_tracker error] {e}")

    def _check_nudges(self):
        """Proactive nudges -- priority-ordered, one max per tick, each
        gated by its own 4h cooldown persisted in jarvis_memory (survives
        restarts). See jarvis_memory.check_nudges for the actual logic."""
        try:
            jarvis_memory.check_nudges(on_notify=self.on_notify, on_checkin_trigger=self.on_checkin_trigger)
        except Exception as e:
            logger.error(f"[heartbeat nudge error] {e}")

    def _check_morning_checkin(self):
        """Deterministic -- auto-triggers the mood check-in flow once per
        day at 9 AM."""
        now = datetime.datetime.now()
        if now.hour != 9:
            return
        today = now.date().isoformat()
        if self._last_morning_checkin_date == today:
            return
        self._last_morning_checkin_date = today
        if self.on_checkin_trigger:
            try:
                self.on_checkin_trigger()
            except Exception as e:
                logger.error(f"[heartbeat morning checkin error] {e}")

    # ── opinionated proactivity ──────────────────────────────────────────────

    def _build_snapshot(self):
        """Gathers the day's cross-signal context into one text block for
        the opinion check. Each field degrades to omission on failure.
        Deliberately excludes command args/results beyond intent names --
        the snapshot goes to the local model only, but there's no reason
        to move private content around at all."""
        now = datetime.datetime.now()
        lines = [f"Time: {now.strftime('%A %H:%M')}"]

        try:
            # read-only peek at today's entry (log_progress() would write
            # an empty entry as a side effect)
            today_entry = jarvis_actions._load_progress_log().get(
                datetime.date.today().isoformat(), {})
            if today_entry:
                lines.append("Today's log: " + json.dumps(today_entry))
            else:
                lines.append("Today's log: nothing logged yet")
        except Exception:
            pass
        try:
            lines.append("7-day trends:\n" + jarvis_actions.show_progress(7))
        except Exception:
            pass
        try:
            lines.append(cf_tracker.cf_rating())
            lines.append(cf_tracker.cf_today())
            lines.append(cf_tracker.cf_upcoming_contest())
        except Exception:
            pass
        try:
            mood = jarvis_memory.get_memory("last_mood")
            energy = jarvis_memory.get_memory("last_energy")
            if mood or energy:
                lines.append(f"Last check-in: mood={mood}, energy={energy}")
            plan = jarvis_memory.get_memory("today_plan_adjustment")
            if plan:
                lines.append(f"Today's plan adjustment: {plan}")
        except Exception:
            pass
        try:
            focus_date = jarvis_memory.get_memory("last_focus_session_date")
            if focus_date:
                lines.append(f"Last focus session: {focus_date}")
        except Exception:
            pass
        try:
            events = jarvis_memory.get_recent_events(12)
            if events:
                names = ", ".join(
                    f"{e['intent_name']}@{datetime.datetime.fromtimestamp(e['timestamp']).strftime('%H:%M')}"
                    for e in events
                )
                lines.append(f"Recent commands (name@time): {names}")
        except Exception:
            pass

        return "\n".join(lines)

    def _build_vault_memory(self):
        """Retrieves a few vault excerpts relevant to long-arc advising
        (goals, decisions, mood/energy patterns, trends) via the local
        BM25 searcher -- gives the opinion loop memory of the user
        beyond today's snapshot. All local; empty string on any failure."""
        try:
            root = jarvis_actions._vault_root()
            if root is None:
                return ""
            import vault_search
            seen, lines = set(), []
            for q in ("goals plans decided",
                      "mood energy pattern",
                      "journal streak progress trend"):
                for hit in vault_search.search(root, q, top_k=2):
                    if hit["page_path"] in seen:
                        continue
                    seen.add(hit["page_path"])
                    lines.append(f"[{hit['page_path']}] {hit['snippet'][:280]}")
            return "\n".join(lines[:5])
        except Exception as e:
            logger.error(f"[opinion vault memory error] {e}")
            return ""

    def _check_opinion(self):
        """GLaDOS-style opinion loop: hand the day's snapshot to the local
        model and ask if it has ONE remark genuinely worth making.
        Rate-limited hard (3h gap, quiet hours) and fails silently -- a
        skipped opinion costs nothing."""
        now = datetime.datetime.now()
        if now.hour >= OPINION_QUIET_START or now.hour < OPINION_QUIET_END:
            return
        try:
            last = float(jarvis_memory.get_memory("opinion_last_spoken_at") or 0)
        except (TypeError, ValueError):
            last = 0
        if time.time() - last < OPINION_MIN_GAP_SECONDS:
            return

        snapshot = self._build_snapshot()
        memory = self._build_vault_memory()
        user_content = f"Snapshot:\n{snapshot}"
        if memory:
            user_content += f"\n\nVAULT MEMORY (user's own notes):\n{memory}"
        try:
            import emotion
            st = emotion.load_state()
            drive, val = emotion.top_drive(st)
            state_line = f"disposition: {emotion.disposition(st)}"
            if val > 0.7:
                state_line += f"; craving: {drive} ({val:.1f})"
            user_content += f"\n\nYOUR OWN STATE: {state_line}"
        except Exception:
            pass
        try:
            response = self._client.chat(
                model=OLLAMA_MODEL,
                messages=[
                    {"role": "system", "content": OPINION_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                format="json",
                think=False,  # qwen3 burns its whole output on hidden reasoning otherwise
                options={"temperature": 0.7},
            )
            decision = self._parse_decision(response["message"]["content"])
        except Exception as e:
            logger.error(f"[opinion check error] {e}")
            return

        remark = (decision.get("remark") or "").strip()
        if decision.get("speak") and remark and remark.lower() != "null":
            logger.info(f"[opinion] {remark}")
            jarvis_memory.set_memory("opinion_last_spoken_at", str(time.time()))
            if self.on_notify:
                try:
                    self.on_notify(remark)
                except Exception as e:
                    logger.error(f"[opinion notify error] {e}")

    def _check_weekly_review(self):
        """Sunday evening (>=18h): writes journal/week-YYYY-Wnn.md -- a
        week-in-review over the local data (goals adherence, CF trend,
        workout adherence, mood arc, aging open tasks), with an optional
        one-paragraph verdict from the local model. Once per ISO week,
        guarded via jarvis_memory so restarts don't duplicate it. All
        data and inference local."""
        now = datetime.datetime.now()
        if now.weekday() != 6 or now.hour < 18:   # Sunday evening only
            return
        iso_year, iso_week, _ = now.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        if jarvis_memory.get_memory("last_weekly_review") == week_key:
            return
        journal = jarvis_actions._vault_subdir("journal")
        if journal is None:
            return
        jarvis_memory.set_memory("last_weekly_review", week_key)

        today = datetime.date.today()
        week_dates = [(today - datetime.timedelta(days=i)).isoformat() for i in range(7)]

        # workout adherence vs the 4x/week goal
        data = {}
        try:
            data = jarvis_actions._load_progress_log()
        except Exception:
            pass
        workout_days = sum(
            1 for d in week_dates
            if data.get(d, {}).get("exercises") or "distance" in data.get(d, {})
        )
        weighins = [(d, data[d]["bodyweight"]) for d in reversed(week_dates)
                    if "bodyweight" in data.get(d, {})]

        # CF: solves this week + rating
        cf_lines = []
        try:
            conn = cf_tracker._get_db()
            week_start = int(datetime.datetime.combine(
                today - datetime.timedelta(days=6), datetime.time.min).timestamp())
            solved = conn.execute(
                "SELECT COUNT(*) FROM cf_submissions WHERE solved_at >= ?",
                (week_start,)).fetchone()[0]
            conn.close()
            cf_lines.append(f"Problems solved this week: {solved}")
            cf_lines.append(cf_tracker.cf_rating())
        except Exception:
            pass

        # mood arc from the mood log
        mood_lines = []
        try:
            conn = jarvis_memory._get_db()
            rows = conn.execute(
                "SELECT timestamp, energy, mood FROM mood_log WHERE timestamp >= ? ORDER BY timestamp",
                (int(datetime.datetime.combine(today - datetime.timedelta(days=6),
                                               datetime.time.min).timestamp()),)).fetchall()
            conn.close()
            for r in rows:
                d = datetime.datetime.fromtimestamp(r["timestamp"]).strftime("%a")
                mood_lines.append(f"{d}: mood={r['mood']}, energy={r['energy']}")
        except Exception:
            pass

        # aging open tasks
        task_lines = []
        try:
            for path, _, t in jarvis_actions._iter_open_tasks():
                task_lines.append(f"{jarvis_actions._task_display(t)} (from {path.stem})")
        except Exception:
            pass

        goal_line = (f"Workout days: {workout_days}/7 (goal: 4) -- "
                     + ("ON TRACK" if workout_days >= 4 else "BEHIND"))
        weigh_line = (" -> ".join(f"{w}kg" for _, w in weighins)
                      if weighins else "no weigh-ins logged")

        facts = "\n".join(filter(None, [
            goal_line,
            f"Bodyweight: {weigh_line}",
            *cf_lines,
            "Mood: " + ("; ".join(mood_lines) if mood_lines else "no check-ins"),
            f"Open tasks: {len(task_lines)}",
        ]))

        verdict = ""
        try:
            response = self._client.chat(
                model=OLLAMA_MODEL,
                messages=[
                    {"role": "system", "content":
                        "You are JARVIS writing a 3-4 sentence week-in-review "
                        "verdict from the facts given. Direct, specific, dry -- "
                        "call out what was strong and what slipped vs the "
                        "4-workouts/week and daily-CF goals. Plain text."},
                    {"role": "user", "content": facts},
                ],
                think=False,
                options={"temperature": 0.5},
            )
            verdict = response["message"]["content"].strip()
        except Exception as e:
            logger.error(f"[weekly review verdict error] {e}")

        content = (
            f"---\ndate: {today.isoformat()}\ntype: weekly-review\nweek: {week_key}\n---\n\n"
            f"# Week in Review — {week_key}\n\n"
            f"## Adherence\n\n- {goal_line}\n- Bodyweight: {weigh_line}\n\n"
            "## Codeforces\n\n" + ("".join(f"- {l}\n" for l in cf_lines) or "- (no data)\n") + "\n"
            "## Mood arc\n\n" + ("".join(f"- {l}\n" for l in mood_lines) or "- (no check-ins)\n") + "\n"
            f"## Open tasks ({len(task_lines)})\n\n"
            + ("".join(f"- [ ] {l}\n" for l in task_lines[:10]) or "- none\n") + "\n"
            + (f"## Verdict\n\n{verdict}\n" if verdict else "")
        )
        (journal / f"week-{week_key}.md").write_text(content, encoding="utf-8")
        logger.info(f"[weekly review] wrote journal/week-{week_key}.md")
        if self.on_notify:
            try:
                self.on_notify("Your week in review is written, sir. "
                               + (f"{workout_days} workout day(s) this week."))
            except Exception:
                pass

    def _check_nightly_distillation(self):
        """Deterministic -- runs the weekly-events distillation once per
        day at/after 11 PM, then mirrors a journal page into the Obsidian
        vault. Fails silently (per spec) if Groq is unreachable;
        jarvis_memory.distill_memory() handles that and just returns
        False, so this simply retries next night."""
        now = datetime.datetime.now()
        if now.hour < 23:
            return
        today = now.date().isoformat()
        if self._last_distillation_date == today:
            return
        self._last_distillation_date = today
        try:
            jarvis_memory.distill_memory()
        except Exception as e:
            logger.error(f"[heartbeat distillation error] {e}")
        self._write_vault_journal()
        self._check_contradictions()
        self._check_roast()

    def _check_roast(self):
        """Roast mode, opt-in via roast_mode in jarvis_config.json. Once a
        night, piggybacking on the 11 PM distillation guard, Jarvis reads
        the same local-only day snapshot the opinion loop uses and delivers
        one roast about the day's behavior. Generation lives in
        jarvis_actions._generate_roast, shared with the on-demand 'roast
        me' voice command; failures are silent -- a missed roast is not a
        problem worth an error sound."""
        if not jarvis_actions._USER_CONFIG.get("roast_mode", False):
            return
        self._set_state("background_processing")
        try:
            roast = jarvis_actions._generate_roast(self._build_snapshot())
        except Exception as e:
            logger.error(f"[roast error] {e}")
            roast = None
        finally:
            self._set_state("idle")
        if not roast:
            return
        logger.info(f"[roast] {roast}")
        if self.on_notify:
            try:
                self.on_notify(f"Tonight's roast, sir: {roast}")
            except Exception as e:
                logger.error(f"[roast notify error] {e}")

    def _check_contradictions(self):
        """Devil's advocate pass, opt-in via contradiction_checks in
        jarvis_config.json. Piggybacks on the nightly 11 PM distillation
        (already once-a-day guarded) rather than owning a timer. Reads
        the vault, never writes it: retrieval + local qwen3 live in
        jarvis_actions._find_contradictions, shared with the on-demand
        voice command. Findings arrive via on_notify like _check_opinion
        -- an observation, not a verdict -- and each tension surfaces at
        most once ever (snippet-hash set persisted in jarvis_memory)."""
        if not jarvis_actions._USER_CONFIG.get("contradiction_checks", False):
            return
        self._set_state("background_processing")
        try:
            findings = jarvis_actions._find_contradictions()
        except Exception as e:
            logger.error(f"[contradiction check error] {e}")
            findings = None
        finally:
            self._set_state("idle")
        if not findings:
            return
        remark = " ".join(findings)
        logger.info(f"[contradictions] {remark}")
        if self.on_notify:
            try:
                self.on_notify(remark)
            except Exception as e:
                logger.error(f"[contradiction notify error] {e}")

    def _write_vault_journal(self):
        """Mirrors the day into <vault>/journal/YYYY-MM-DD.md. Content is
        distilled/derived only (summary text, CF public stats, mood
        words, streaks) -- raw events never reach the vault. Idempotent
        by full rewrite: this file is Jarvis-owned."""
        try:
            journal = jarvis_actions._vault_subdir("journal")
            if journal is None:
                logger.info("[vault journal] vault not configured -- skipping")
                return
            today = datetime.date.today().isoformat()

            summary = None
            try:
                summary = jarvis_memory.get_summary_for_date(today)
            except Exception:
                pass

            cf_lines = []
            for fn in (cf_tracker.cf_rating, cf_tracker.cf_today, cf_tracker.cf_last_contest):
                try:
                    cf_lines.append(fn())
                except Exception:
                    pass

            mood_lines = []
            try:
                mood = jarvis_memory.get_memory("last_mood")
                energy = jarvis_memory.get_memory("last_energy")
                if mood or energy:
                    mood_lines.append(f"Last check-in: mood={mood}, energy={energy}")
                plan = jarvis_memory.get_memory("today_plan_adjustment")
                if plan:
                    mood_lines.append(f"Plan adjustment: {plan}")
            except Exception:
                pass

            streak_lines = []
            try:
                streak_lines.append(jarvis_actions.show_streaks())
            except Exception:
                pass
            try:
                entry = jarvis_actions._load_progress_log().get(today, {})
                if entry.get("exercises"):
                    streak_lines.append(f"Lifts logged today: {len(entry['exercises'])}")
                if "distance" in entry:
                    streak_lines.append(f"Run: {entry['distance']} km")
                if "bodyweight" in entry:
                    streak_lines.append(f"Bodyweight: {entry['bodyweight']} kg")
            except Exception:
                pass

            def section(title, lines):
                body = "\n".join(f"- {l}" for l in lines) if lines else "- (nothing recorded)"
                return f"## {title}\n\n{body}\n"

            content = (
                f"---\ndate: {today}\ntype: jarvis-journal\n---\n\n"
                f"# Jarvis Journal — {today}\n\n"
                f"## Day Summary\n\n{summary or '(no distillation available)'}\n\n"
                + section("Codeforces", cf_lines) + "\n"
                + section("Mood", mood_lines) + "\n"
                + section("Streaks / PRs", streak_lines)
            )
            (journal / f"{today}.md").write_text(content, encoding="utf-8")
            logger.info(f"[vault journal] wrote journal/{today}.md")
        except Exception as e:
            logger.error(f"[vault journal error] {e}")

    def _refresh_vault_index(self):
        """Keeps the local vault search index fresh for ask_brain --
        builds on first run, refreshes when >24h old, no-op otherwise.
        Runs on the heartbeat thread, never the voice path."""
        try:
            root = jarvis_actions._vault_root()
            if root is None:
                return
            import vault_search
            vault_search.ensure_index(root)
        except Exception as e:
            logger.error(f"[vault index error] {e}")

    def _tick_emotion(self):
        """Feeds world state into the affect layer's drives -- open task
        pressure (order), days without logged progress (growth), and
        sympathy for a fresh rough mood. Expression-only downstream."""
        try:
            import emotion
            open_tasks, oldest_days = 0, 0
            try:
                today = datetime.date.today()
                for path, _, _ in jarvis_actions._iter_open_tasks():
                    open_tasks += 1
                    try:
                        d = datetime.date.fromisoformat(path.stem[:10])
                        oldest_days = max(oldest_days, (today - d).days)
                    except ValueError:
                        pass
            except Exception:
                pass
            days_since = None
            try:
                data = jarvis_actions._load_progress_log()
                if data:
                    last = max(datetime.date.fromisoformat(d) for d in data)
                    days_since = (datetime.date.today() - last).days
            except Exception:
                pass
            mood, _ = jarvis_memory.get_fresh_mood()
            emotion.tick(open_tasks=open_tasks, oldest_task_days=oldest_days,
                         days_since_progress=days_since, user_mood=mood)
        except Exception as e:
            logger.error(f"[emotion tick error] {e}")

    def _tick(self):
        self._tick_emotion()
        self._refresh_vault_index()
        self._check_progress_reminder()
        self._check_monthly_charts()
        self._check_cf_contests()
        self._check_nudges()
        self._check_morning_checkin()
        self._check_nightly_distillation()
        self._check_weekly_review()
        self._check_opinion()

        with open(TASK_BOARD_PATH, "r", encoding="utf-8") as f:
            board = f.read()

        now = datetime.datetime.now().isoformat()
        status = jarvis_actions.system_status()
        user_prompt = (
            f"Current time: {now}\n\n"
            f"System status:\n{status}\n\n"
            f"Task board:\n{board}\n\n"
            "Based on the current time, system status, and this task list, "
            "does any background automation task need to be executed right now?"
        )

        response = self._client.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            format="json",
            think=False,  # qwen3: hidden reasoning starves the JSON output
            options={"temperature": 0.1},
        )
        decision = self._parse_decision(response["message"]["content"])

        if decision.get("task_due"):
            summary = decision.get("summary") or "background task"
            logger.info(f"[heartbeat] running due task: {summary}")
            self._set_state("background_processing")
            try:
                self._execute(summary)
            finally:
                self._set_state("idle")

    @staticmethod
    def _parse_decision(raw_text):
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text[:-3]
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            logger.error(f"[heartbeat] could not parse decision JSON:\n{raw_text}")
            return {"task_due": False, "summary": None}

    def _execute(self, summary):
        """Hook point for actual sub-routines (backups, web checks, crash
        recovery, etc). Currently logs the decision -- wire specific
        jarvis_actions functions in here as you add real tasks to
        TASK_BOARD.md."""
        logger.info(f"[heartbeat] task summary: {summary}")
