
import os
import ctypes
import subprocess
import webbrowser
import datetime
import base64
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
    {"name": "save_note", "description": "Appends a timestamped note to the notes file.", "args": {"text": "string"}},
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
        return func(**args)
    except TypeError as e:
        return f"Bad arguments for {name}: {e}"
    except Exception as e:
        return f"Error running {name}: {e}"


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