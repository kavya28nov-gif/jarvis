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
import logging

import ollama

import jarvis_actions

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


class HeartbeatAgent:
    """on_state_change(state) is called when a background task starts
    ("background_processing") and again when it finishes ("idle") --
    main.py wires this to the orb's state so the rings reflect autonomous
    work. on_state_change may be None to run headless without UI ties."""

    def __init__(self, on_state_change=None, interval=HEARTBEAT_INTERVAL_SECONDS):
        self.on_state_change = on_state_change
        self.interval = interval
        self._stop_flag = False
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

    def _tick(self):
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
