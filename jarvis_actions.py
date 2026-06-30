
import os
import ctypes
import subprocess
import webbrowser
import datetime
import base64
import json
import threading
import shutil
import urllib.parse

import requests
import psutil
from PIL import Image, ImageGrab
from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
import screen_brightness_control as sbc
import keyboard
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv

# This is the most-imported module in the project and reads several
# secrets at module level below -- load .env here too so any standalone
# script/test that imports this module directly still gets them, not
# just the main.py entrypoint.
load_dotenv()

# Imported by name (not `import cf_tracker`) so FUNCTION_REGISTRY's
# globals()[name] lookup below resolves these directly, same dispatch
# pattern as every other manifest entry in this file.
from cf_tracker import (
    cf_rating, cf_today, cf_last_contest, cf_monthly_summary, cf_upcoming_contest,
    build_cf_monthly_payload,
)
from jarvis_memory import check_in, memory_summary, whats_my_plan, last_time
from easter_eggs import (
    easter_dont_leave, easter_rumble, easter_inevitable, easter_rick,
    easter_on_your_left, easter_evangelion, easter_mandalorian, easter_shirou,
    easter_deathnote_chip, easter_mha, easter_keikaku, easter_pokemon,
)


# ---------------------------------------------------------------------------
# Config you should edit
# ---------------------------------------------------------------------------

PROJECT_PATHS = {
    # "jarvis": r"C:\Users\YourName\Projects\jarvis",
    # "portfolio": r"C:\Users\YourName\Projects\portfolio",
}

SITES = {
    "codeforces": "https://codeforces.com",
    "leetcode": "https://leetcode.com",
    "claude": "https://claude.ai",
    "gemini": "https://gemini.google.com",
    "youtube": "https://youtube.com",
    "github": "https://github.com",
    "google": "https://google.com",
}

SCREENSHOT_DIR = os.path.expanduser("~/Pictures/jarvis_screenshots")
NOTES_FILE = os.path.expanduser("~/jarvis_notes.txt")
AI_OUTPUT_DIR = os.path.expanduser("~/jarvis_ai_outputs")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.makedirs(AI_OUTPUT_DIR, exist_ok=True)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TEXT_MODEL = "llama-3.3-70b-versatile"
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# Spotify: create an app at developer.spotify.com/dashboard to get these.
# Set its redirect URI to exactly match SPOTIFY_REDIRECT_URI below.
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = "http://localhost:8888/callback"

# WhatsApp: numbers in international format, digits only (country code +
# number, no "+" or spaces), e.g. "919876543210".
WHATSAPP_CONTACTS = {
    # "mom": "919876543210",
}

# Bare 10-digit numbers (no country code) get this prepended automatically.
# Without it, WhatsApp Web shows an "invalid phone number" page instead of
# a chat, and the automation just hangs for a minute waiting for an input
# box that will never appear. Set to your own country code.
WHATSAPP_DEFAULT_COUNTRY_CODE = "91"

FOLDER_SHORTCUTS = {
    "downloads": os.path.expanduser("~/Downloads"),
    "desktop": os.path.expanduser("~/Desktop"),
    "documents": os.path.expanduser("~/Documents"),
    "pictures": os.path.expanduser("~/Pictures"),
}


# ---------------------------------------------------------------------------
# System controls
# ---------------------------------------------------------------------------

def lock_screen():
    ctypes.windll.user32.LockWorkStation()
    return "Locked the screen."


def sleep_pc():
    subprocess.run("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)
    return "Putting PC to sleep."


def shutdown_pc(seconds=5):
    subprocess.run(f"shutdown /s /t {seconds}", shell=True)
    return f"Shutting down in {seconds} seconds. Say 'cancel shutdown' to stop it."


def cancel_shutdown():
    subprocess.run("shutdown /a", shell=True)
    return "Shutdown cancelled."


_volume_iface_cache = None
_volume_iface_lock = threading.Lock()


def _volume_interface():
    # Cached -- repeatedly Activate()-ing and discarding a fresh COM
    # pointer on every call (the old behavior) is what caused the random
    # "access violation" crashes from pycaw's COM cleanup. Reusing one
    # interface for the process lifetime avoids that churn.
    global _volume_iface_cache
    with _volume_iface_lock:
        if _volume_iface_cache is None:
            devices = AudioUtilities.GetSpeakers()
            # Newer pycaw wraps IMMDevice in an AudioDevice object; unwrap it first.
            dev = getattr(devices, "_dev", devices)
            interface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            _volume_iface_cache = cast(interface, POINTER(IAudioEndpointVolume))
        return _volume_iface_cache


def set_volume(level=50):
    level = max(0, min(int(level), 100))
    _volume_interface().SetMasterVolumeLevelScalar(level / 100, None)
    return f"Volume set to {level}%."


def mute(state=True):
    _volume_interface().SetMute(1 if state else 0, None)
    return "Muted." if state else "Unmuted."


def set_brightness(level=70):
    level = max(0, min(int(level), 100))
    sbc.set_brightness(level)
    return f"Brightness set to {level}%."


# ---------------------------------------------------------------------------
# Coding / work shortcuts
# ---------------------------------------------------------------------------

def open_project(name):
    path = PROJECT_PATHS.get(name.lower().strip())
    if not path:
        return f"No project path configured for '{name}'. Add it to PROJECT_PATHS."
    subprocess.Popen(["code", path], shell=True)
    return f"Opened project '{name}' in VS Code."


def open_terminal(path=None):
    path = path or os.path.expanduser("~")
    subprocess.Popen(f'start wt -d "{path}"', shell=True)
    return f"Opened terminal at {path}."


def open_site(name):
    key = name.lower().strip()
    if key in SITES:
        url = SITES[key]
    elif name.startswith("http://") or name.startswith("https://"):
        url = name
    elif " " in key:
        # Not a real domain or shortcut -- e.g. the model hallucinated
        # something like "youtube subscribe" as a second site to open.
        # Search instead of building a bogus "youtube subscribe.com" URL.
        return google_search(name)
    elif "." in key:
        url = f"https://{key}"
    else:
        url = f"https://{key}.com"
    webbrowser.open(url)
    return f"Opened {url}"


# ---------------------------------------------------------------------------
# Quick utilities
# ---------------------------------------------------------------------------

def take_screenshot():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SCREENSHOT_DIR, f"screenshot_{timestamp}.png")
    ImageGrab.grab().save(path)
    return path


def save_note(text):
    with open(NOTES_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}] {text}\n")
    return "Note saved."


# ---------------------------------------------------------------------------
# Daily progress tracker (weekly lifting split, runs, CF problems)
# ---------------------------------------------------------------------------

PROGRESS_LOG_PATH = os.path.expanduser("~/jarvis_progress_log.json")

# Your weekly split -- Monday is Day 1, Saturday is Day 6. Used so you can
# just rattle off weights in order and have them matched to the right
# exercise automatically, instead of naming each one every time.
WEEKLY_ROUTINE = {
    "Monday": [
        "Seated row", "T-bar row", "Archer row", "Lat pulldown",
        "Face pulls", "DB curls",
    ],
    "Tuesday": [
        "Flat barbell bench", "Incline DB press", "Cable crossover",
        "Seated shoulder press", "Face pulls", "Lateral raises",
        "Tricep rope pushdown",
    ],
    "Wednesday": [
        "Leg press", "Romanian deadlift", "Single-leg RDL",
        "Walking lunges", "Leg extension", "Cable crunch",
        "Hanging leg raise", "Plank",
    ],
    "Thursday": [
        "Pull-ups", "Single-arm DB row", "Cable row", "Face pulls",
        "Reverse flys", "Lat pulldown", "Hammer curls",
    ],
    "Friday": [
        "Incline barbell press", "DB shoulder press", "Cable flys",
        "Arnold press", "Lateral raises", "Rear delt flys",
        "Tricep overhead extension",
    ],
    "Saturday": [],  # full-body metabolic / run day -- no fixed lift list
}


def _load_progress_log():
    if not os.path.exists(PROGRESS_LOG_PATH):
        return {}
    with open(PROGRESS_LOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_progress_log(data):
    with open(PROGRESS_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def todays_workout():
    """Lists today's exercises from the weekly split, so you know what
    you're about to log weights for."""
    day_name = datetime.date.today().strftime("%A")
    exercises = WEEKLY_ROUTINE.get(day_name, [])
    if not exercises:
        return f"{day_name}: no fixed lift list -- full-body/metabolic or rest day."
    return f"{day_name}'s lifts: " + ", ".join(exercises)


CF_LETTERS = ["A", "B", "C", "D", "E", "F", "G"]


def log_progress(weights=None, exercises=None, distance=None, run_minutes=None,
                  problems_solved=None, cf_breakdown=None):
    """Logs today's numbers. `weights` is a list of numbers in the order
    you say them, matched positionally to today's exercise list from
    WEEKLY_ROUTINE (so you can just say "60, 25, 15, 40, 20, 15, 18" and
    have each land on the right lift) -- OR pass `exercises` as a
    {name: weight} dict directly if you want to name them explicitly or
    only log some of today's lifts. `distance` (km) and `run_minutes`
    (minutes taken) cover a run. `cf_breakdown` is a list of problem
    counts in order A, B, C, D... (e.g. "3, 4, 1, 0" -> A=3, B=4, C=1,
    D=0) and automatically sums into `problems_solved` -- or pass
    `problems_solved` directly for just a total with no breakdown.
    Call multiple times through the day -- each call only overwrites the
    fields you actually pass."""
    today = datetime.date.today().isoformat()
    day_name = datetime.date.today().strftime("%A")
    data = _load_progress_log()
    entry = data.get(today, {})
    entry.setdefault("exercises", {})

    if weights is not None:
        todays_list = WEEKLY_ROUTINE.get(day_name, [])
        for name, w in zip(todays_list, weights):
            entry["exercises"][name] = float(w)
        leftover = len(weights) - len(todays_list)
        if leftover > 0:
            return (
                f"Logged {len(todays_list)} of today's lifts, but you gave "
                f"{leftover} extra number(s) with no matching exercise -- "
                "check WEEKLY_ROUTINE or pass `exercises` explicitly instead."
            )
    if exercises is not None:
        for name, w in exercises.items():
            entry["exercises"][name] = float(w)

    if distance is not None:
        entry["distance"] = float(distance)
    if run_minutes is not None:
        entry["run_minutes"] = float(run_minutes)
    if cf_breakdown is not None:
        breakdown = {
            letter: int(count)
            for letter, count in zip(CF_LETTERS, cf_breakdown)
            if int(count) > 0
        }
        entry["cf_breakdown"] = breakdown
        entry["problems_solved"] = sum(int(c) for c in cf_breakdown)
    elif problems_solved is not None:
        entry["problems_solved"] = int(problems_solved)

    data[today] = entry
    _save_progress_log(data)

    parts = []
    if entry["exercises"]:
        parts.append(f"{len(entry['exercises'])} lift(s) logged")
    if "distance" in entry:
        pace = ""
        if "run_minutes" in entry and entry["distance"]:
            pace = f" ({entry['run_minutes'] / entry['distance']:.1f} min/km)"
        parts.append(f"{entry['distance']}km run" + (f" in {entry['run_minutes']}min{pace}" if "run_minutes" in entry else pace))
    if "problems_solved" in entry:
        if entry.get("cf_breakdown"):
            breakdown_str = ", ".join(f"{k}: {v}" for k, v in entry["cf_breakdown"].items())
            parts.append(f"{entry['problems_solved']} CF problems solved ({breakdown_str})")
        else:
            parts.append(f"{entry['problems_solved']} CF problems solved")
    return f"Logged for today: {', '.join(parts) if parts else 'nothing yet'}."


def has_logged_today():
    """True the moment ANY field is logged for today. Kept for backwards
    compatibility -- prefer has_logged_weights_today()/has_logged_cf_today()
    when you need to know specifically which part is still missing."""
    today = datetime.date.today().isoformat()
    return today in _load_progress_log()


def has_logged_weights_today():
    today = datetime.date.today().isoformat()
    entry = _load_progress_log().get(today, {})
    return bool(entry.get("exercises"))


def has_logged_cf_today():
    today = datetime.date.today().isoformat()
    entry = _load_progress_log().get(today, {})
    return "problems_solved" in entry


def show_progress(days=7):
    """Summarizes the last `days` days -- per-exercise weight trend, run
    pace trend, and CF problems trend."""
    data = _load_progress_log()
    if not data:
        return "No progress logged yet. Try logging today's numbers first."

    cutoff = datetime.date.today() - datetime.timedelta(days=days - 1)
    recent = sorted(
        (date, entry) for date, entry in data.items()
        if datetime.date.fromisoformat(date) >= cutoff
    )
    if not recent:
        return f"No entries in the last {days} days."

    def _trend(vals):
        if len(vals) < 2:
            return f"{vals[-1]}" if vals else "no data"
        first, last = vals[0], vals[-1]
        if last > first:
            return f"up ({first} -> {last})"
        elif last < first:
            return f"down ({first} -> {last})"
        return f"flat ({last})"

    lines = [f"Progress over the last {len(recent)} logged day(s):"]

    exercise_names = sorted({name for _, e in recent for name in e.get("exercises", {})})
    for name in exercise_names:
        vals = [e["exercises"][name] for _, e in recent if name in e.get("exercises", {})]
        lines.append(f"- {name}: {_trend(vals)} kg")

    distances = [e["distance"] for _, e in recent if "distance" in e]
    if distances:
        lines.append(f"- Distance run (km): {_trend(distances)}")
    paces = [
        e["run_minutes"] / e["distance"] for _, e in recent
        if "distance" in e and "run_minutes" in e and e["distance"]
    ]
    if paces:
        lines.append(f"- Run pace (min/km): {_trend([round(p, 1) for p in paces])}")

    problems = [e["problems_solved"] for _, e in recent if "problems_solved" in e]
    if problems:
        lines.append(f"- CF problems solved: {_trend(problems)}")

    return "\n".join(lines)


PROGRESS_CHARTS_DIR = os.path.expanduser("~/Desktop/jarvis_progress_charts")


def generate_progress_charts(month=None, year=None):
    """Builds line/bar charts for a given month -- one per exercise
    (weight over time), one for run pace, one for CF problems solved --
    and saves them as PNGs in a dated subfolder. Defaults to the current
    month if not specified."""
    today = datetime.date.today()
    month = int(month) if month else today.month
    year = int(year) if year else today.year

    data = _load_progress_log()
    month_entries = sorted(
        (date, entry) for date, entry in data.items()
        if datetime.date.fromisoformat(date).month == month
        and datetime.date.fromisoformat(date).year == year
    )
    if not month_entries:
        return f"No logged entries for {year}-{month:02d}."

    import matplotlib
    matplotlib.use("Agg")  # headless -- no Tk window popping up mid-workout
    import matplotlib.pyplot as plt

    out_dir = os.path.join(PROGRESS_CHARTS_DIR, f"{year}-{month:02d}")
    os.makedirs(out_dir, exist_ok=True)
    saved = []

    dates = [datetime.date.fromisoformat(d) for d, _ in month_entries]
    day_labels = [d.strftime("%d") for d in dates]

    # one weight line-chart per exercise
    exercise_names = sorted({name for _, e in month_entries for name in e.get("exercises", {})})
    for name in exercise_names:
        xs, ys = [], []
        for label, (_, e) in zip(day_labels, month_entries):
            if name in e.get("exercises", {}):
                xs.append(label)
                ys.append(e["exercises"][name])
        if len(ys) < 1:
            continue
        plt.figure(figsize=(8, 4))
        plt.plot(xs, ys, marker="o", color="#ff6600")
        plt.title(f"{name} -- {year}-{month:02d}")
        plt.xlabel("Day")
        plt.ylabel("Weight (kg)")
        plt.grid(alpha=0.3)
        plt.tight_layout()
        safe_name = "".join(c if c.isalnum() else "_" for c in name).strip("_")
        path = os.path.join(out_dir, f"{safe_name}.png")
        plt.savefig(path)
        plt.close()
        saved.append(path)

    # bar chart: run pace (min/km) per logged run day
    pace_xs, pace_ys = [], []
    for label, (_, e) in zip(day_labels, month_entries):
        if "distance" in e and "run_minutes" in e and e["distance"]:
            pace_xs.append(label)
            pace_ys.append(e["run_minutes"] / e["distance"])
    if pace_ys:
        plt.figure(figsize=(8, 4))
        plt.bar(pace_xs, pace_ys, color="#3399ff")
        plt.title(f"Run pace (min/km) -- {year}-{month:02d}")
        plt.xlabel("Day")
        plt.ylabel("min/km")
        plt.grid(alpha=0.3, axis="y")
        plt.tight_layout()
        path = os.path.join(out_dir, "run_pace.png")
        plt.savefig(path)
        plt.close()
        saved.append(path)

    # bar chart: CF problems solved per day (total)
    cf_xs, cf_ys = [], []
    for label, (_, e) in zip(day_labels, month_entries):
        if "problems_solved" in e:
            cf_xs.append(label)
            cf_ys.append(e["problems_solved"])
    if cf_ys:
        plt.figure(figsize=(8, 4))
        plt.bar(cf_xs, cf_ys, color="#33cc66")
        plt.title(f"CF problems solved (total) -- {year}-{month:02d}")
        plt.xlabel("Day")
        plt.ylabel("Problems solved")
        plt.grid(alpha=0.3, axis="y")
        plt.tight_layout()
        path = os.path.join(out_dir, "cf_problems_total.png")
        plt.savefig(path)
        plt.close()
        saved.append(path)

    # line chart: CF problems solved per letter (A, B, C, D...) -- same
    # per-item breakdown style as the per-exercise weight charts above
    letters_present = sorted({l for _, e in month_entries for l in e.get("cf_breakdown", {})})
    if letters_present:
        plt.figure(figsize=(8, 4))
        for letter in letters_present:
            xs, ys = [], []
            for label, (_, e) in zip(day_labels, month_entries):
                if letter in e.get("cf_breakdown", {}):
                    xs.append(label)
                    ys.append(e["cf_breakdown"][letter])
            if xs:
                plt.plot(xs, ys, marker="o", label=f"Problem {letter}")
        plt.title(f"CF problems solved by letter -- {year}-{month:02d}")
        plt.xlabel("Day")
        plt.ylabel("Problems solved")
        plt.legend()
        plt.grid(alpha=0.3)
        plt.tight_layout()
        path = os.path.join(out_dir, "cf_problems_by_letter.png")
        plt.savefig(path)
        plt.close()
        saved.append(path)

    if not saved:
        return f"No chartable data for {year}-{month:02d} (entries exist but had no weights/run/CF numbers)."

    assessment = _get_hermes_assessment(month_entries, month, year)
    if assessment:
        with open(os.path.join(out_dir, "assessment.txt"), "w", encoding="utf-8") as f:
            f.write(assessment)

    subprocess.Popen(f'explorer "{out_dir}"', shell=True)
    note = " Hermes left a note in assessment.txt." if assessment else ""
    return f"Saved {len(saved)} chart(s) to {out_dir} (opened in Explorer).{note}"


HERMES_ASSESSMENT_PROMPT = (
    "You are reviewing someone's monthly fitness and Codeforces progress "
    "log. All lifting weights are in KILOGRAMS, not pounds -- never say lbs. "
    "Each day's entry may include `distance` (km) and `run_minutes` (time "
    "taken to run that distance) for a run. Pace = run_minutes / distance: "
    "a LOWER run_minutes for the same or similar distance means the person "
    "got FASTER, which is IMPROVEMENT, not regression -- do not get this "
    "backwards. There may also be a top-level `codeforces` key with "
    "auto-tracked data (not manually logged): rating_start/rating_end/"
    "rating_delta (CF rating change this month -- positive is good), "
    "contests_participated, problems_solved, by_index (count per problem "
    "letter A/B/C/D... -- solving harder/later letters consistently is a "
    "stronger signal than just a high total), by_tag (topics practiced), "
    "and contest_results (per-contest solved list and penalty -- lower "
    "penalty for the same solve count is better). There may also be a "
    "`mood` key from daily check-ins: checkins (how many check-ins this "
    "month), avg_energy (1=low, 2=medium, 3=high), and rough_days (count "
    "of days logged as mood=rough) -- mention if energy/mood looks "
    "correlated with the other metrics. Look at all the raw data below "
    "and write a short, honest assessment (4-6 sentences): are they "
    "progressing well or stalling/regressing on each metric (lifting "
    "weights, run pace, CF rating/problems/contest performance, mood/"
    "energy)? Call out anything that looks genuinely good and anything "
    "that looks weak or inconsistent. Be direct, not generic encouragement. "
    "Plain text only, "
    "no markdown."
)


def _get_hermes_assessment(month_entries, month, year):
    """Asks the local Ollama 'hermes3' model for a qualitative verdict on
    the month's raw numbers -- this runs during the same background tick
    as chart generation, so Hermes's slower local inference doesn't cost
    anything time-sensitive (unlike the real-time voice path, which uses
    Groq instead). Includes the auto-tracked Codeforces payload (rating,
    contests, by-index/by-tag breakdown) alongside the manual gym/run log,
    so Hermes judges both halves together."""
    try:
        import ollama
        payload = dict(month_entries)
        try:
            cf_payload = build_cf_monthly_payload(month, year)
            payload["codeforces"] = cf_payload["codeforces"]
        except Exception as e:
            print(f"[hermes assessment] could not attach cf payload: {e}")
        try:
            import jarvis_memory
            payload["mood"] = jarvis_memory.get_mood_summary_for_month(month, year)
        except Exception as e:
            print(f"[hermes assessment] could not attach mood payload: {e}")
        raw = json.dumps(payload, indent=2)
        client = ollama.Client(host="http://localhost:11434")
        response = client.chat(
            model="hermes3",
            messages=[
                {"role": "system", "content": HERMES_ASSESSMENT_PROMPT},
                {"role": "user", "content": f"Data for {year}-{month:02d}:\n{raw}"},
            ],
            options={"temperature": 0.4},
        )
        return response["message"]["content"].strip()
    except Exception as e:
        print(f"[hermes assessment error] {e}")
        return None


def _popup(title, message):
    # Native Win32 message box -- safe to call from a background thread,
    # unlike Tkinter dialogs.
    ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x1000)


def set_timer(minutes=5, label="Timer"):
    def fire():
        _popup("Jarvis", f"{label} is done!")
    timer = threading.Timer(float(minutes) * 60, fire)
    timer.daemon = True
    timer.start()
    return f"Timer set for {minutes} minute(s)."


# ---------------------------------------------------------------------------
# Media controls (works regardless of which player has focus)
# ---------------------------------------------------------------------------

def media_play_pause():
    keyboard.send("play/pause media")
    return "Toggled play/pause."


def media_next():
    keyboard.send("next track")
    return "Skipped to next track."


def media_previous():
    keyboard.send("previous track")
    return "Went to previous track."


_youtube_queue = []
_youtube_queue_index = -1


def _youtube_play_at(index):
    global _youtube_queue_index
    if not _youtube_queue or not (0 <= index < len(_youtube_queue)):
        return None
    video = _youtube_queue[index]
    url = video.get("webpage_url") or f"https://www.youtube.com/watch?v={video['id']}"
    webbrowser.open(url)
    _youtube_queue_index = index
    return video.get("title", url)


def play_youtube(query, count=10):
    """Searches YouTube for `query`, fetches the top `count` results as a
    queue, and opens the top result's watch page (which autoplays). Use
    skip_youtube()/previous_youtube() afterwards to move through the rest
    of the queue without re-searching."""
    global _youtube_queue, _youtube_queue_index
    try:
        # extract_flat skips fully resolving each video (which is what
        # made fetching a 10-result queue much slower than the old
        # single-video lookup) -- flat entries still have id/title/url,
        # which is all _youtube_play_at needs.
        ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist"}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
            entries = info.get("entries", [])
            if entries:
                _youtube_queue = entries
                _youtube_queue_index = -1
                title = _youtube_play_at(0)
                if title:
                    return f"Playing on YouTube: {title}"
    except Exception as e:
        print(f"YouTube resolve failed, falling back to search: {e}")

    # Fallback: plain search results page if resolving the top video failed
    _youtube_queue = []
    _youtube_queue_index = -1
    url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
    webbrowser.open(url)
    return f"Opened YouTube search for '{query}' (couldn't auto-resolve the top result)."


def skip_youtube():
    """Plays the next video in the current YouTube queue (from the last
    play_youtube() search), instead of re-searching."""
    if not _youtube_queue:
        return "No YouTube queue active -- play something with play_youtube first."
    next_index = _youtube_queue_index + 1
    if next_index >= len(_youtube_queue):
        return "No more videos left in the queue."
    title = _youtube_play_at(next_index)
    return f"Playing on YouTube: {title}"


def previous_youtube():
    """Plays the previous video in the current YouTube queue."""
    if not _youtube_queue:
        return "No YouTube queue active -- play something with play_youtube first."
    prev_index = _youtube_queue_index - 1
    if prev_index < 0:
        return "Already at the first video in the queue."
    title = _youtube_play_at(prev_index)
    return f"Playing on YouTube: {title}"


# ---------------------------------------------------------------------------
# Spotify -- search and play a specific track
# ---------------------------------------------------------------------------

_spotify_client = None


def _get_spotify_client():
    global _spotify_client
    if _spotify_client is not None:
        return _spotify_client
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    auth_manager = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="user-modify-playback-state user-read-playback-state",
        cache_path=".spotify_token_cache",
    )
    _spotify_client = spotipy.Spotify(auth_manager=auth_manager)
    return _spotify_client


def _spotify_open(uri):
    """Opens a Spotify URI in the web player — autoplays on any tier."""
    # Convert spotify:track:ID → https://open.spotify.com/track/ID
    if uri.startswith("spotify:"):
        parts = uri.split(":")          # ['spotify', 'track', 'TRACKID']
        if len(parts) == 3:
            url = f"https://open.spotify.com/{parts[1]}/{parts[2]}"
        else:
            url = "https://open.spotify.com"
    else:
        url = uri
    webbrowser.open(url)


def play_spotify_search(query):
    """Searches Spotify for a track and autoplays it in the desktop app."""
    client = _get_spotify_client()
    if client:
        try:
            results = client.search(q=query, type="track", limit=1)
            tracks  = results.get("tracks", {}).get("items", [])
            if tracks:
                track  = tracks[0]
                artist = track["artists"][0]["name"] if track["artists"] else ""
                _spotify_open(track["uri"])
                return f"Playing on Spotify: {track['name']} by {artist}."
        except Exception as e:
            print(f"[spotify search error] {e}")

    # Fallback: open search in app
    _spotify_open(f"spotify:search:{urllib.parse.quote(query)}")
    return f"Opened Spotify and searched for '{query}'."


# ---------------------------------------------------------------------------
# File / folder management
# ---------------------------------------------------------------------------

FILE_CATEGORIES = {
    "images": [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"],
    "documents": [".pdf", ".docx", ".doc", ".txt", ".xlsx", ".pptx", ".csv"],
    "videos": [".mp4", ".mov", ".avi", ".mkv"],
    "audio": [".mp3", ".wav", ".flac"],
    "archives": [".zip", ".rar", ".7z", ".tar", ".gz"],
    "installers": [".exe", ".msi"],
}


def organize_downloads():
    """Sorts loose files in the Downloads folder into subfolders by type
    (images, documents, videos, audio, archives, installers)."""
    downloads = FOLDER_SHORTCUTS["downloads"]
    moved = 0
    for filename in os.listdir(downloads):
        filepath = os.path.join(downloads, filename)
        if not os.path.isfile(filepath):
            continue
        ext = os.path.splitext(filename)[1].lower()
        category = next((cat for cat, exts in FILE_CATEGORIES.items() if ext in exts), None)
        if not category:
            continue
        dest_dir = os.path.join(downloads, category)
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(filepath, os.path.join(dest_dir, filename))
        moved += 1
    return f"Organized {moved} file(s) in Downloads."


def find_files(query, search_dir=None):
    """Searches for files whose name contains `query` under search_dir
    (defaults to your home folder) and opens Explorer with the first
    match selected."""
    # Resolve shortcut names the LLM might pass (e.g. "Downloads", "Documents")
    if search_dir:
        search_dir = FOLDER_SHORTCUTS.get(search_dir.lower().strip(), search_dir)
    else:
        search_dir = os.path.expanduser("~")
    search_dir = os.path.normpath(search_dir)
    matches = []
    for root, _, files in os.walk(search_dir):
        for f in files:
            if query.lower() in f.lower():
                matches.append(os.path.join(root, f))
                if len(matches) >= 20:
                    break
        if len(matches) >= 20:
            break

    if not matches:
        return f"No files found matching '{query}'."
    os.startfile(matches[0])
    extra = f" (+{len(matches) - 1} more found)" if len(matches) > 1 else ""
    return f"Opened: {os.path.basename(matches[0])}{extra}"


def open_folder(name):
    path = FOLDER_SHORTCUTS.get(name.lower().strip())
    if not path:
        return f"No folder configured for '{name}'."
    path = os.path.normpath(path)
    subprocess.Popen(f'explorer /root,"{path}"', shell=True)
    return f"Opened {name} folder."


# ---------------------------------------------------------------------------
# System monitoring
# ---------------------------------------------------------------------------

def system_status():
    """Reports current CPU, RAM, and battery usage."""
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    lines = [
        f"CPU usage: {cpu}%",
        f"RAM usage: {ram.percent}% ({ram.used // (1024**3)}GB / {ram.total // (1024**3)}GB)",
    ]
    battery = psutil.sensors_battery()
    if battery:
        status = "charging" if battery.power_plugged else "on battery"
        lines.append(f"Battery: {battery.percent}% ({status})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Web search & quick lookups
# ---------------------------------------------------------------------------

def google_search(query):
    """Opens a Google search for `query` in the default browser."""
    url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
    webbrowser.open(url)
    return f"Searched Google for '{query}'."


# ---------------------------------------------------------------------------
# Communication -- WhatsApp auto-send via Playwright, with a webbrowser fallback
# ---------------------------------------------------------------------------

WHATSAPP_PROFILE_DIR = os.path.expanduser("~/.jarvis_whatsapp_profile")

INPUT_BOX_SELECTOR = 'div[contenteditable="true"][data-tab="10"]'
SEND_BUTTON_SELECTOR = 'button[aria-label="Send"], span[data-icon="send"]'

_whatsapp_playwright = None
_whatsapp_browser_ctx = None


def _get_whatsapp_context():
    """Launches (once) a persistent Chromium profile so the WhatsApp Web
    login survives across calls -- without this, every send would hit a
    fresh QR-login screen instead of an already-authenticated session."""
    global _whatsapp_playwright, _whatsapp_browser_ctx
    if _whatsapp_browser_ctx is not None:
        return _whatsapp_browser_ctx
    from playwright.sync_api import sync_playwright
    _whatsapp_playwright = sync_playwright().start()
    _whatsapp_browser_ctx = _whatsapp_playwright.chromium.launch_persistent_context(
        WHATSAPP_PROFILE_DIR, headless=False
    )
    return _whatsapp_browser_ctx


def _send_whatsapp_autonomous(number, message):
    ctx = _get_whatsapp_context()
    page = ctx.new_page()
    encoded = urllib.parse.quote(message)
    page.goto(f"https://web.whatsapp.com/send?phone={number}&text={encoded}", timeout=60000)

    # Wait for the chat's input box to mount and actually contain our text
    # (the page pre-fills it asynchronously after the QR/load handshake).
    input_box = page.locator(INPUT_BOX_SELECTOR).first
    input_box.wait_for(state="visible", timeout=60000)
    page.wait_for_function(
        """(sel) => {
            const el = document.querySelector(sel);
            return el && el.innerText.trim().length > 0;
        }""",
        arg=INPUT_BOX_SELECTOR,
        timeout=15000,
    )
    # Let the DOM settle (WhatsApp Web re-renders the box briefly after fill).
    page.wait_for_timeout(400)

    try:
        input_box.press("Enter")
    except Exception:
        page.locator(SEND_BUTTON_SELECTOR).first.click(timeout=5000)

    page.wait_for_timeout(800)
    page.close()


def send_whatsapp(contact, message):
    """Sends `message` to `contact` on WhatsApp Web completely hands-free
    -- navigates to the pre-filled chat, waits for the message box to
    populate, then presses Enter (falling back to clicking the send icon)
    so it's actually transmitted, not just drafted. `contact` can be a
    name from WHATSAPP_CONTACTS or a raw phone number with country code.
    Requires `playwright` (pip install playwright && playwright install
    chromium) and a one-time manual QR login on first run. Falls back to
    just opening the chat in your default browser if Playwright isn't
    available or the automation fails -- in that case you still need to
    click Send yourself."""
    key = contact.lower().strip()
    number = WHATSAPP_CONTACTS.get(key)
    if not number:
        digits_only = "".join(ch for ch in contact if ch.isdigit())
        if len(digits_only) == 10:
            # Bare local number, no country code -- assume default rather
            # than letting WhatsApp Web silently reject it and hang for
            # 60s waiting on a chat box that never loads.
            number = WHATSAPP_DEFAULT_COUNTRY_CODE + digits_only
        elif len(digits_only) > 10:
            number = digits_only
        else:
            return (
                f"'{contact}' isn't a configured contact and doesn't look like "
                "a phone number. Either add it to WHATSAPP_CONTACTS, or say the "
                "full number including country code."
            )

    try:
        _send_whatsapp_autonomous(number, message)
        return f"Sent WhatsApp message to {contact}."
    except ImportError:
        encoded = urllib.parse.quote(message)
        webbrowser.open(f"https://web.whatsapp.com/send?phone={number}&text={encoded}")
        return (
            f"Opened WhatsApp chat with {contact} (Playwright not installed, "
            "so message wasn't auto-sent -- click send to deliver it)."
        )
    except Exception as e:
        encoded = urllib.parse.quote(message)
        webbrowser.open(f"https://web.whatsapp.com/send?phone={number}&text={encoded}")
        return (
            f"Opened WhatsApp chat with {contact}, but auto-send failed ({e}) "
            "-- click send to deliver it."
        )


def draft_email(to, subject, body):
    """Opens your default mail client with a new draft pre-filled. You
    still need to hit send yourself."""
    params = urllib.parse.urlencode({"subject": subject, "body": body})
    webbrowser.open(f"mailto:{to}?{params}")
    return f"Opened an email draft to {to}."


# ---------------------------------------------------------------------------
# Focus / Pomodoro mode
# ---------------------------------------------------------------------------

# Process names (as seen in Task Manager) to close when a focus session
# starts. Add your own distractions here, e.g. "Discord.exe", "Steam.exe".
DISTRACTION_PROCESSES = [
    # "Discord.exe",
    # "Steam.exe",
]

_focus_session_active = False


def start_focus_session(minutes=25):
    """Closes configured distracting apps and starts a focus timer that
    alerts you when it's done. Note: this closes whole apps, not
    individual browser tabs -- closing just specific tabs in an
    already-open browser isn't reliably automatable without a browser
    extension, so if your distraction is browser-based, add your
    browser itself (e.g. "chrome.exe") to DISTRACTION_PROCESSES."""
    global _focus_session_active
    closed = set()
    for proc in psutil.process_iter(["name"]):
        name = proc.info.get("name")
        if name in DISTRACTION_PROCESSES:
            try:
                proc.terminate()
                closed.add(name)
            except Exception:
                pass

    _focus_session_active = True

    def _end():
        global _focus_session_active
        if _focus_session_active:
            _focus_session_active = False
            _popup("Jarvis", "Focus session complete!")

    timer = threading.Timer(float(minutes) * 60, _end)
    timer.daemon = True
    timer.start()

    # Lets the "no focus session logged today" nudge know it happened.
    try:
        import jarvis_memory
        jarvis_memory.set_memory("last_focus_session_date", datetime.date.today().isoformat())
    except Exception:
        pass

    closed_msg = f" Closed: {', '.join(closed)}." if closed else ""
    return f"Focus session started for {minutes} minutes.{closed_msg}"


def end_focus_session():
    """Ends the current focus session early, if one is running."""
    global _focus_session_active
    if not _focus_session_active:
        return "No focus session is currently running."
    _focus_session_active = False
    return "Focus session ended early."


# ---------------------------------------------------------------------------
# AI delegation -- hand work off to an LLM (Groq)
# ---------------------------------------------------------------------------

def _call_groq(messages, model, max_tokens=2000):
    if not GROQ_API_KEY:
        return None, "GROQ_API_KEY environment variable is not set."

    # Memory injection wraps every Groq call made through this helper
    # (ask_ai, solve_from_screenshot) with a [JARVIS MEMORY] context
    # block -- never rewrites the caller's messages, just prepends.
    # Falls back to the original messages unchanged on any failure.
    try:
        import jarvis_memory
        messages = jarvis_memory.inject_memory(messages)
    except Exception:
        pass

    response = requests.post(
        GROQ_API_URL,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"], None


def _save_and_open(text, filename_prefix):
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(AI_OUTPUT_DIR, f"{filename_prefix}_{timestamp}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    subprocess.Popen(["notepad.exe", path], shell=True)
    return path


def ask_ai(task):
    """Delegates a work task (e.g. 'outline a 5-slide PPT about X',
    'write a Python function that...') to Groq's Llama model and opens
    the result. Free -- uses the same GROQ_API_KEY as your brain."""
    text, error = _call_groq(
        [{"role": "user", "content": task}],
        model=GROQ_TEXT_MODEL,
    )
    if error:
        return error
    path = _save_and_open(text, "ai_output")
    return f"Got a response. Saved and opened: {path}"


def solve_from_screenshot(prompt="Solve this problem and explain your reasoning."):
    """Takes a screenshot of the current screen and sends it to a Groq
    vision model (e.g. for a coding/math problem visible on screen)."""
    screenshot_path = take_screenshot()

    # Groq's base64-image limit is 4MB -- downscale/recompress to be safe.
    img = Image.open(screenshot_path)
    img.thumbnail((1600, 1600))
    compressed_path = screenshot_path.rsplit(".", 1)[0] + "_compressed.jpg"
    img.convert("RGB").save(compressed_path, "JPEG", quality=80)

    with open(compressed_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    text, error = _call_groq(
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
            ],
        }],
        model=GROQ_VISION_MODEL,
    )
    if error:
        return error
    path = _save_and_open(text, "screenshot_solution")
    return f"Sent screenshot to Groq's vision model. Answer saved and opened: {path}"


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def get_time():
    return f"It's {datetime.datetime.now().strftime('%I:%M %p')}."

def get_date():
    return f"Today is {datetime.datetime.now().strftime('%A, %B %d %Y')}."


# ---------------------------------------------------------------------------
# Weather  (wttr.in — no API key needed)
# ---------------------------------------------------------------------------

def get_weather(city=""):
    try:
        url  = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        cur  = data["current_condition"][0]
        area = data["nearest_area"][0]["areaName"][0]["value"]
        desc = cur["weatherDesc"][0]["value"]
        return (f"Weather in {area}: {desc}, {cur['temp_C']}°C "
                f"(feels like {cur['FeelsLikeC']}°C), humidity {cur['humidity']}%.")
    except Exception as e:
        return f"Couldn't fetch weather: {e}"


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def list_processes(top=5):
    procs = sorted(psutil.process_iter(["name", "cpu_percent", "memory_percent"]),
                   key=lambda p: p.info["cpu_percent"] or 0, reverse=True)[:int(top)]
    lines = [f"{p.info['name']}  CPU {p.info['cpu_percent']}%  RAM {p.info['memory_percent']:.1f}%"
             for p in procs]
    return "Top processes:\n" + "\n".join(lines)

def kill_process(name):
    killed = []
    for proc in psutil.process_iter(["name"]):
        if name.lower() in (proc.info["name"] or "").lower():
            try:
                proc.kill(); killed.append(proc.info["name"])
            except Exception:
                pass
    return f"Killed: {', '.join(killed)}" if killed else f"No process found matching '{name}'."

def is_process_running(name):
    for proc in psutil.process_iter(["name"]):
        if name.lower() in (proc.info["name"] or "").lower():
            return f"Yes, '{name}' is running."
    return f"No, '{name}' is not running."


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

def read_clipboard():
    import pyperclip
    text = pyperclip.paste().strip()
    if not text:
        return "Clipboard is empty."
    return text[:400] + ("…" if len(text) > 400 else "")

def summarize_clipboard():
    import pyperclip
    text = pyperclip.paste().strip()
    if not text:
        return "Clipboard is empty."
    result, err = _call_groq(
        [{"role": "user", "content": f"Summarise this in 2–3 sentences:\n\n{text}"}],
        model=GROQ_TEXT_MODEL, max_tokens=200)
    return result or err

def translate_clipboard(target_language="Spanish"):
    import pyperclip
    text = pyperclip.paste().strip()
    if not text:
        return "Clipboard is empty."
    result, err = _call_groq(
        [{"role": "user", "content": f"Translate to {target_language}. Reply with only the translation:\n\n{text}"}],
        model=GROQ_TEXT_MODEL, max_tokens=500)
    return result or err


# ---------------------------------------------------------------------------
# Quick LLM utilities — spoken inline, no file saved
# ---------------------------------------------------------------------------

def translate(text, target_language="Spanish"):
    result, err = _call_groq(
        [{"role": "user", "content": f"Translate to {target_language}. Reply with only the translation:\n\n{text}"}],
        model=GROQ_TEXT_MODEL, max_tokens=300)
    return result or err

def define_word(word):
    result, err = _call_groq(
        [{"role": "user", "content": f"Define '{word}' in one clear sentence."}],
        model=GROQ_TEXT_MODEL, max_tokens=80)
    return result or err

def calculate(expression):
    result, err = _call_groq(
        [{"role": "user", "content": f"Calculate: {expression}. Give just the answer with a one-sentence explanation."}],
        model=GROQ_TEXT_MODEL, max_tokens=80)
    return result or err


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def open_calendar():
    subprocess.Popen("start outlookcal:", shell=True)
    return "Opened Calendar."

def add_calendar_event(title, date="", time=""):
    params = {"text": title}
    if date and time:
        dt = date.replace("-", "") + "T" + time.replace(":", "") + "00"
        params["dates"] = f"{dt}/{dt}"
    elif date:
        d = date.replace("-", "")
        params["dates"] = f"{d}/{d}"
    url = "https://calendar.google.com/calendar/r/eventedit?" + urllib.parse.urlencode(params)
    webbrowser.open(url)
    return f"Opening Google Calendar to add: {title}."


# ---------------------------------------------------------------------------
# Spotify playlist
# ---------------------------------------------------------------------------

def play_spotify_playlist(name):
    client = _get_spotify_client()
    if not client:
        return "Spotify API credentials not configured."
    try:
        playlists, offset = [], 0
        while True:
            batch = client.current_user_playlists(limit=50, offset=offset)
            items = batch.get("items", [])
            if not items:
                break
            playlists.extend(items)
            offset += len(items)
            if not batch.get("next"):
                break
        name_lower = name.lower().strip()
        match = None
        for pl in playlists:
            if (pl.get("name") or "").lower() == name_lower:
                match = pl; break
        if not match:
            for pl in playlists:
                if name_lower in (pl.get("name") or "").lower():
                    match = pl; break
        if match:
            _spotify_open(match["uri"])
            return f"Playing playlist: {match['name']}."
        available = ", ".join(pl["name"] for pl in playlists[:8])
        return f"Couldn't find '{name}'. Your playlists: {available}."
    except Exception as e:
        return f"Spotify error: {e}"


# ---------------------------------------------------------------------------
# Manifest -- single source of truth for both dispatch and the brain's prompt
# ---------------------------------------------------------------------------

FUNCTION_MANIFEST = [
    {"name": "lock_screen", "description": "Locks the Windows screen immediately.", "args": {}},
    {"name": "sleep_pc", "description": "Puts the PC to sleep.", "args": {}},
    {"name": "shutdown_pc", "description": "Shuts down the PC after a delay.", "args": {"seconds": "int, default 5"}},
    {"name": "cancel_shutdown", "description": "Cancels a pending shutdown.", "args": {}},
    {"name": "set_volume", "description": "Sets system volume.", "args": {"level": "int 0-100"}},
    {"name": "mute", "description": "Mutes or unmutes system audio.", "args": {"state": "bool"}},
    {"name": "set_brightness", "description": "Sets screen brightness.", "args": {"level": "int 0-100"}},
    {"name": "open_project", "description": "Opens a configured project folder in VS Code.", "args": {"name": "string, must match PROJECT_PATHS key"}},
    {"name": "open_terminal", "description": "Opens a terminal window, optionally at a path.", "args": {"path": "string, optional"}},
    {"name": "open_site", "description": "Opens a website (configured shortcut name or any URL/domain).", "args": {"name": "string"}},
    {"name": "take_screenshot", "description": "Takes a screenshot and saves it.", "args": {}},
    {"name": "save_note", "description": "Appends a timestamped free-text note to the notes file. Do NOT use this for workout weights, run distance/time, or CF problem counts -- those always go through log_progress instead, even if the user just says a bare list of numbers with the word 'log'.", "args": {"text": "string"}},
    {"name": "todays_workout", "description": "Lists today's lifts from the weekly workout split, in order.", "args": {}},
    {"name": "log_progress", "description": "Logs today's gym/run/CF numbers. ALWAYS use this (never save_note) whenever the user says 'log' followed by a list of bare numbers (e.g. 'log 85 20 25 60 45 25') -- pass them as `weights` in the order given, and they'll be matched to today's exercises in order automatically. If the user names specific exercises, pass `exercises` as a {exercise_name: weight} object instead. For CF problems solved 'in order A, B, C, D' (e.g. 'I solved 3, 4, 1, 0 problems today'), pass `cf_breakdown` as a list of counts in that A/B/C/D... order -- it auto-sums into the total. Only pass the fields actually mentioned -- can be called multiple times per day.", "args": {"weights": "list of numbers, optional, weights in the order today's exercises are listed", "exercises": "object, optional, {exercise_name: weight_kg} for naming specific lifts", "distance": "number, optional, km run", "run_minutes": "number, optional, minutes taken for the run", "cf_breakdown": "list of ints, optional, CF problems solved per letter in order A, B, C, D...", "problems_solved": "int, optional, CF total with no breakdown"}},
    {"name": "show_progress", "description": "Summarizes logged progress over the last N days -- per-exercise weight trend, run pace trend, and CF problems trend.", "args": {"days": "int, optional, default 7"}},
    {"name": "generate_progress_charts", "description": "Builds and saves line/bar chart PNGs for a given month, defaulting to the current month: per-exercise weight over time, run pace, CF problems solved (total bar chart AND a per-letter A/B/C/D breakdown line chart, same style as the per-exercise charts), plus an assessment.txt written by the local Hermes model judging whether progress was good/bad on each metric. Opens the folder in Explorer when done.", "args": {"month": "int, optional, 1-12, defaults to current month", "year": "int, optional, defaults to current year"}},
    {"name": "cf_rating", "description": "Current Codeforces rating (auto-tracked via the CF API) and the change vs one week ago.", "args": {}},
    {"name": "cf_today", "description": "Problems solved on Codeforces today (auto-tracked), with problem names.", "args": {}},
    {"name": "cf_last_contest", "description": "Breakdown of your most recently FINISHED Codeforces contest -- problems solved, first-AC time per problem, wrong-submission penalty.", "args": {}},
    {"name": "cf_monthly_summary", "description": "Codeforces summary for a month (auto-tracked) -- problems solved by index (A/B/C/D...), rating delta, contests participated.", "args": {"month": "int, optional, defaults to current month", "year": "int, optional, defaults to current year"}},
    {"name": "cf_upcoming_contest", "description": "Next upcoming Codeforces contest (Div 1/2/1+2/Educational) and time remaining until it starts.", "args": {}},
    {"name": "check_in", "description": "Triggers the mood/energy check-in flow -- Jarvis asks about energy, mood, and soreness/injuries via voice, then speaks an adjusted plan for today. Use this whenever the user says things like 'check in', 'how am I doing', or asks about their energy/mood.", "args": {}},
    {"name": "memory_summary", "description": "Speaks today's distilled memory summary (generated nightly at 11 PM from the past week's activity).", "args": {}},
    {"name": "whats_my_plan", "description": "Reads today's plan adjustment (from the last mood check-in), whether today is a workout day, and the next upcoming CF contest.", "args": {}},
    {"name": "last_time", "description": "Looks up the last time the user asked about or did something related to a topic, e.g. 'last time I asked about Spotify'.", "args": {"topic": "string, the topic/keyword to search for"}},
    {"name": "easter_dont_leave", "description": "Triggers ONLY when the user says the exact phrase 'jarvis don't leave me buddy'. A scripted emotional sequence, ends by putting the PC to sleep.", "args": {}},
    {"name": "easter_rumble", "description": "Triggers ONLY when the user says the exact phrase 'jarvis rumble'. Attack on Titan themed sequence.", "args": {}},
    {"name": "easter_inevitable", "description": "Triggers ONLY when the user says the exact phrase 'jarvis i am inevitable'. Thanos themed sequence that scans (not deletes) the Downloads folder.", "args": {}},
    {"name": "easter_rick", "description": "Triggers ONLY when the user says the exact phrase 'jarvis i used to be you'. Rick and Morty themed sequence.", "args": {}},
    {"name": "easter_on_your_left", "description": "Triggers ONLY when the user says the exact phrase 'jarvis on your left'. Avengers Endgame themed sequence that opens several apps in order.", "args": {}},
    {"name": "easter_evangelion", "description": "Triggers ONLY when the user says the exact phrase 'jarvis get in the robot'. Evangelion themed sequence.", "args": {}},
    {"name": "easter_mandalorian", "description": "Triggers ONLY when the user says the exact phrase 'jarvis this is the way'. Mandalorian themed focus-session sequence.", "args": {}},
    {"name": "easter_shirou", "description": "Triggers ONLY when the user says the exact phrase 'jarvis people die when they are killed'. Fate themed sequence, ends by putting the PC to sleep.", "args": {}},
    {"name": "easter_deathnote_chip", "description": "Triggers ONLY when the user says the exact phrase about taking a potato chip and eating it. Death Note themed sequence.", "args": {}},
    {"name": "easter_mha", "description": "Triggers ONLY when the user says the exact phrase 'jarvis go beyond'. My Hero Academia themed sequence.", "args": {}},
    {"name": "easter_keikaku", "description": "Triggers ONLY when the user says the exact phrase 'jarvis just according to keikaku'. Death Note themed sequence reading today's real progress data.", "args": {}},
    {"name": "easter_pokemon", "description": "Triggers ONLY when the user says the exact phrase 'jarvis i choose you'. Pokemon themed sequence.", "args": {}},
    {"name": "set_timer", "description": "Sets a timer/reminder that pops up an alert when done.", "args": {"minutes": "number", "label": "string, optional"}},
    {"name": "media_play_pause", "description": "Toggles play/pause on the active media player (e.g. Spotify).", "args": {}},
    {"name": "media_next", "description": "Skips to the next track.", "args": {}},
    {"name": "media_previous", "description": "Goes to the previous track.", "args": {}},
    {"name": "ask_ai", "description": "Delegates a work task to an LLM (e.g. PPT outline, code, writing) via Groq and opens the result.", "args": {"task": "string, full task description"}},
    {"name": "solve_from_screenshot", "description": "Screenshots the screen and sends it to a Groq vision model to analyze/solve (e.g. a problem on screen).", "args": {"prompt": "string, optional instruction"}},
    {"name": "play_youtube", "description": "Searches YouTube, builds a queue of top results, and plays the top one.", "args": {"query": "string, what to search/play", "count": "int, optional, queue size, default 10"}},
    {"name": "skip_youtube", "description": "Plays the next video in the current YouTube queue from the last play_youtube search.", "args": {}},
    {"name": "previous_youtube", "description": "Plays the previous video in the current YouTube queue.", "args": {}},
    {"name": "play_spotify_search", "description": "Searches Spotify and plays the top track result (needs Premium + an open Spotify app; falls back to opening search if unavailable).", "args": {"query": "string, song/artist to search and play"}},
    {"name": "organize_downloads", "description": "Sorts loose files in the Downloads folder into subfolders by type.", "args": {}},
    {"name": "find_files", "description": "Searches for files by name under a folder and opens Explorer at the first match.", "args": {"query": "string, filename or partial name", "search_dir": "string, optional folder to search under"}},
    {"name": "open_folder", "description": "Opens a folder in Explorer. Use name='downloads' for the Downloads folder, name='documents' for Documents, name='desktop' for Desktop, name='pictures' for Pictures. IMPORTANT: 'open downloads' → name='downloads', 'open documents' → name='documents'. Never confuse these two.", "args": {"name": "string: exactly one of 'downloads', 'documents', 'desktop', 'pictures'"}},
    {"name": "system_status", "description": "Reports current CPU, RAM, and battery usage.", "args": {}},
    {"name": "google_search", "description": "Opens a Google search for a query in the default browser.", "args": {"query": "string"}},
    {"name": "send_whatsapp", "description": "Sends a WhatsApp message hands-free via browser automation (falls back to opening a pre-filled draft if automation is unavailable). Contact can be a name from WHATSAPP_CONTACTS or a raw phone number with country code.", "args": {"contact": "string, configured contact name or phone number", "message": "string"}},
    {"name": "draft_email", "description": "Opens the default mail client with a new draft pre-filled (user must click send).", "args": {"to": "string, recipient email", "subject": "string", "body": "string"}},
    {"name": "start_focus_session", "description": "Closes configured distracting apps and starts a focus/Pomodoro timer that alerts when done.", "args": {"minutes": "number, optional, default 25"}},
    {"name": "end_focus_session", "description": "Ends the current focus session early.", "args": {}},
    {"name": "get_time", "description": "Returns the current time. Use when user asks 'what time is it' or 'what's the time'.", "args": {}},
    {"name": "get_date", "description": "Returns today's date. Use when user asks 'what day is it' or 'what's the date'.", "args": {}},
    {"name": "get_weather", "description": "Gets current weather for a city. Use when user asks about weather or temperature. Leave city empty for auto-detect.", "args": {"city": "string, optional city name"}},
    {"name": "list_processes", "description": "Lists the top CPU-consuming running processes. Use when user asks what's using CPU or RAM.", "args": {"top": "int, default 5"}},
    {"name": "kill_process", "description": "Kills all running processes whose name matches. Use when user says 'kill', 'close', or 'stop' followed by an app name.", "args": {"name": "string"}},
    {"name": "is_process_running", "description": "Checks whether a named process is currently running.", "args": {"name": "string"}},
    {"name": "read_clipboard", "description": "Reads and returns the current clipboard text.", "args": {}},
    {"name": "summarize_clipboard", "description": "Summarises whatever text is on the clipboard. Use when user says 'summarize clipboard' or 'summarize what I copied'.", "args": {}},
    {"name": "translate_clipboard", "description": "Translates clipboard text into a target language.", "args": {"target_language": "string, e.g. Hindi, French"}},
    {"name": "translate", "description": "Translates given text into a target language and speaks the result aloud. Use when user says 'translate X to Y'.", "args": {"text": "string", "target_language": "string"}},
    {"name": "define_word", "description": "Gives a one-sentence definition of a word and speaks it aloud. Use when user says 'define X' or 'what does X mean'.", "args": {"word": "string"}},
    {"name": "calculate", "description": "Evaluates a maths expression or unit conversion and speaks the answer. Use when user asks to calculate, convert units, or asks a maths question.", "args": {"expression": "string"}},
    {"name": "open_calendar", "description": "Opens the Windows Calendar app.", "args": {}},
    {"name": "add_calendar_event", "description": "Opens Google Calendar to add a new event with a pre-filled title, date and time.", "args": {"title": "string", "date": "string YYYY-MM-DD, optional", "time": "string HH:MM, optional"}},
    {"name": "play_spotify_playlist", "description": "Finds a playlist by name in the user's Spotify library and opens it. Use when user says 'play playlist X' or 'open playlist X on spotify'.", "args": {"name": "string, playlist name"}},
]

FUNCTION_REGISTRY = {f["name"]: globals()[f["name"]] for f in FUNCTION_MANIFEST}


def run_function(name, args=None):
    args = args or {}
    func = FUNCTION_REGISTRY.get(name)
    if not func:
        return f"Unknown function: {name}"
    try:
        result = func(**args)
    except TypeError as e:
        result = f"Bad arguments for {name}: {e}"
    except Exception as e:
        result = f"Error running {name}: {e}"

    # Event logging wraps the dispatcher -- every call gets recorded for
    # the memory layer. Failure here must never break the actual
    # dispatch, hence the blanket except.
    try:
        import jarvis_memory
        jarvis_memory.log_event(name, args, result)
    except Exception:
        pass

    return result


def build_prompt_snippet():
    lines = ["Available functions:"]
    for f in FUNCTION_MANIFEST:
        args_str = ", ".join(f"{k} ({v})" for k, v in f["args"].items()) or "none"
        lines.append(f"- {f['name']}: {f['description']} | args: {args_str}")
    lines.append("")
    lines.append('Respond ONLY with JSON: {"actions": [{"function": "<name>", "args": {...}}, ...]}')
    lines.append("Use multiple entries for multi-step requests (e.g. 'let's code' could open a project, codeforces, leetcode, and start music).")
    return "\n".join(lines)


if __name__ == "__main__":
    # Cross-platform sanity check -- just prints the prompt text.
    print(build_prompt_snippet())