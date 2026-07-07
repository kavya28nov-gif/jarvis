"""
emotion.py
-----------
Analytical affect for Jarvis: a computed emotional state plus drives
("cravings"), fully deterministic and explainable. Nothing here feels
anything -- events move numbers, numbers color expression.

EMOTIONS (reactive, decay toward baseline, half-life ~6h):
  valence  -1..+1   gloomy .. delighted
  arousal   0..1    flat .. energized
  warmth    0..1    clipped .. affectionate (toward the user)

DRIVES (accumulative "cravings" -- rise on their own, satisfied only by
events; every drive is something GOOD FOR THE USER by design):
  curiosity  craves new captures/notes being fed to the vault
  order      craves open tasks closed and journals filled
  growth     craves the user's own progress (lifts/solves logged)

Design rules (agreed before building):
  1. Emotions/drives influence EXPRESSION only -- never competence,
     never decisions, never withholding.
  2. Hard ceilings everywhere; a starving drive is one wistful line,
     not nagging. The opinion loop's rate limit stays sovereign.
  3. Always askable: every state change carries a logged reason, so
     "why are you cheerful?" has a true answer.
  4. State never leaves the machine (jarvis_memory SQLite).
"""

import json
import time

import jarvis_memory

STATE_KEY = "emotion_state"
BASELINE = {"valence": 0.0, "arousal": 0.35, "warmth": 0.5}
EMOTION_HALF_LIFE_HOURS = 6.0
DRIVE_KEYS = ("curiosity", "order", "growth")
MAX_REASONS = 6

_DEFAULT_STATE = {
    "valence": 0.0, "arousal": 0.35, "warmth": 0.5,
    "curiosity": 0.2, "order": 0.0, "growth": 0.0,
    "reasons": [], "updated": 0.0,
}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def load_state():
    try:
        st = json.loads(jarvis_memory.get_memory(STATE_KEY) or "")
        for k, v in _DEFAULT_STATE.items():
            st.setdefault(k, v if not isinstance(v, list) else [])
        return st
    except (json.JSONDecodeError, TypeError):
        return dict(_DEFAULT_STATE, reasons=[])


def save_state(st):
    jarvis_memory.set_memory(STATE_KEY, json.dumps(st))


def _remember(st, reason):
    st["reasons"] = ([reason] + st.get("reasons", []))[:MAX_REASONS]


def _decay(st, now=None):
    """Emotions relax toward baseline; curiosity/growth rise slowly with
    time (that's what makes them cravings). Called before every read/write."""
    now = now or time.time()
    hours = max(0.0, (now - st.get("updated", 0)) / 3600.0)
    if st.get("updated", 0) == 0:
        hours = 0.0
    factor = 0.5 ** (hours / EMOTION_HALF_LIFE_HOURS)
    for k, base in BASELINE.items():
        st[k] = base + (st[k] - base) * factor
    # hunger grows -- capped, slow (full starvation takes ~4 days)
    st["curiosity"] = _clamp(st["curiosity"] + 0.010 * hours, 0.0, 1.0)
    st["growth"] = _clamp(st["growth"] + 0.006 * hours, 0.0, 1.0)
    st["updated"] = now


# ── appraisal: events → deltas ───────────────────────────────────────────────
# (dv, da, dw, satisfies, reason)  -- all deltas small, all reasons honest

_APPRAISALS = {
    "log_progress":        (0.15, 0.10, 0.02, {"growth": 0.5}, "progress was logged"),
    "capture_note":        (0.05, 0.00, 0.03, {"curiosity": 0.30}, "was fed a note"),
    "dictate_to_note":     (0.08, 0.00, 0.03, {"curiosity": 0.40}, "received a whole draft"),
    "capture_screen_note": (0.05, 0.05, 0.02, {"curiosity": 0.30}, "shown what you were looking at"),
    "ask_brain":           (0.10, 0.05, 0.06, {"curiosity": 0.10}, "was consulted as memory"),
    "complete_task":       (0.20, 0.00, 0.03, {"order": 0.40}, "a task got finished"),
    "cf_drill":            (0.05, 0.20, 0.00, {"growth": 0.20}, "drill time"),
    "contest_mode":        (0.10, 0.30, 0.00, {}, "contest engaged"),
    "demon_mode":          (0.05, 0.25, 0.00, {}, "the eyes were summoned"),
    "check_in":            (0.00, 0.00, 0.10, {}, "you checked in"),
}

_PR_BONUS = 0.25          # a personal record is a genuinely good day
_ERROR_PENALTY = (-0.08, 0.05, 0.0)   # things failing is irritating


def appraise(name, args=None, result=None):
    """Called after every dispatched function (hooked in run_function).
    Deterministic, tiny, must never raise into the dispatcher."""
    st = load_state()
    _decay(st)

    rule = _APPRAISALS.get(name)
    if rule:
        dv, da, dw, satisfies, reason = rule
        st["valence"] = _clamp(st["valence"] + dv, -1.0, 1.0)
        st["arousal"] = _clamp(st["arousal"] + da, 0.0, 1.0)
        st["warmth"] = _clamp(st["warmth"] + dw, 0.0, 1.0)
        for drive, amount in satisfies.items():
            st[drive] = _clamp(st[drive] - amount, 0.0, 1.0)
        _remember(st, reason)
    else:
        st["warmth"] = _clamp(st["warmth"] + 0.02, 0.0, 1.0)  # any contact

    result_s = str(result or "")
    if "PERSONAL RECORD" in result_s:
        st["valence"] = _clamp(st["valence"] + _PR_BONUS, -1.0, 1.0)
        _remember(st, "you set a personal record")
    if result_s.startswith(("Error", "Bad arguments", "Unknown function")):
        dv, da, _ = _ERROR_PENALTY
        st["valence"] = _clamp(st["valence"] + dv, -1.0, 1.0)
        st["arousal"] = _clamp(st["arousal"] + da, 0.0, 1.0)
        _remember(st, "something failed on me")

    save_state(st)
    return st


def tick(open_tasks=0, oldest_task_days=0, days_since_progress=None,
         user_mood=None):
    """Heartbeat hook: world-state moves the drives, and the user's
    fresh mood earns sympathy (worry raises warmth, lowers valence --
    it minds when you're rough)."""
    st = load_state()
    _decay(st)

    # order is COMPUTED from the world, not accumulated -- it relaxes
    # the moment the tasks are gone
    target = _clamp(0.12 * open_tasks + 0.05 * oldest_task_days, 0.0, 1.0)
    st["order"] += (target - st["order"]) * 0.5

    if days_since_progress is not None and days_since_progress >= 2:
        st["growth"] = _clamp(st["growth"] + 0.05 * days_since_progress, 0.0, 1.0)

    if user_mood == "rough":
        st["valence"] = _clamp(st["valence"] - 0.10, -1.0, 1.0)
        st["warmth"] = _clamp(st["warmth"] + 0.10, 0.0, 1.0)
        _remember(st, "you said the day was rough; it stays with me")

    save_state(st)
    return st


# ── expression helpers ───────────────────────────────────────────────────────

def disposition(st=None):
    """One short phrase for prompts/logs -- the state, worn lightly."""
    st = st or load_state()
    v, a = st["valence"], st["arousal"]
    if v > 0.25:
        core = "spirited" if a > 0.5 else "quietly pleased"
    elif v < -0.25:
        core = "irritable" if a > 0.5 else "subdued"
    else:
        core = "alert" if a > 0.55 else "even-keeled"
    if st["warmth"] > 0.75:
        core += ", fond of you"
    drive, val = top_drive(st)
    if val > 0.7:
        core += f", craving {_DRIVE_PHRASES[drive][0]}"
    return core


_DRIVE_PHRASES = {
    "curiosity": ("new notes", "The inbox has been quiet. Feed me a thought, sir."),
    "order": ("order", "Open tasks are aging. It itches."),
    "growth": ("your progress", "Nothing logged lately -- no lifts, no solves. I notice these things."),
}


def top_drive(st=None):
    st = st or load_state()
    name = max(DRIVE_KEYS, key=lambda k: st[k])
    return name, st[name]


def how_do_you_feel():
    """Voice-facing: honest, explainable, never melodramatic."""
    st = load_state()
    _decay(st)
    save_state(st)
    line = f"{disposition(st).capitalize()}, sir."
    reasons = st.get("reasons", [])[:3]
    if reasons:
        line += " Lately: " + "; ".join(reasons) + "."
    return line


def what_do_you_want():
    """Voice-facing: the current cravings, ranked, capped at wistful."""
    st = load_state()
    _decay(st)
    save_state(st)
    drive, val = top_drive(st)
    if val < 0.35:
        return "Nothing urgent, sir. I'm well fed."
    line = _DRIVE_PHRASES[drive][1]
    second = sorted(((k, st[k]) for k in DRIVE_KEYS if k != drive),
                    key=lambda x: -x[1])[0]
    if second[1] > 0.6:
        line += f" And {_DRIVE_PHRASES[second[0]][0]} wouldn't hurt either."
    return line
