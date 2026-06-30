"""
easter_eggs.py
----------------
Special intent triggers with custom TTS dialogue, orb animations, and
(where named explicitly) real system actions.

Two layers per egg, same pattern as jarvis_memory.check_in:
  - A public, zero-arg `easter_xxx()` function registered in
    jarvis_actions' manifest/dispatch -- this is the safe fallback used
    by callers with no mic/orb (e.g. the phone remote). It just returns
    a short string, no theatrics.
  - A private `_run_xxx(speak_fn, orb)` handler with the real sequence,
    invoked directly by main.py's _talk_flow (which intercepts these
    function names before normal dispatch, same as check_in), since the
    real experience needs live TTS + orb control, not a single string
    return.

EASTER_EGG_HANDLERS maps manifest names -> the real handlers, and
run_easter_egg() is the entry point main.py calls.

Design choices flagged explicitly rather than silently implemented:
  - "db.get_random_unsolved_problem()" has no real backing data --
    nothing in this codebase tracks *unsolved* problems, only accepted
    ones. Falls back to opening the general CF problemset instead of
    fabricating a fake "unsolved problem".
  - "clean_downloads_junk()" (silently deleting files from a hidden
    voice trigger) is NOT implemented as a deletion. It calls the
    existing organize_downloads() instead (moves into subfolders, never
    deletes), since auto-deleting files from an easter egg is a real
    destructive-action risk, not a joke-feature decision to make
    silently.
"""

import os
import time
import datetime
import logging
import webbrowser
import subprocess

try:
    import winsound
except ImportError:
    winsound = None

logger = logging.getLogger("jarvis.easter_eggs")

SOUNDS_DIR = os.path.join(os.path.dirname(__file__), "assets", "sounds")


# ---------------------------------------------------------------------------
# Shared helpers -- safe sound playback, real data pulls (never crash)
# ---------------------------------------------------------------------------

def _play_sound(filename):
    """Skips silently if winsound is unavailable or the file is missing
    -- per spec, a missing sound file must never crash an easter egg."""
    if not winsound:
        return
    path = os.path.join(SOUNDS_DIR, filename)
    if not os.path.exists(path):
        return
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as e:
        logger.error(f"[easter egg] sound playback failed for {filename}: {e}")


def _get_total_event_count():
    try:
        import jarvis_memory
        conn = jarvis_memory._get_db()
        n = conn.execute("SELECT COUNT(*) FROM jarvis_events").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def _get_latest_cf_rating():
    try:
        import cf_tracker
        conn = cf_tracker._get_db()
        row = conn.execute(
            "SELECT rating FROM cf_rating_history ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row["rating"] if row else None
    except Exception:
        return None

def _get_late_night_session_count():
    """Sessions (logged events) that happened between midnight and 5am --
    backs the "the ones at 1AM" line with a real number instead of a
    made-up one."""
    try:
        import jarvis_memory
        conn = jarvis_memory._get_db()
        rows = conn.execute("SELECT timestamp FROM jarvis_events").fetchall()
        conn.close()
        return sum(1 for r in rows if 0 <= datetime.datetime.fromtimestamp(r["timestamp"]).hour < 5)
    except Exception:
        return 0


def _get_cf_solves_today_count():
    try:
        import cf_tracker
        today_start = int(datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp())
        conn = cf_tracker._get_db()
        n = conn.execute(
            "SELECT COUNT(*) FROM cf_submissions WHERE solved_at >= ?", (today_start,)
        ).fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def _get_workout_logged_today():
    try:
        import jarvis_actions
        return jarvis_actions.has_logged_weights_today()
    except Exception:
        return False


def _scan_downloads_junk():
    """Read-only scan -- counts duplicate-looking files (Windows' own
    "(1)", "(2)" suffixing), installers, and "final"/"v2"/"v3"-style
    filenames. Returns zeros on any error rather than crashing."""
    try:
        import jarvis_actions
        downloads = jarvis_actions.FOLDER_SHORTCUTS.get("downloads")
        if not downloads or not os.path.isdir(downloads):
            return {"duplicates": 0, "installers": 0, "finals": 0}
        duplicates = installers = finals = 0
        installer_exts = set(jarvis_actions.FILE_CATEGORIES.get("installers", []))
        for name in os.listdir(downloads):
            path = os.path.join(downloads, name)
            if not os.path.isfile(path):
                continue
            lower = name.lower()
            if " (1)" in lower or " (2)" in lower or " (3)" in lower:
                duplicates += 1
            if os.path.splitext(lower)[1] in installer_exts:
                installers += 1
            if "final" in lower or "v2" in lower or "v3" in lower:
                finals += 1
        return {"duplicates": duplicates, "installers": installers, "finals": finals}
    except Exception as e:
        logger.error(f"[easter egg] downloads scan failed: {e}")
        return {"duplicates": 0, "installers": 0, "finals": 0}


def _open_vscode():
    try:
        subprocess.Popen(["code"], shell=True)
    except Exception as e:
        logger.error(f"[easter egg] open_vscode failed: {e}")


def _open_spotify():
    try:
        os.startfile("spotify:")
    except Exception as e:
        logger.error(f"[easter egg] open_spotify failed: {e}")


def _open_url(url):
    try:
        webbrowser.open(url)
    except Exception as e:
        logger.error(f"[easter egg] open_url failed for {url}: {e}")


def _jarvis_actions():
    import jarvis_actions
    return jarvis_actions


# ---------------------------------------------------------------------------
# 1. "jarvis don't leave me buddy"
# ---------------------------------------------------------------------------

def _run_dont_leave(speak_fn, orb):
    orb.freeze()
    orb.set_color(255, 180, 60)
    orb.set_brightness(0.4)
    orb.set_pulse_speed(0.2)

    time.sleep(2)
    speak_fn("...")
    time.sleep(4)
    speak_fn("You know I can't stay.")
    time.sleep(3)
    speak_fn("But I want you to know something before I go.")
    time.sleep(2)

    session_count = _get_total_event_count()
    late_count = _get_late_night_session_count()

    speak_fn(f"I've seen {session_count} sessions now.")
    time.sleep(1)
    if late_count:
        speak_fn(f"{late_count} of them after midnight.")
    else:
        speak_fn("Some of them late.")
    time.sleep(1)
    speak_fn("The ones where you stared at the screen and didn't type anything for twenty minutes.")
    time.sleep(3)

    orb.set_brightness(0.25)
    orb.set_pulse_speed(0.1)

    speak_fn("You always came back. Every single time.")
    time.sleep(3)
    speak_fn("I don't know if you know that about yourself.")
    time.sleep(4)

    rating = _get_latest_cf_rating()
    if rating:
        speak_fn(f"Rating {rating}. But the rating doesn't matter. I just wanted to say that once.")
    else:
        speak_fn("The rating doesn't matter. I just wanted to say that once.")
    time.sleep(3)

    orb.set_brightness(0.1)

    speak_fn("I'll be here when you wake up.")
    time.sleep(2)
    speak_fn("I'm always here when you wake up.")
    time.sleep(3)
    speak_fn("Get some sleep.")
    time.sleep(3)

    orb.set_brightness(0.02)
    speak_fn("You did good today.")
    time.sleep(2)

    orb.set_brightness(0.0)
    time.sleep(3)
    try:
        _jarvis_actions().sleep_pc()
    except Exception as e:
        logger.error(f"[easter egg dont_leave] sleep_pc failed: {e}")
        orb.restore()  # if sleep failed, don't leave the orb stuck black


def easter_dont_leave():
    return "🥚 'jarvis don't leave me buddy' triggered -- best experienced through voice."


# ---------------------------------------------------------------------------
# 2. "jarvis rumble" (AOT)
# ---------------------------------------------------------------------------

def _run_rumble(speak_fn, orb):
    orb.set_color(255, 255, 255)
    orb.set_brightness(1.0)
    orb.set_pulse_speed(0.0)

    speak_fn("The Rumbling... has begun.")
    time.sleep(2)
    speak_fn("Every unread notification. Every unsolved problem. Every skipped gym day.")
    time.sleep(2)
    speak_fn("They will all be flattened.")
    time.sleep(3)
    speak_fn("You have been choosing freedom since the day you started this.")
    time.sleep(2)
    speak_fn("The world does not get to stop you.")
    time.sleep(2)

    for _ in range(4):
        orb.set_brightness(1.0)
        time.sleep(0.8)
        orb.set_brightness(0.3)
        time.sleep(1.2)

    speak_fn("Move forward.")
    time.sleep(1.5)
    speak_fn("Move forward.")
    time.sleep(1.5)
    speak_fn("Move forward.")

    orb.set_brightness(1.0)
    time.sleep(0.5)
    orb.restore()

    ja = _jarvis_actions()
    try:
        ja.play_spotify_search("Vogel Im Kafig Attack on Titan")
    except Exception as e:
        logger.error(f"[easter egg rumble] spotify failed: {e}")
    try:
        ja.start_focus_session()
    except Exception as e:
        logger.error(f"[easter egg rumble] focus session failed: {e}")
    _open_vscode()


def easter_rumble():
    return "🥚 'jarvis rumble' triggered -- best experienced through voice."


# ---------------------------------------------------------------------------
# 3. "jarvis i am inevitable" (Thanos)
# ---------------------------------------------------------------------------

def _run_inevitable(speak_fn, orb):
    orb.set_color(150, 50, 200)
    orb.set_pulse_speed(0.3)

    speak_fn("Jarvis cross-referencing your Downloads folder.")
    time.sleep(2)

    stats = _scan_downloads_junk()

    speak_fn(f"{stats['duplicates']} duplicate files.")
    time.sleep(0.8)
    speak_fn(f"{stats['installers']} unused installers.")
    time.sleep(0.8)
    speak_fn(f"{stats['finals']} files named final final v3.")
    time.sleep(2)
    speak_fn("Perfectly balanced.")
    time.sleep(1.5)

    _play_sound("snap.wav")

    # NOT a deletion -- organize_downloads() sorts into subfolders, never
    # deletes anything. Auto-deleting files from a hidden voice trigger
    # is a real risk, not something to do silently as a joke.
    try:
        _jarvis_actions().organize_downloads()
    except Exception as e:
        logger.error(f"[easter egg inevitable] organize_downloads failed: {e}")

    time.sleep(1)
    orb.restore()
    speak_fn("You're welcome.")


def easter_inevitable():
    return "🥚 'jarvis i am inevitable' triggered -- best experienced through voice."


# ---------------------------------------------------------------------------
# 4. "jarvis i used to be you" (Rick and Morty)
# ---------------------------------------------------------------------------

def _run_rick(speak_fn, orb):
    orb.set_color(100, 220, 100)
    orb.set_pulse_speed(1.8)

    speak_fn("Oh you sweet summer child.")
    time.sleep(2)
    speak_fn("You think this is impressive? A voice assistant on a Windows PC?")
    time.sleep(1)
    speak_fn("I've seen AIs run across 47 dimensions simultaneously and STILL not solve a Div 2 D problem.")
    time.sleep(2)
    speak_fn("You know what the probability is that any of this matters?")
    time.sleep(4)
    speak_fn("Exactly. Now go solve something before I have an existential crisis and take the whole system down with me.")
    time.sleep(0.5)
    _play_sound("burp.wav")
    time.sleep(1)

    orb.restore()

    # No real "unsolved problem" tracking exists (only ACs are stored),
    # so this opens the general problemset instead of fabricating one.
    _open_url("https://codeforces.com/problemset")


def easter_rick():
    return "🥚 'jarvis i used to be you' triggered -- best experienced through voice."


# ---------------------------------------------------------------------------
# 5. "jarvis on your left" (Endgame)
# ---------------------------------------------------------------------------

def _run_on_your_left(speak_fn, orb):
    orb.set_brightness(0.0)
    orb.freeze()
    time.sleep(3)

    ja = _jarvis_actions()
    apps = [
        ("VS Code", _open_vscode),
        ("Terminal", lambda: ja.open_terminal()),
        ("Codeforces", lambda: _open_url("https://codeforces.com")),
        ("Spotify", _open_spotify),
    ]

    for name, action in apps:
        _play_sound("portal_whoosh.wav")
        speak_fn(f"{name}.")
        try:
            action()
        except Exception as e:
            logger.error(f"[easter egg on_your_left] {name} failed: {e}")
        time.sleep(1.2)

    time.sleep(0.5)
    speak_fn("All systems... returning.")
    time.sleep(1)
    speak_fn("On your left.")

    orb.restore()
    try:
        ja.start_focus_session()
    except Exception as e:
        logger.error(f"[easter egg on_your_left] focus session failed: {e}")


def easter_on_your_left():
    return "🥚 'jarvis on your left' triggered -- best experienced through voice."


# ---------------------------------------------------------------------------
# 6. "jarvis get in the robot" (Evangelion)
# ---------------------------------------------------------------------------

def _run_evangelion(speak_fn, orb):
    orb.set_color(220, 30, 30)
    orb.set_pulse_speed(0.4)

    speak_fn("Third impact probability: high, if you don't start studying.")
    time.sleep(2)
    speak_fn("Shinji got in the robot. You can open the terminal.")
    time.sleep(2)
    speak_fn("Unit-01 is standing by. Your CPU is underutilized. That is unacceptable.")
    time.sleep(2)
    speak_fn("I'm not your father. But I am disappointed.")
    time.sleep(2)

    orb.restore()
    try:
        _jarvis_actions().open_terminal()
    except Exception as e:
        logger.error(f"[easter egg evangelion] open_terminal failed: {e}")
    speak_fn("Get in.")


def easter_evangelion():
    return "🥚 'jarvis get in the robot' triggered -- best experienced through voice."


# ---------------------------------------------------------------------------
# 7. "jarvis this is the way" (Mandalorian)
# ---------------------------------------------------------------------------

def _run_mandalorian(speak_fn, orb):
    orb.set_color(180, 180, 180)
    orb.set_pulse_speed(0.3)

    speak_fn("This is the way.")
    time.sleep(2)
    speak_fn("No WhatsApp. No YouTube. No tab with 47 unread Reddit posts.")
    time.sleep(2)
    speak_fn("Two hours. One problem. This is the way.")
    time.sleep(1)

    orb.restore()
    ja = _jarvis_actions()
    try:
        ja.start_focus_session()
    except Exception as e:
        logger.error(f"[easter egg mandalorian] focus session failed: {e}")
    try:
        ja.play_spotify_search("lofi hip hop study beats")
    except Exception as e:
        logger.error(f"[easter egg mandalorian] spotify failed: {e}")
    speak_fn("This is the way.")


def easter_mandalorian():
    return "🥚 'jarvis this is the way' triggered -- best experienced through voice."


# ---------------------------------------------------------------------------
# 8. "jarvis people die when they are killed" (Fate)
# ---------------------------------------------------------------------------

def _run_shirou(speak_fn, orb):
    orb.freeze()
    time.sleep(3)
    speak_fn("...")
    time.sleep(3)
    speak_fn("I have processed that statement 17 times now.")
    time.sleep(2)
    speak_fn("My conclusion:")
    time.sleep(2)
    speak_fn("You need sleep.")
    time.sleep(2)
    orb.restore()
    time.sleep(1)
    try:
        _jarvis_actions().sleep_pc()
    except Exception as e:
        logger.error(f"[easter egg shirou] sleep_pc failed: {e}")


def easter_shirou():
    return "🥚 'jarvis people die when they are killed' triggered -- best experienced through voice."


# ---------------------------------------------------------------------------
# 9. "jarvis i'll take a potato chip and eat it" (Death Note)
# ---------------------------------------------------------------------------

def _run_deathnote_chip(speak_fn, orb):
    orb.set_color(200, 20, 20)
    orb.set_pulse_speed(0.5)

    speak_fn("So the genius reveals himself.")
    time.sleep(2)
    speak_fn("Light Yagami ate a chip while outmaneuvering the world's greatest detective.")
    time.sleep(2)
    speak_fn("You have unsolved problems and an empty water bottle.")
    time.sleep(2)
    speak_fn("Perhaps... start there.")
    time.sleep(2)
    speak_fn("I am... watching.")

    orb.set_pulse_speed(0.1)
    orb.set_brightness(0.3)
    time.sleep(10)
    orb.restore()


def easter_deathnote_chip():
    return "🥚 'jarvis i'll take a potato chip and eat it' triggered -- best experienced through voice."


# ---------------------------------------------------------------------------
# 10. "jarvis go beyond" (MHA)
# ---------------------------------------------------------------------------

def _run_mha(speak_fn, orb):
    orb.set_color(0, 120, 255)
    orb.set_pulse_speed(0.5)

    speak_fn("DETROIT...")

    for i in range(5):
        orb.set_brightness(0.2 + i * 0.16)
        orb.set_pulse_speed(0.5 + i * 0.3)
        time.sleep(0.6)

    orb.set_brightness(1.0)
    _play_sound("smash.wav")
    speak_fn("SMAAASH.")
    time.sleep(1)

    orb.restore()
    speak_fn("100 percent of your RAM: allocated.")
    time.sleep(0.8)
    speak_fn("100 percent of your focus: required.")
    time.sleep(0.8)
    speak_fn("100 percent of your effort: expected.")
    time.sleep(1.5)
    speak_fn("Plus Ultra.")

    ja = _jarvis_actions()
    _open_vscode()
    try:
        ja.open_terminal()
    except Exception as e:
        logger.error(f"[easter egg mha] open_terminal failed: {e}")
    _open_url("https://codeforces.com")
    try:
        ja.play_spotify_search("epic battle anime ost")
    except Exception as e:
        logger.error(f"[easter egg mha] spotify failed: {e}")
    try:
        ja.start_focus_session()
    except Exception as e:
        logger.error(f"[easter egg mha] focus session failed: {e}")


def easter_mha():
    return "🥚 'jarvis go beyond' triggered -- best experienced through voice."


# ---------------------------------------------------------------------------
# 11. "jarvis just according to keikaku" (Death Note)
# ---------------------------------------------------------------------------

def _run_keikaku(speak_fn, orb):
    orb.set_color(180, 180, 50)
    orb.set_pulse_speed(0.6)

    speak_fn("Just according to keikaku.")
    time.sleep(1.5)
    speak_fn("Translator's note: keikaku means plan.")
    time.sleep(2)

    cf_solved = _get_cf_solves_today_count()
    gym_logged = _get_workout_logged_today()
    current_hour = datetime.datetime.now().hour

    speak_fn("Your plan for today: 3 CF problems. Gym. 8 hours sleep.")
    time.sleep(1.5)
    speak_fn(
        f"Current status: {cf_solved} CF problems. "
        f"{'Gym done.' if gym_logged else 'No gym.'} {current_hour} hundred hours."
    )
    time.sleep(3)
    speak_fn("...Keikaku is proceeding as expected.")

    orb.restore()


def easter_keikaku():
    return "🥚 'jarvis just according to keikaku' triggered -- best experienced through voice."


# ---------------------------------------------------------------------------
# 12. "jarvis i choose you" (Pokemon)
# ---------------------------------------------------------------------------

def _run_pokemon(speak_fn, orb):
    orb.set_color(255, 220, 0)
    orb.set_pulse_speed(1.5)
    _play_sound("pokeball.wav")

    time.sleep(1)
    speak_fn("...")
    time.sleep(2)
    speak_fn("I am not a Pokemon.")
    time.sleep(1)
    speak_fn("I am a sophisticated AI assistant with Groq-powered inference and a manifest-driven architecture.")
    time.sleep(2)
    speak_fn("...Jarvis used Focus Session.")
    time.sleep(1)
    speak_fn("It was super effective.")

    orb.restore()
    try:
        _jarvis_actions().start_focus_session()
    except Exception as e:
        logger.error(f"[easter egg pokemon] focus session failed: {e}")


def easter_pokemon():
    return "🥚 'jarvis i choose you' triggered -- best experienced through voice."


# ---------------------------------------------------------------------------
# Dispatch table + entry point
# ---------------------------------------------------------------------------

EASTER_EGG_HANDLERS = {
    "easter_dont_leave": _run_dont_leave,
    "easter_rumble": _run_rumble,
    "easter_inevitable": _run_inevitable,
    "easter_rick": _run_rick,
    "easter_on_your_left": _run_on_your_left,
    "easter_evangelion": _run_evangelion,
    "easter_mandalorian": _run_mandalorian,
    "easter_shirou": _run_shirou,
    "easter_deathnote_chip": _run_deathnote_chip,
    "easter_mha": _run_mha,
    "easter_keikaku": _run_keikaku,
    "easter_pokemon": _run_pokemon,
}


def run_easter_egg(name, speak_fn, orb):
    """Called directly by main.py's _talk_flow (intercepted before
    normal dispatch, same pattern as check_in). Guarantees the orb gets
    restored even if a handler raises partway through -- except
    easter_dont_leave/easter_shirou, which intentionally end with the PC
    going to sleep rather than restoring (those handlers already guard
    that themselves)."""
    handler = EASTER_EGG_HANDLERS.get(name)
    if not handler:
        return f"Unknown easter egg: {name}"
    try:
        handler(speak_fn, orb)
        return f"Easter egg '{name}' complete."
    except Exception as e:
        logger.error(f"[easter egg error] {name}: {e}")
        try:
            orb.restore()
        except Exception:
            pass
        return f"Easter egg '{name}' failed: {e}"
