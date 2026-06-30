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
OLLAMA_MODEL = "hermes3"

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

    def _check_nightly_distillation(self):
        """Deterministic -- runs the weekly-events distillation once per
        day at/after 11 PM. Fails silently (per spec) if Groq is
        unreachable; jarvis_memory.distill_memory() handles that and
        just returns False, so this simply retries next night."""
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

    def _tick(self):
        self._check_progress_reminder()
        self._check_monthly_charts()
        self._check_cf_contests()
        self._check_nudges()
        self._check_morning_checkin()
        self._check_nightly_distillation()

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
