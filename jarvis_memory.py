"""
jarvis_memory.py
------------------
Persistent memory + proactive nudges + mood/energy check-ins.

Three interconnected pieces:
  1. Memory layer -- every dispatched intent gets logged (jarvis_events),
     a nightly Groq distillation summarizes the week (memory_summary),
     and that gets injected into other Groq calls as a [JARVIS MEMORY]
     block (build_memory_block / inject into _call_groq).
  2. Proactive nudges -- deterministic, priority-ordered checks run from
     the heartbeat, one nudge max per tick, each with its own 4h cooldown
     persisted in jarvis_memory (the table, not just in-process state).
  3. Mood/energy check-in -- a scripted 3-question voice flow. This
     module never touches TTS/mic directly (speak_fn/listen_fn are
     injected by main.py) so it stays decoupled and testable.

Deliberately uses lazy imports for jarvis_actions/cf_tracker to avoid a
circular import: jarvis_actions.run_function calls into this module to
log events, so this module can't import jarvis_actions at load time.
"""

import os
import json
import sqlite3
import time
import datetime
import logging

import requests

logger = logging.getLogger("jarvis.memory")

DB_PATH = os.path.expanduser("~/jarvis_memory.db")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_DISTILL_MODEL = "llama-3.3-70b-versatile"

NUDGE_COOLDOWN_SECONDS = 4 * 3600
MEMORY_BLOCK_MAX_CHARS = 1600  # ~400 tokens at a ~4 chars/token heuristic

DISTILL_PROMPT = (
    "You are summarizing a week of Jarvis assistant usage for context "
    "injection. Produce a short memory block (max 300 words) covering: "
    "what the user worked on, CF activity, gym sessions, mood patterns, "
    "recurring requests, and anything notable. Be factual and dense, no "
    "filler."
)


# ---------------------------------------------------------------------------
# DB migration
# ---------------------------------------------------------------------------

def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS jarvis_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER,
        intent_name TEXT,
        entities TEXT,
        outcome TEXT
    );

    CREATE TABLE IF NOT EXISTS jarvis_memory (
        key TEXT PRIMARY KEY,
        value TEXT,
        last_updated INTEGER
    );

    CREATE TABLE IF NOT EXISTS memory_summary (
        date TEXT PRIMARY KEY,
        summary_text TEXT
    );

    CREATE TABLE IF NOT EXISTS mood_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER,
        energy TEXT,
        mood TEXT,
        notes TEXT
    );
    """)
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------------------
# jarvis_memory key/value store
# ---------------------------------------------------------------------------

def set_memory(key, value):
    conn = _get_db()
    conn.execute(
        "INSERT INTO jarvis_memory (key, value, last_updated) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, last_updated=excluded.last_updated",
        (key, str(value), int(time.time())),
    )
    conn.commit()
    conn.close()


def get_memory(key, default=None):
    conn = _get_db()
    row = conn.execute("SELECT value FROM jarvis_memory WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


# ---------------------------------------------------------------------------
# Event logging -- wraps the dispatcher
# ---------------------------------------------------------------------------

def log_event(intent_name, entities, outcome):
    """Called from jarvis_actions.run_function after every dispatch.
    Failures here must never break the actual function call -- caller
    wraps this in try/except."""
    conn = _get_db()
    conn.execute(
        "INSERT INTO jarvis_events (timestamp, intent_name, entities, outcome) VALUES (?, ?, ?, ?)",
        (int(time.time()), intent_name, json.dumps(entities or {}, default=str), str(outcome)[:2000]),
    )
    conn.commit()
    conn.close()


def get_recent_events(n=5):
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM jarvis_events ORDER BY timestamp DESC LIMIT ?", (n,)
    ).fetchall()
    conn.close()
    return rows


def get_events_last_days(days=7):
    cutoff = int(time.time()) - days * 24 * 3600
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM jarvis_events WHERE timestamp >= ? ORDER BY timestamp", (cutoff,)
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Nightly distillation
# ---------------------------------------------------------------------------

def distill_memory():
    """Runs once nightly (triggered by heartbeat_agent at 11 PM). Fails
    silently if Groq is unreachable -- next night just tries again, per
    spec."""
    if not GROQ_API_KEY:
        logger.warning("[memory] GROQ_API_KEY not set -- skipping nightly distillation")
        return False

    events = get_events_last_days(7)
    if not events:
        return False

    raw = json.dumps(
        [{"t": r["timestamp"], "intent": r["intent_name"], "entities": r["entities"], "outcome": r["outcome"]}
         for r in events],
        default=str,
    )[:8000]  # cap input size -- a week of events can get long

    try:
        response = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_DISTILL_MODEL,
                "messages": [
                    {"role": "system", "content": DISTILL_PROMPT},
                    {"role": "user", "content": raw},
                ],
                "max_tokens": 450,
            },
            timeout=30,
        )
        response.raise_for_status()
        summary = response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"[memory] nightly distillation failed: {e}")
        return False

    today = datetime.date.today().isoformat()
    conn = _get_db()
    conn.execute(
        "INSERT INTO memory_summary (date, summary_text) VALUES (?, ?) "
        "ON CONFLICT(date) DO UPDATE SET summary_text=excluded.summary_text",
        (today, summary),
    )
    conn.commit()
    conn.close()
    logger.info(f"[memory] distilled summary stored for {today}")
    return True


def get_fresh_mood(max_age_hours=18):
    """(mood, energy) from the last check-in, or (None, None) if it's
    older than max_age_hours -- stale mood shouldn't color today's
    behavior."""
    try:
        ts = float(get_memory("last_mood_checkin_at") or 0)
    except (TypeError, ValueError):
        return (None, None)
    if time.time() - ts > max_age_hours * 3600:
        return (None, None)
    return (get_memory("last_mood"), get_memory("last_energy"))


def get_summary_for_date(date_iso):
    """Distilled summary text for a date, or None. Used by the vault
    journal mirror -- distilled output only, never raw events."""
    conn = _get_db()
    row = conn.execute(
        "SELECT summary_text FROM memory_summary WHERE date=?", (date_iso,)
    ).fetchone()
    conn.close()
    return row["summary_text"] if row else None


# ---------------------------------------------------------------------------
# Context injection
# ---------------------------------------------------------------------------

def build_memory_block():
    """[JARVIS MEMORY] block prepended to Groq calls -- today's distilled
    summary, last 5 live events, and current key/value state. Truncated
    to stay under ~400 tokens."""
    today = datetime.date.today().isoformat()
    conn = _get_db()
    summary_row = conn.execute(
        "SELECT summary_text FROM memory_summary WHERE date=?", (today,)
    ).fetchone()
    recent = conn.execute(
        "SELECT * FROM jarvis_events ORDER BY timestamp DESC LIMIT 5"
    ).fetchall()
    mem_rows = conn.execute("SELECT * FROM jarvis_memory").fetchall()
    conn.close()

    lines = ["[JARVIS MEMORY]"]
    if summary_row:
        lines.append(f"Today's context: {summary_row['summary_text']}")
    if recent:
        recent_str = "; ".join(r["intent_name"] for r in reversed(recent))
        lines.append(f"Recent actions: {recent_str}")
    if mem_rows:
        state_str = ", ".join(f"{r['key']}={r['value']}" for r in mem_rows)
        lines.append(f"Known state: {state_str}")

    if len(lines) == 1:
        return ""  # nothing to inject yet

    block = "\n".join(lines)
    if len(block) > MEMORY_BLOCK_MAX_CHARS:
        block = block[:MEMORY_BLOCK_MAX_CHARS] + "...[truncated]"
    return block


def inject_memory(messages):
    """Wraps a Groq `messages` list -- prepends the memory block as its
    own system message, ahead of whatever system/user messages the
    caller already built. Never raises; on any failure, returns the
    original messages unchanged so the underlying Groq call still works."""
    try:
        block = build_memory_block()
        if not block:
            return messages
        return [{"role": "system", "content": block}] + list(messages)
    except Exception as e:
        logger.error(f"[memory] injection failed, continuing without it: {e}")
        return messages


# ---------------------------------------------------------------------------
# Proactive nudges
# ---------------------------------------------------------------------------

def _nudge_on_cooldown(nudge_type):
    last = get_memory(f"nudge_cooldown_{nudge_type}")
    if not last:
        return False
    try:
        return (time.time() - float(last)) < NUDGE_COOLDOWN_SECONDS
    except ValueError:
        return False


def _set_nudge_cooldown(nudge_type):
    set_memory(f"nudge_cooldown_{nudge_type}", str(time.time()))


def _has_cf_activity_today():
    """Checks both the auto-tracker and the manual log -- whichever
    source has today's solve counts."""
    try:
        import cf_tracker
        if "No problems solved" not in cf_tracker.cf_today():
            return True
    except Exception:
        pass
    try:
        import jarvis_actions
        return jarvis_actions.has_logged_cf_today()
    except Exception:
        return False


def _next_contest_seconds_until():
    try:
        import cf_tracker
        return cf_tracker.seconds_until_next_contest()
    except Exception:
        return None


def check_nudges(on_notify=None, on_checkin_trigger=None):
    """Called every heartbeat tick. Checks all conditions in priority
    order, fires at most ONE nudge per call (returns as soon as one
    fires), each gated by its own 4h cooldown persisted in jarvis_memory
    so it survives restarts."""
    now = datetime.datetime.now()

    # 1/2: CF contest within 2 hours
    seconds_until = _next_contest_seconds_until()
    if seconds_until is not None and 0 < seconds_until <= 7200:
        hours_left = seconds_until / 3600
        practiced = _has_cf_activity_today()
        nudge_type = "cf_contest_ready" if practiced else "cf_contest_warm"
        if not _nudge_on_cooldown(nudge_type):
            msg = (
                f"Contest in {hours_left:.1f} hours. You're warmed up. Stay sharp."
                if practiced else
                f"Contest in {hours_left:.1f} hours and you haven't practiced today. Quick warm-up?"
            )
            if on_notify:
                on_notify(msg)
            _set_nudge_cooldown(nudge_type)
            return

    # 3: gym after 6 PM on a workout day
    if now.hour >= 18:
        try:
            import jarvis_actions
            day_name = now.strftime("%A")
            is_workout_day = bool(jarvis_actions.WEEKLY_ROUTINE.get(day_name))
            if is_workout_day and not jarvis_actions.has_logged_weights_today():
                if not _nudge_on_cooldown("gym"):
                    if on_notify:
                        on_notify("Haven't hit the gym yet today. Still going?")
                    _set_nudge_cooldown("gym")
                    return
        except Exception as e:
            logger.error(f"[nudge gym check error] {e}")

    # 4: focus session after 8 PM
    if now.hour >= 20:
        last_focus = get_memory("last_focus_session_date")
        if last_focus != now.date().isoformat():
            if not _nudge_on_cooldown("focus"):
                if on_notify:
                    on_notify("No focus session logged today. Even 25 minutes counts.")
                _set_nudge_cooldown("focus")
                return

    # 5: no CF solve after 9 PM
    if now.hour >= 21:
        if not _has_cf_activity_today():
            if not _nudge_on_cooldown("cf_solve"):
                if on_notify:
                    on_notify("No CF solve today. Solve one before bed?")
                _set_nudge_cooldown("cf_solve")
                return

    # 6: mood check-in stale (>24h, or never)
    last_checkin = get_memory("last_mood_checkin_at")
    try:
        stale = (not last_checkin) or (time.time() - float(last_checkin) > 24 * 3600)
    except ValueError:
        stale = True
    if stale and not _nudge_on_cooldown("mood_checkin"):
        _set_nudge_cooldown("mood_checkin")
        if on_checkin_trigger:
            on_checkin_trigger()
        elif on_notify:
            on_notify("Quick check-in: how's energy today?")
        return


# ---------------------------------------------------------------------------
# Mood/energy check-in
# ---------------------------------------------------------------------------

ENERGY_KEYWORDS = {
    "low": ["not great", "kinda tired", "pretty tired", "low", "tired", "exhausted",
            "drained", "sleepy", "rough", "bad"],
    "high": ["high", "great", "energetic", "pumped", "amazing", "good", "fantastic"],
    "medium": ["medium", "okay", "ok", "alright", "fine", "decent", "moderate", "so so", "so-so"],
}

MOOD_KEYWORDS = {
    "rough": ["rough", "bad", "terrible", "awful", "down", "stressed", "not good", "not great"],
    "good": ["pretty good", "good", "great", "happy", "positive", "fantastic"],
    "okay": ["okay", "ok", "alright", "fine", "meh", "so so", "so-so"],
}


def _fuzzy_match(text, keyword_map, default):
    """Checks longer phrases first so e.g. 'not great' (-> low/rough)
    doesn't get matched by the shorter substring 'great' (-> high/good)."""
    text_l = (text or "").lower()
    all_pairs = [(kw, canonical) for canonical, kws in keyword_map.items() for kw in kws]
    for kw, canonical in sorted(all_pairs, key=lambda p: len(p[0]), reverse=True):
        if kw in text_l:
            return canonical
    return default


def _fuzzy_energy(text):
    return _fuzzy_match(text, ENERGY_KEYWORDS, "medium")


def _fuzzy_mood(text):
    return _fuzzy_match(text, MOOD_KEYWORDS, "okay")


def _plan_adjustment(energy, mood):
    if energy == "low" and mood == "rough":
        return "Rest day recommended. Light walk if anything. One easy CF problem max."
    if energy == "low":
        return "Skip gym or do light session. Focus on easy CF problems today."
    if energy == "medium":
        return "Normal plan. Maybe skip the heaviest lift."
    if energy == "high" and mood == "good":
        return "Full plan. Good day to attempt a harder CF problem."
    return "Normal plan."  # high+rough or high+okay -- not in the spec'd table, sensible default


CHECKIN_QUESTIONS = [
    ("energy", "Energy level -- low, medium, or high?"),
    ("mood", "Mood -- rough, okay, or good?"),
    ("notes", "Any injuries or soreness today?"),
]


def run_mood_checkin(speak_fn, listen_fn):
    """Scripted 3-question voice flow. speak_fn/listen_fn are injected
    by the caller (main.py wires self._speak / voice_input.listen) so
    this module has no direct TTS/mic dependency. Blocking -- caller is
    responsible for running this off the main Tk thread and guarding
    against overlap with a normal voice command (main.py uses _busy)."""
    answers = {}
    for key, question in CHECKIN_QUESTIONS:
        speak_fn(question)
        raw = listen_fn() or ""
        answers[key] = raw

    energy = _fuzzy_energy(answers["energy"])
    mood = _fuzzy_mood(answers["mood"])
    notes = answers["notes"]

    conn = _get_db()
    conn.execute(
        "INSERT INTO mood_log (timestamp, energy, mood, notes) VALUES (?, ?, ?, ?)",
        (int(time.time()), energy, mood, notes),
    )
    conn.commit()
    conn.close()

    set_memory("last_mood", mood)
    set_memory("last_energy", energy)
    set_memory("last_mood_checkin_at", str(time.time()))

    plan = _plan_adjustment(energy, mood)
    set_memory("today_plan_adjustment", plan)
    speak_fn(plan)

    return f"Check-in logged: energy={energy}, mood={mood}. Plan: {plan}"


def get_mood_summary_for_month(month, year):
    """Feeds into the Hermes monthly payload under a 'mood' key."""
    month_start = int(datetime.datetime(year, month, 1).timestamp())
    next_month = datetime.datetime(year + 1, 1, 1) if month == 12 else datetime.datetime(year, month + 1, 1)
    month_end = int(next_month.timestamp())

    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM mood_log WHERE timestamp >= ? AND timestamp < ?", (month_start, month_end)
    ).fetchall()
    conn.close()

    if not rows:
        return {"checkins": 0, "avg_energy": None, "rough_days": 0}

    energy_score = {"low": 1, "medium": 2, "high": 3}
    scores = [energy_score.get(r["energy"], 2) for r in rows]
    avg_energy = sum(scores) / len(scores)
    rough_days = sum(1 for r in rows if r["mood"] == "rough")
    return {"checkins": len(rows), "avg_energy": round(avg_energy, 2), "rough_days": rough_days}


# ---------------------------------------------------------------------------
# 4. Manifest-dispatchable intents
# ---------------------------------------------------------------------------

def check_in():
    """The real interactive flow is special-cased in main.py's
    _talk_flow (detected by function name before normal dispatch),
    since it needs live speak+listen, not a single string return. This
    is just the fallback for callers without a mic (e.g. phone remote)."""
    return "Check-in needs voice -- say 'Jarvis, check in' through the mic."


def memory_summary():
    today = datetime.date.today().isoformat()
    conn = _get_db()
    row = conn.execute("SELECT summary_text FROM memory_summary WHERE date=?", (today,)).fetchone()
    conn.close()
    if not row:
        return "No memory summary for today yet -- it's generated nightly at 11 PM."
    return row["summary_text"]


def whats_my_plan():
    plan = get_memory("today_plan_adjustment") or "No check-in done yet today, so no plan adjustment."

    try:
        import cf_tracker
        contest = cf_tracker.cf_upcoming_contest()
    except Exception:
        contest = ""

    try:
        import jarvis_actions
        day_name = datetime.date.today().strftime("%A")
        has_lifts = bool(jarvis_actions.WEEKLY_ROUTINE.get(day_name))
        workout_str = f"Today ({day_name}) is a workout day." if has_lifts else f"Today ({day_name}) has no fixed lift day."
    except Exception:
        workout_str = ""

    return " ".join(p for p in [plan, workout_str, contest] if p)


def last_time(topic=None):
    if not topic:
        return "Which topic? e.g. 'last time I asked about Spotify'."
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM jarvis_events WHERE intent_name LIKE ? OR entities LIKE ? ORDER BY timestamp DESC LIMIT 1",
        (f"%{topic}%", f"%{topic}%"),
    ).fetchone()
    conn.close()
    if not row:
        return f"No record of anything related to '{topic}'."
    when = datetime.datetime.fromtimestamp(row["timestamp"]).strftime("%Y-%m-%d %H:%M")
    return f"Last time related to '{topic}': {when} -- {row['intent_name']}."
