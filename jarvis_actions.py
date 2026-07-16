
import os
import re
import sys
import ctypes
import hashlib
import random
import subprocess
import webbrowser
import datetime
import base64
import json
import threading
import time
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
    cf_upsolve, cf_drill, build_cf_monthly_payload,
)
from jarvis_memory import check_in, memory_summary, whats_my_plan, last_time
from emotion import how_do_you_feel, what_do_you_want
from easter_eggs import (
    easter_dont_leave, easter_rumble, easter_inevitable, easter_rick,
    easter_on_your_left, easter_evangelion, easter_mandalorian, easter_shirou,
    easter_deathnote_chip, easter_mha, easter_keikaku, easter_pokemon,
)


# ---------------------------------------------------------------------------
# Config you should edit
# ---------------------------------------------------------------------------
# Personal entries (contacts, project paths, extra sites/folders) live in
# jarvis_config.json -- it's gitignored, so private data never risks being
# committed. The dicts below are just defaults; matching keys in the JSON
# ("project_paths", "sites", "whatsapp_contacts", "folder_shortcuts",
# "whatsapp_default_country_code") are merged over them at import time.

# ---------------------------------------------------------------------------
# Hooks injected by main.py -- give action functions optional access to
# live TTS and the orb without a circular import. Both stay None when
# this module is used headless (phone server, tests), and every user of
# them degrades gracefully in that case.
# ---------------------------------------------------------------------------

SPEAK_FN = None         # set to JarvisOrb._speak
ORB_CONTROLLER = None   # set to JarvisOrb.orb_controller
GESTURE_EYES = None     # set to JarvisOrb.gesture_eyes (gesture_eyes.GestureEyes)

# Voice output settings, read by main._speak on every utterance.
WHISPER_MODE = False                # manual quiet-mode toggle
VOICE_RATE = 0                      # edge-tts rate offset in percent (-50..50)
VOICE_NAME = "en-US-AriaNeural"

# Last dispatched action -- shown on the phone remote's status card.
# Private functions get their result masked before landing here.
LAST_ACTION = {"name": None, "result": None, "time": None}

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "jarvis_config.json")


def _load_user_config():
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_USER_CONFIG = _load_user_config()

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
# Obsidian vault integration (Stage 1: voice capture)
# ---------------------------------------------------------------------------

def _vault_root():
    """Configured vault as a Path, or None if unset/missing -- callers
    speak a graceful 'not configured' instead of crashing."""
    from pathlib import Path
    p = _USER_CONFIG.get("vault_path")
    if not p:
        return None
    root = Path(p)
    return root if root.is_dir() else None


def _vault_subdir(kind):
    """kind: 'inbox' | 'journal'. Creates the folder on first use --
    these are Jarvis-owned; wiki/ is never touched."""
    root = _vault_root()
    if root is None:
        return None
    sub = root / _USER_CONFIG.get(f"vault_{kind}", kind)
    sub.mkdir(parents=True, exist_ok=True)
    return sub


_CAPTURE_ACKS = ["Noted.", "Captured.", "In the inbox.", "Got it, sir."]


def capture_note(text, todo=False, source="voice"):
    """Appends a spoken note to the vault's daily inbox file
    (<vault>/inbox/YYYY-MM-DD.md), creating it with frontmatter on the
    first capture of the day. todo=True writes it as an unchecked
    checkbox task instead, completable later via complete_task. Note
    content is NEVER logged to the event DB -- capture_note is in
    PRIVATE_FUNCTIONS."""
    inbox = _vault_subdir("inbox")
    if inbox is None:
        return "Vault not configured, sir -- set vault_path in jarvis_config.json."
    text = (text or "").strip()
    if not text:
        return "Nothing to capture, sir."
    today = datetime.date.today().isoformat()
    path = inbox / f"{today}.md"
    prefix = "- [ ] " if todo else "- "
    line = f"{prefix}**{datetime.datetime.now().strftime('%H:%M')}** ({source}) {text}\n"
    if not path.exists():
        path.write_text(f"---\ndate: {today}\ntype: voice-inbox\n---\n\n{line}",
                        encoding="utf-8")
    else:
        # guard: if the file somehow lost its trailing newline (manual
        # edit, crash mid-write), don't glue onto the previous entry
        needs_nl = not path.read_text(encoding="utf-8").endswith("\n")
        with open(path, "a", encoding="utf-8") as f:
            f.write(("\n" if needs_nl else "") + line)
    return random.choice(_CAPTURE_ACKS)


def open_note(name):
    """Opens a vault note in Obsidian via an obsidian:// URI. Fuzzy
    match of the spoken name against filenames in wiki/, journal/,
    inbox/ (read-only listing); speaks which note it chose."""
    root = _vault_root()
    if root is None:
        return "Vault not configured, sir -- set vault_path in jarvis_config.json."
    vault_name = _USER_CONFIG.get("vault_name") or root.name

    name_tokens = set((name or "").lower().replace("-", " ").split())
    if not name_tokens:
        return "Which note, sir?"

    best = None  # (score, rel_path_no_ext, display)
    for sub in ("wiki", "journal", "inbox"):
        d = root / sub
        if not d.is_dir():
            continue
        for p in d.rglob("*.md"):
            stem_words = p.stem.lower().replace("-", " ").replace("_", " ")
            overlap = sum(1 for t in name_tokens if t in stem_words)
            score = overlap / len(name_tokens)
            if score > 0 and (best is None or score > best[0]):
                rel = p.relative_to(root).with_suffix("").as_posix()
                best = (score, rel, p.stem)

    if best is None or best[0] < 0.5:
        return f"No note matching '{name}', sir."
    _, rel, display = best
    uri = (f"obsidian://open?vault={urllib.parse.quote(vault_name)}"
           f"&file={urllib.parse.quote(rel)}")
    os.startfile(uri)
    return f"Opening {display}."


def dictate_to_note(max_seconds=180):
    """Long-form dictation into a vault DRAFT note (instead of typing
    into the focused window like dictation_mode). Each utterance becomes
    a paragraph; 'stop dictation' ends it. The librarian structures the
    draft on its next ingest. Content never reaches the event DB."""
    import voice_input
    inbox = _vault_subdir("inbox")
    if inbox is None:
        return "Vault not configured, sir -- set vault_path in jarvis_config.json."
    if SPEAK_FN:
        SPEAK_FN("Dictating to a draft. Speak in passages; say stop dictation when done.")
    stamp = datetime.datetime.now()
    paras = []
    deadline = time.time() + float(max_seconds)
    while time.time() < deadline:
        try:
            text = voice_input.listen(max_wait=8)
        except Exception:
            break
        if not text:
            continue
        if text.lower().strip().rstrip(".!") in ("stop", "stop dictation", "end dictation"):
            break
        paras.append(text)
    if not paras:
        return "Nothing dictated, sir -- no draft created."
    path = inbox / f"draft-{stamp.strftime('%Y-%m-%d-%H%M')}.md"
    content = (
        f"---\ndate: {stamp.date().isoformat()}\ntype: draft\nstatus: raw\n---\n\n"
        f"# Draft — {stamp.strftime('%Y-%m-%d %H:%M')}\n\n"
        + "\n\n".join(paras) + "\n"
    )
    path.write_text(content, encoding="utf-8")
    return f"Draft saved -- {len(paras)} passage(s). The librarian can shape it later."


def capture_screen_note(comment=""):
    """'Note what I'm looking at': AI description of the current screen
    plus the user's spoken comment, captured together as one inbox note.
    Uses the same Groq vision surface as describe_screen; screenshots
    are deleted immediately, and the note content stays redacted from
    the event DB."""
    if _vault_root() is None:
        return "Vault not configured, sir -- set vault_path in jarvis_config.json."
    desc = describe_screen()
    text = f"[screen] {desc}"
    comment = (comment or "").strip()
    if comment:
        text += f" | my comment: {comment}"
    capture_note(text, source="screen")
    return "Screen noted, sir."


# ── vault tasks: list / complete checkbox items in Jarvis-owned files ───────

def _task_files():
    """Files Jarvis may edit checkboxes in: inbox dailies + CF debriefs.
    NOT the daily journal files (the heartbeat rewrites those nightly,
    which would silently undo a checkbox) and never wiki/."""
    files = []
    inbox = _vault_subdir("inbox")
    journal = _vault_subdir("journal")
    if inbox:
        # only the YYYY-MM-DD dailies Jarvis creates -- other inbox files
        # (dropped-in documents like the roadmap) aren't Jarvis-owned and
        # their checkboxes belong to the librarian/user
        files += sorted(
            (p for p in inbox.glob("*.md") if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", p.name)),
            reverse=True)
    if journal:
        files += sorted(journal.glob("cf-*-debrief.md"), reverse=True)
    return files


_UNCHECKED_RE = re.compile(r"^\s*- \[ \] (.+)$")


def _iter_open_tasks():
    for path in _task_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for i, ln in enumerate(lines):
            m = _UNCHECKED_RE.match(ln)
            if m:
                yield path, i, m.group(1).strip()


def _task_display(raw):
    """Strips the '**HH:MM** (voice) ' capture prefix for speech."""
    return re.sub(r"^\*\*\d{2}:\d{2}\*\* \((?:voice|migrated|phone|screen)\) ", "", raw)


def list_tasks():
    """Speaks the open (unchecked) tasks from the vault inbox and CF
    debriefs."""
    if _vault_root() is None:
        return "Vault not configured, sir."
    tasks = [_task_display(t) for _, _, t in _iter_open_tasks()]
    if not tasks:
        return "No open tasks, sir. Suspiciously productive."
    listed = tasks[:8]
    more = f" ...and {len(tasks) - 8} more." if len(tasks) > 8 else ""
    return f"{len(tasks)} open task(s): " + "; ".join(listed) + "." + more


def complete_task(name):
    """Marks the best-matching open checkbox task as done ([x] plus a
    completion date). Only edits Jarvis-owned files."""
    if _vault_root() is None:
        return "Vault not configured, sir."
    name_tokens = set((name or "").lower().split())
    if not name_tokens:
        return "Which task, sir?"

    best = None  # (score, path, line_idx, text)
    for path, i, text in _iter_open_tasks():
        text_l = _task_display(text).lower()
        overlap = sum(1 for t in name_tokens if t in text_l)
        score = overlap / len(name_tokens)
        if score > 0 and (best is None or score > best[0]):
            best = (score, path, i, text)

    if best is None or best[0] < 0.5:
        return f"No open task matching '{name}', sir."
    _, path, i, text = best
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    today = datetime.date.today().isoformat()
    lines[i] = lines[i].replace("- [ ]", "- [x]", 1).rstrip("\n") + f" ✅ {today}\n"
    path.write_text("".join(lines), encoding="utf-8")
    return f"Done: {_task_display(text)}. Marked complete."


# ── Stage 3: ask_brain -- voice retrieval over the vault ────────────────────

_ASK_BRAIN_PROMPT = (
    "You are JARVIS answering from the user's personal Obsidian notes. "
    "Answer ONLY from the provided notes -- never from general knowledge. "
    "If the notes don't contain the answer, say exactly: "
    "\"I don't have that in your notes.\" "
    "Under 60 words, plain spoken text, no markdown, dry JARVIS tone, "
    "'sir' optional. /no_think"
)


def _retrieve_vault_chunks(query, top_k=5):
    """Tries the vault's own retrieval pipeline first (scripts/retrieve.py
    -- exits 10 unprovisioned, and its bm25 sibling hard-imports fcntl so
    it currently can't run on Windows), then falls back to the local
    pure-Python BM25 searcher. Returns (chunks, source_label)."""
    root = _vault_root()
    script = root / _USER_CONFIG.get("vault_retrieve_script", "scripts/retrieve.py")
    if script.is_file():
        try:
            proc = subprocess.run(
                [sys.executable, str(script), query, "--top", str(top_k), "--no-rerank"],
                capture_output=True, text=True, timeout=15, cwd=str(root),
            )
            if proc.returncode == 0:
                data = json.loads(proc.stdout)
                chunks = [
                    {"page_path": c.get("page_path", "?"), "snippet": c.get("snippet", "")}
                    for c in data.get("candidates", []) if c.get("snippet")
                ]
                if chunks:
                    return chunks, "vault-retrieve"
        except Exception as e:
            print(f"[ask_brain] vault retrieve.py failed ({e}) -- using local fallback")
    import vault_search
    return vault_search.search(root, query, top_k=top_k), "local-bm25-fallback"


def ask_brain(query):
    """Answers a question from the user's Obsidian notes: retrieve top
    chunks (vault pipeline or local BM25), then ask local Ollama qwen3:8b
    with a strict only-from-notes prompt. Speaks an acknowledgment first
    since retrieval + local inference can take a while; the rest runs on
    this dispatch thread (already off the UI thread, _busy held), so the
    answer flows back as the normal spoken outcome."""
    root = _vault_root()
    if root is None:
        return "Vault not configured, sir -- set vault_path in jarvis_config.json."
    if SPEAK_FN:
        try:
            SPEAK_FN("Checking your notes. Give me a moment.")
        except Exception:
            pass

    try:
        # 4 chunks max -- prompt prefill dominates latency on CPU-only
        # Ollama (measured ~64s at 1000 prompt tokens on this machine)
        chunks, source = _retrieve_vault_chunks(query, top_k=4)
        print(f"[ask_brain] retrieval via {source}: {len(chunks)} chunk(s)")
    except Exception as e:
        print(f"[ask_brain] retrieval error: {e}")
        return "I couldn't search your notes, sir -- check the log."
    if not chunks:
        return "I don't have that in your notes."

    context = "\n\n".join(
        f"[note: {c['page_path']}]\n{c['snippet'][:500]}" for c in chunks
    )
    try:
        resp = requests.post(
            "http://127.0.0.1:11434/api/chat",
            json={
                "model": "qwen3:8b",
                "stream": False,
                # think:false is essential -- without it qwen3 burns the
                # whole num_predict budget on hidden reasoning and returns
                # an empty answer (and takes 10x longer)
                "think": False,
                "messages": [
                    {"role": "system", "content": _ASK_BRAIN_PROMPT},
                    {"role": "user",
                     "content": f"Notes:\n{context}\n\nQuestion: {query}"},
                ],
                # num_predict caps runaway generations; keep_alive keeps
                # the model resident so follow-up questions skip the
                # ~30s cold load
                "options": {"temperature": 0.3, "num_predict": 150},
                "keep_alive": "30m",
            },
            timeout=120,
        )
        resp.raise_for_status()
        answer = resp.json()["message"]["content"]
        answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.S).strip()
        return answer or "I don't have that in your notes."
    except Exception as e:
        print(f"[ask_brain] ollama error: {e}")
        return "I couldn't reach the local model, sir -- is Ollama running?"


# ---------------------------------------------------------------------------
# Devil's advocate -- contradiction surfacing over the vault. Read-only:
# this feature NEVER writes the vault (no new files, no wiki/ edits); the
# only state it keeps is a seen-hash list in jarvis_memory.
# ---------------------------------------------------------------------------

_DEVILS_ADVOCATE_PROMPT = (
    "You are JARVIS quietly playing devil's advocate over the user's own "
    "notes. You get RECENT notes (last 7 days) and OLDER notes retrieved "
    "for the same topics. Find at most 2 genuine tensions: a stated "
    "belief, plan, or claim that a later entry contradicts or silently "
    "abandons, or a commitment asserted once and never revisited.\n\n"
    "The bar is HIGH. Both sides must be specific and quotable from the "
    "notes given. If there is no real contradiction, return zero findings "
    "-- do NOT manufacture tension, do NOT pad with vague 'have you "
    "considered' advice. Empty is the expected answer most nights.\n\n"
    "Tone: a neutral observation between equals -- curious, never "
    "moralizing. Each 'say' is ONE sentence, spoken aloud, naming both "
    "sides with their dates or note names in plain speech (no markdown, "
    "no [[links]]), ending with a light question. Example: 'You wrote in "
    "March that finished means public, but Tuesday's entry calls the "
    "tracker done with the repo still private -- still the rule?'\n\n"
    "Respond with raw JSON only, exactly: "
    '{"findings": [{"earlier": "<short verbatim quote>", '
    '"later": "<short verbatim quote>", "say": "<one spoken sentence>"}]}'
)

# Tokens too generic to steer BM25 toward a topic (plus the vault's own
# furniture words that appear in every Jarvis-written journal page).
_CONTRA_STOPWORDS = {
    "the", "and", "that", "this", "with", "for", "was", "are", "but",
    "not", "you", "your", "have", "has", "had", "just", "about", "from",
    "they", "them", "then", "than", "there", "here", "what", "when",
    "how", "why", "did", "does", "will", "would", "should", "could",
    "into", "over", "under", "again", "more", "less", "very", "still",
    "today", "yesterday", "tomorrow", "day", "week", "month",
    "jarvis", "journal", "note", "notes", "entry", "logged", "recorded",
    "summary", "nothing", "none",
}

_SEEN_CONTRADICTIONS_KEY = "seen_contradictions"
_SEEN_CONTRADICTIONS_CAP = 200


def _contradiction_hash(earlier, later):
    """Stable id for a tension: both verbatim quotes, normalized to bare
    alphanumerics so re-punctuation between runs doesn't defeat dedupe."""
    def norm(s):
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    return hashlib.sha1(f"{norm(earlier)}|{norm(later)}".encode()).hexdigest()[:16]


def _recent_vault_entries(root, days=7, per_entry_chars=400):
    """(date, rel_path, text) for date-stamped journal/ and inbox/ pages
    from the last `days` days -- direct file reads, no BM25 needed since
    the filenames ARE the dates."""
    import vault_search
    out = []
    today = datetime.date.today()
    for kind in ("journal", "inbox"):
        d = _vault_subdir(kind)
        if d is None or not d.is_dir():
            continue
        for i in range(days):
            day = (today - datetime.timedelta(days=i)).isoformat()
            p = d / f"{day}.md"
            if not p.exists():
                continue
            try:
                text = vault_search._strip_frontmatter(
                    p.read_text(encoding="utf-8", errors="ignore")).strip()
            except OSError:
                continue
            if text:
                out.append((day, f"{d.name}/{p.name}", text[:per_entry_chars]))
    return out


def _contra_key_terms(texts, root, top_n=10):
    """Most topic-bearing tokens of the recent entries, weighted by
    count x BM25 idf from the existing vault index -- so the retrieval
    query favors 'bench'/'tracker' over words every page shares."""
    import math
    import vault_search
    index = vault_search.ensure_index(root)
    n, df = max(index["n"], 1), index["df"]
    counts = {}
    for t in vault_search._tokenize(" ".join(texts)):
        if len(t) < 3 or t in _CONTRA_STOPWORDS:
            continue
        counts[t] = counts.get(t, 0) + 1
    scored = sorted(
        (c * math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)), t)
        for t, c in counts.items() if t in df
    )
    return [t for _, t in scored[-top_n:]]


def _find_contradictions(mark_seen=True):
    """Shared core for the nightly heartbeat pass and the on-demand voice
    command. Returns a list of speakable finding sentences ([] when the
    notes are consistent or there's nothing to compare), or None when the
    local model call failed. Already-surfaced tensions (hash of the two
    quoted snippets, persisted in jarvis_memory) are filtered out so the
    same one is never re-nagged."""
    root = _vault_root()
    if root is None:
        return []
    recent = _recent_vault_entries(root)
    if not recent:
        return []

    import vault_search
    older = []
    terms = _contra_key_terms([t for _, _, t in recent], root)
    if terms:
        recent_paths = {p for _, p, _ in recent}
        for hit in vault_search.search(root, " ".join(terms), top_k=10):
            if hit["page_path"] not in recent_paths:
                older.append(hit)
            if len(older) >= 5:
                break
    if not older:
        return []

    recent_block = "\n\n".join(f"[{p} — {d}]\n{t}" for d, p, t in recent)
    older_block = "\n\n".join(
        f"[{h['page_path']}]\n{h['snippet'][:300]}" for h in older)
    try:
        resp = requests.post(
            "http://127.0.0.1:11434/api/chat",
            json={
                "model": "qwen3:8b",
                "stream": False,
                "think": False,  # qwen3: hidden reasoning starves the JSON output
                "format": "json",
                "messages": [
                    {"role": "system", "content": _DEVILS_ADVOCATE_PROMPT},
                    {"role": "user", "content":
                        f"RECENT NOTES (last 7 days):\n{recent_block}\n\n"
                        f"OLDER NOTES (same topics):\n{older_block}"},
                ],
                "options": {"temperature": 0.3, "num_predict": 300},
                "keep_alive": "30m",
            },
            timeout=240,
        )
        resp.raise_for_status()
        raw = resp.json()["message"]["content"]
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
        findings = json.loads(raw).get("findings") or []
    except Exception as e:
        print(f"[contradictions] error: {e}")
        return None

    import jarvis_memory
    try:
        seen_list = json.loads(
            jarvis_memory.get_memory(_SEEN_CONTRADICTIONS_KEY) or "[]")
    except (TypeError, ValueError):
        seen_list = []
    seen = set(seen_list)

    out, new_hashes = [], []
    for f in findings[:2]:
        if not isinstance(f, dict):
            continue
        say = (f.get("say") or "").strip()
        h = _contradiction_hash(f.get("earlier"), f.get("later"))
        if say and h not in seen:
            out.append(say)
            new_hashes.append(h)
    if mark_seen and new_hashes:
        try:
            jarvis_memory.set_memory(
                _SEEN_CONTRADICTIONS_KEY,
                json.dumps((seen_list + new_hashes)[-_SEEN_CONTRADICTIONS_CAP:]))
        except Exception as e:
            print(f"[contradictions] seen-set persist error: {e}")
    return out


def check_contradictions():
    """Voice-invoked devil's advocate pass. In PRIVATE_FUNCTIONS: the
    result quotes journal content, which must never reach the event DB
    (it feeds the Groq distillation)."""
    root = _vault_root()
    if root is None:
        return "Vault not configured, sir -- set vault_path in jarvis_config.json."
    if SPEAK_FN:
        try:
            SPEAK_FN("Playing devil's advocate against your notes. Give me a minute.")
        except Exception:
            pass
    findings = _find_contradictions()
    if findings is None:
        return "I couldn't reach the local model, sir -- is Ollama running?"
    if not findings:
        return "Nothing stood out, sir."
    return " ".join(findings)


# Merge user overrides from jarvis_config.json over the defaults above.
PROJECT_PATHS.update(_USER_CONFIG.get("project_paths", {}))
SITES.update(_USER_CONFIG.get("sites", {}))
WHATSAPP_CONTACTS.update(_USER_CONFIG.get("whatsapp_contacts", {}))
FOLDER_SHORTCUTS.update(_USER_CONFIG.get("folder_shortcuts", {}))
WHATSAPP_DEFAULT_COUNTRY_CODE = _USER_CONFIG.get(
    "whatsapp_default_country_code", WHATSAPP_DEFAULT_COUNTRY_CODE
)


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


_audio_meter_cache = None


def get_audio_peak():
    """Current system audio output peak, 0.0-1.0 -- read locally from the
    Windows audio endpoint (pycaw), nothing leaves the machine. Used to
    make the idle orb pulse with music. Returns 0.0 on any failure."""
    global _audio_meter_cache
    try:
        from pycaw.pycaw import IAudioMeterInformation
        with _volume_iface_lock:
            if _audio_meter_cache is None:
                devices = AudioUtilities.GetSpeakers()
                dev = getattr(devices, "_dev", devices)
                interface = dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
                _audio_meter_cache = cast(interface, POINTER(IAudioMeterInformation))
        return float(_audio_meter_cache.GetPeakValue())
    except Exception:
        return 0.0


_ducked_sessions = {}


def duck_other_audio(duck=True, level=0.25):
    """Lowers every other app's volume (Spotify, browser, games) to
    `level` while Jarvis speaks, then restores -- so Jarvis talks OVER
    the music instead of competing with it. Per-app session volumes via
    pycaw, all local. Fails silently: ducking is a nicety, never worth
    breaking speech over."""
    global _ducked_sessions
    try:
        from pycaw.pycaw import AudioUtilities
        sessions = AudioUtilities.GetAllSessions()
        if duck:
            _ducked_sessions = {}
            for s in sessions:
                if s.Process is None:
                    continue
                vol = s.SimpleAudioVolume
                cur = vol.GetMasterVolume()
                if cur > level:
                    _ducked_sessions[s.Process.pid] = cur
                    vol.SetMasterVolume(level, None)
        else:
            for s in sessions:
                if s.Process and s.Process.pid in _ducked_sessions:
                    s.SimpleAudioVolume.SetMasterVolume(
                        _ducked_sessions[s.Process.pid], None)
            _ducked_sessions = {}
    except Exception:
        pass


def minimize_windows():
    """Minimizes all windows (Win+D) -- 'clear my screen'."""
    keyboard.send("windows+d")
    return "Screen cleared."


def dictation_mode(max_seconds=120):
    """Types what you say into whatever window has focus -- say 'stop
    dictation' (or just 'stop') to finish. Audio goes through the same
    Groq Whisper STT as normal commands; nothing is stored."""
    import voice_input
    deadline = time.time() + float(max_seconds)
    typed_chunks = 0
    if SPEAK_FN:
        SPEAK_FN("Dictation on. Speak, and say stop dictation when done.")
    while time.time() < deadline:
        try:
            text = voice_input.listen(max_wait=6)
        except Exception:
            break
        if not text:
            continue
        if text.lower().strip().rstrip(".!") in ("stop", "stop dictation", "end dictation"):
            break
        keyboard.write(text + " ")
        typed_chunks += 1
    return f"Dictation finished -- typed {typed_chunks} segment(s)."


def describe_screen():
    """Speaks a 2-sentence summary of what's currently on screen. Note:
    this sends one screenshot to Groq's vision model (same as
    solve_from_screenshot) -- the answer is spoken, nothing is saved."""
    screenshot_path = take_screenshot()
    img = Image.open(screenshot_path)
    img.thumbnail((1400, 1400))
    compressed_path = screenshot_path.rsplit(".", 1)[0] + "_desc.jpg"
    img.convert("RGB").save(compressed_path, "JPEG", quality=75)
    with open(compressed_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")
    text, error = _call_groq(
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe what is on this screen in at most 2 plain sentences, spoken-style, no markdown."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
            ],
        }],
        model=GROQ_VISION_MODEL, max_tokens=120,
    )
    for p in (screenshot_path, compressed_path):
        try:
            os.remove(p)
        except OSError:
            pass
    return text or f"Couldn't read the screen: {error}"


def run_diagnostics():
    """Self-test: checks each subsystem and reports what's broken.
    Read-only -- makes one tiny request per service, changes nothing."""
    results = []

    def check(name, fn):
        try:
            ok, detail = fn()
            results.append(f"{name}: {'OK' if ok else 'FAIL'}{' -- ' + detail if detail else ''}")
        except Exception as e:
            results.append(f"{name}: FAIL -- {e}")

    def _mic():
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
        return True, dev.get("name", "")

    def _groq():
        if not GROQ_API_KEY:
            return False, "GROQ_API_KEY not set"
        r = requests.get("https://api.groq.com/openai/v1/models",
                         headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=10)
        return r.ok, f"HTTP {r.status_code}"

    def _ollama():
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
        return r.ok, ", ".join(models) or "no models pulled"

    def _spotify():
        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            return False, "credentials not configured"
        return _get_spotify_client() is not None, ""

    def _cf():
        import cf_tracker
        handle = cf_tracker._get_cf_handle()
        if not handle:
            return False, "cf_handle not set in jarvis_config.json"
        r = requests.get("https://codeforces.com/api/user.info",
                         params={"handles": handle}, timeout=10)
        return r.json().get("status") == "OK", f"handle {handle}"

    def _speaker():
        _volume_interface()
        return True, ""

    check("Microphone", _mic)
    check("Speaker/volume", _speaker)
    check("Groq API", _groq)
    check("Ollama", _ollama)
    check("Spotify", _spotify)
    check("Codeforces API", _cf)
    failed = sum(1 for r in results if "FAIL" in r)
    verdict = "All systems operational, sir." if failed == 0 else f"{failed} system(s) need attention."
    return verdict + "\n" + "\n".join(results)


def run_routine(name):
    """Runs a user-defined routine from jarvis_config.json -- a named
    list of plain-language commands executed in order. Define like:
    "routines": {"good night": ["whisper mode on", "mute", "lock the screen"]}"""
    routines = _USER_CONFIG.get("routines", {})
    key = (name or "").lower().strip()
    steps = routines.get(key)
    if steps is None:
        match = [k for k in routines if key in k]
        if len(match) == 1:
            key, steps = match[0], routines[match[0]]
    if steps is None:
        available = ", ".join(routines) or "none defined yet (add a 'routines' block to jarvis_config.json)"
        return f"No routine called '{name}'. Available: {available}."

    import intent_parser  # lazy: intent_parser imports this module at load
    outcomes = []
    for step in steps:
        try:
            parsed = intent_parser.parse_command(step)
            for action in parsed.get("actions", []):
                fn = action.get("function")
                if fn == "chat" or fn == "run_routine":
                    continue  # no chatting or recursion inside routines
                outcomes.append(run_function(fn, action.get("args", {})))
        except Exception as e:
            outcomes.append(f"Error on step '{step}': {e}")
    done = "; ".join(o for o in outcomes if o) or "nothing ran"
    return f"Routine '{key}' complete: {done}"


def show_streaks():
    """Current day-streaks: consecutive days with a CF solve, and
    consecutive gym days (Sundays don't break the gym streak -- rest
    day)."""
    import cf_tracker
    today = datetime.date.today()

    # CF streak from the local submissions DB
    conn = cf_tracker._get_db()
    rows = conn.execute("SELECT DISTINCT date(solved_at, 'unixepoch', 'localtime') d FROM cf_submissions").fetchall()
    conn.close()
    cf_days = {r["d"] for r in rows}
    cf_streak = 0
    day = today
    if day.isoformat() not in cf_days:
        day = day - datetime.timedelta(days=1)  # today isn't over yet
    while day.isoformat() in cf_days:
        cf_streak += 1
        day -= datetime.timedelta(days=1)

    # gym streak from the progress log
    data = _load_progress_log()
    gym_days = {d for d, e in data.items() if e.get("exercises") or "distance" in e}
    gym_streak = 0
    day = today
    if day.isoformat() not in gym_days:
        day = day - datetime.timedelta(days=1)
    while True:
        if day.strftime("%A") == "Sunday":
            day -= datetime.timedelta(days=1)
            continue
        if day.isoformat() in gym_days:
            gym_streak += 1
            day -= datetime.timedelta(days=1)
        else:
            break

    parts = []
    parts.append(f"CF solve streak: {cf_streak} day(s)" if cf_streak else "No active CF streak")
    parts.append(f"gym streak: {gym_streak} day(s)" if gym_streak else "no active gym streak")
    return ", ".join(parts) + "."


# ---------------------------------------------------------------------------
# Voice output settings (read by main._speak)
# ---------------------------------------------------------------------------

def set_whisper_mode(state=True):
    """Manually toggles quiet mode -- TTS plays at reduced volume. Quiet
    hours (11 PM - 7 AM) apply automatically regardless of this toggle."""
    global WHISPER_MODE
    WHISPER_MODE = bool(state)
    return "Whisper mode on -- I'll keep it down, sir." if WHISPER_MODE else "Whisper mode off."


def set_voice_speed(percent=0):
    """Adjusts TTS speaking rate. percent is -50 (slowest) to 50 (fastest),
    0 = normal."""
    global VOICE_RATE
    VOICE_RATE = max(-50, min(50, int(percent)))
    if VOICE_RATE == 0:
        return "Speaking at normal speed."
    return f"Speaking rate set to {VOICE_RATE:+d} percent."


VOICE_CHOICES = {
    "aria": "en-US-AriaNeural",
    "jenny": "en-US-JennyNeural",
    "guy": "en-US-GuyNeural",
    "sonia": "en-GB-SoniaNeural",
    "ryan": "en-GB-RyanNeural",
}


def set_orb_alignment(mode="auto"):
    """Switches the orb's nature: 'angel' (white-gold halos, rising
    motes), 'demon' (black eclipse core, crimson corona, fractured
    halos, sinking embers), or 'auto' (angelic normally, demonic on
    errors and red easter eggs)."""
    import orb_renderer
    mode = (mode or "auto").lower().strip()
    if mode not in ("angel", "demon", "auto"):
        return f"Unknown alignment '{mode}'. Options: angel, demon, auto."
    orb_renderer.set_alignment(mode)
    if mode == "demon":
        return "Embracing the darkness, sir."
    if mode == "angel":
        return "Ascending. As it should be."
    return "Orb alignment back to automatic."


def set_voice(name="aria"):
    """Switches the TTS voice. Options: aria, jenny, guy (US); sonia,
    ryan (British)."""
    global VOICE_NAME
    key = name.lower().strip()
    if key not in VOICE_CHOICES:
        return f"Unknown voice '{name}'. Options: {', '.join(VOICE_CHOICES)}."
    VOICE_NAME = VOICE_CHOICES[key]
    return f"Voice switched to {key.capitalize()}. How do I sound, sir?"


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
    """Legacy alias -- notes now go to the Obsidian vault inbox. Kept in
    FUNCTION_REGISTRY (not the manifest) in case anything internal still
    calls it; jarvis_notes.txt is no longer written."""
    return capture_note(text)


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
                  problems_solved=None, cf_breakdown=None, bodyweight=None):
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

    def _previous_best(exercise):
        best = None
        for date, e in data.items():
            if date == today:
                continue
            w = e.get("exercises", {}).get(exercise)
            if w is not None and (best is None or w > best):
                best = w
        return best

    new_prs = []

    def _record(name, w):
        w = float(w)
        prev = _previous_best(name)
        if prev is not None and w > prev:
            new_prs.append(f"{name} {w}kg (previous best {prev}kg)")
        entry["exercises"][name] = w

    if weights is not None:
        todays_list = WEEKLY_ROUTINE.get(day_name, [])
        for name, w in zip(todays_list, weights):
            _record(name, w)
        leftover = len(weights) - len(todays_list)
        if leftover > 0:
            return (
                f"Logged {len(todays_list)} of today's lifts, but you gave "
                f"{leftover} extra number(s) with no matching exercise -- "
                "check WEEKLY_ROUTINE or pass `exercises` explicitly instead."
            )
    if exercises is not None:
        for name, w in exercises.items():
            _record(name, w)

    if distance is not None:
        entry["distance"] = float(distance)
    if run_minutes is not None:
        entry["run_minutes"] = float(run_minutes)
    if bodyweight is not None:
        entry["bodyweight"] = float(bodyweight)
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
    if "bodyweight" in entry:
        parts.append(f"bodyweight {entry['bodyweight']}kg")
    if "problems_solved" in entry:
        if entry.get("cf_breakdown"):
            breakdown_str = ", ".join(f"{k}: {v}" for k, v in entry["cf_breakdown"].items())
            parts.append(f"{entry['problems_solved']} CF problems solved ({breakdown_str})")
        else:
            parts.append(f"{entry['problems_solved']} CF problems solved")
    msg = f"Logged for today: {', '.join(parts) if parts else 'nothing yet'}."
    if new_prs:
        msg += " NEW PERSONAL RECORD on " + "; ".join(new_prs) + ". Outstanding, sir!"
    return msg


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

    bodyweights = [e["bodyweight"] for _, e in recent if "bodyweight" in e]
    if bodyweights:
        lines.append(f"- Bodyweight (kg): {_trend(bodyweights)}")

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

    # line chart: bodyweight over the month
    bw_xs, bw_ys = [], []
    for label, (_, e) in zip(day_labels, month_entries):
        if "bodyweight" in e:
            bw_xs.append(label)
            bw_ys.append(e["bodyweight"])
    if bw_ys:
        plt.figure(figsize=(8, 4))
        plt.plot(bw_xs, bw_ys, marker="o", color="#cc44aa")
        plt.title(f"Bodyweight (kg) -- {year}-{month:02d}")
        plt.xlabel("Day")
        plt.ylabel("kg")
        plt.grid(alpha=0.3)
        plt.tight_layout()
        path = os.path.join(out_dir, "bodyweight.png")
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
    note = " The local model left a note in assessment.txt." if assessment else ""
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
            model="qwen3:8b",
            messages=[
                {"role": "system", "content": HERMES_ASSESSMENT_PROMPT},
                {"role": "user", "content": f"Data for {year}-{month:02d}:\n{raw}"},
            ],
            think=False,  # qwen3: hidden reasoning starves the output
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


# Named-timer registry: label -> (Timer, fire_at_epoch). Lets you list
# and cancel timers instead of fire-and-forget.
_timers = {}
_timers_lock = threading.Lock()


def set_timer(minutes=5, label="Timer"):
    label = (label or "Timer").strip()
    def fire():
        with _timers_lock:
            _timers.pop(label, None)
        if SPEAK_FN:
            SPEAK_FN(f"{label} is done, sir.")
        else:
            _popup("Jarvis", f"{label} is done!")
    timer = threading.Timer(float(minutes) * 60, fire)
    timer.daemon = True
    with _timers_lock:
        old = _timers.pop(label, None)
        if old:
            old[0].cancel()
        _timers[label] = (timer, time.time() + float(minutes) * 60)
    timer.start()
    return f"Timer '{label}' set for {minutes} minute(s)."


def list_timers():
    """Lists running timers and their remaining time."""
    with _timers_lock:
        items = [(label, fire_at - time.time()) for label, (_, fire_at) in _timers.items()]
    live = [(l, s) for l, s in items if s > 0]
    if not live:
        return "No timers running."
    parts = [f"{l}: {int(s // 60)}m {int(s % 60)}s left" for l, s in live]
    return "Running timers -- " + "; ".join(parts) + "."


def cancel_timer(label="Timer"):
    """Cancels a named timer (or the default 'Timer')."""
    label = (label or "Timer").strip()
    with _timers_lock:
        entry = _timers.pop(label, None)
        if entry is None:
            # fuzzy: single partial match wins
            matches = [l for l in _timers if label.lower() in l.lower()]
            if len(matches) == 1:
                entry = _timers.pop(matches[0])
                label = matches[0]
    if entry is None:
        return f"No timer named '{label}' found."
    entry[0].cancel()
    return f"Cancelled timer '{label}'."


def rest_timer(seconds=90):
    """Between-sets rest timer -- announces out loud when rest is over,
    so you don't have to look at anything with chalk on your hands."""
    seconds = max(5, float(seconds))
    def fire():
        if SPEAK_FN:
            SPEAK_FN("Rest over, sir. Next set.")
        else:
            _popup("Jarvis", "Rest over -- next set!")
    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    return f"Resting {int(seconds)} seconds. I'll call it."


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


# ---------------------------------------------------------------------------
# Gesture control (webcam "eyes" -- see gesture_eyes.py)
# ---------------------------------------------------------------------------

def eyes_on():
    """Starts the webcam gesture watcher. GESTURE_EYES is injected by
    main.py at boot; headless users (phone server, tests) get a polite
    refusal instead of a crash."""
    if GESTURE_EYES is None:
        return "My eyes aren't wired up in this session, sir."
    return GESTURE_EYES.start()


def eyes_off():
    if GESTURE_EYES is None:
        return "My eyes aren't wired up in this session, sir."
    return GESTURE_EYES.stop()


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
    first = matches[0]
    extra = f" (+{len(matches) - 1} more found)" if len(matches) > 1 else ""
    # Never execute a matched file -- os.startfile on an .exe/.bat RUNS it,
    # which a fuzzy voice-matched search should never do. Reveal it in
    # Explorer instead; only genuinely inert documents get opened directly.
    ext = os.path.splitext(first)[1].lower()
    if ext in (".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".scr", ".lnk"):
        subprocess.Popen(f'explorer /select,"{first}"', shell=True)
        return f"Found executable {os.path.basename(first)} -- showed it in Explorer instead of running it.{extra}"
    os.startfile(first)
    return f"Opened: {os.path.basename(first)}{extra}"


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

def buzz_pc(times=5):
    """Plays loud locator beeps so you can find the PC (or check it's
    on) from the phone remote. Unmutes and raises volume first, since a
    muted buzzer finds nothing."""
    try:
        mute(False)
        set_volume(85)
    except Exception:
        pass

    def _beep():
        try:
            import winsound
            for _ in range(max(1, int(times))):
                winsound.Beep(1600, 400)
                time.sleep(0.15)
        except Exception:
            pass

    threading.Thread(target=_beep, daemon=True).start()
    return "Buzzing the PC now."


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
# Contest mode -- battle stations for a CF round
# ---------------------------------------------------------------------------

_contest_mode_active = False


def contest_mode():
    """Locks in for a Codeforces contest: closes configured distracting
    apps, opens the contests page, tints the orb red, and announces
    elapsed time every 30 minutes until end_contest_mode (auto-expires
    after 3.5 hours)."""
    global _contest_mode_active
    if _contest_mode_active:
        return "Contest mode is already active, sir."
    _contest_mode_active = True

    closed = set()
    for proc in psutil.process_iter(["name"]):
        if proc.info.get("name") in DISTRACTION_PROCESSES:
            try:
                proc.terminate()
                closed.add(proc.info["name"])
            except Exception:
                pass

    webbrowser.open("https://codeforces.com/contests")
    if ORB_CONTROLLER:
        ORB_CONTROLLER.set_color(255, 40, 40)

    def _ticker():
        elapsed = 0
        while _contest_mode_active and elapsed < int(3.5 * 3600):
            time.sleep(1800)
            elapsed += 1800
            if _contest_mode_active and SPEAK_FN:
                SPEAK_FN(f"{elapsed // 60} minutes in, sir. Keep pushing.")
        # auto-expire so the orb doesn't stay red forever
        if _contest_mode_active:
            end_contest_mode()

    threading.Thread(target=_ticker, daemon=True).start()
    closed_msg = f" Closed: {', '.join(closed)}." if closed else ""
    return f"Contest mode engaged. Distractions cleared, problems opening.{closed_msg} Good hunting, sir."


_demon_mode_active = False


def demon_mode(minutes=60):
    """Focus mode with teeth: closes every configured distraction app,
    and the orb transforms into a pair of demonic eyes that stay on
    screen watching you -- blinking, gaze wandering, locking onto you --
    until the time runs out or you say 'end demon mode'. All visual;
    nothing is recorded and no camera is involved."""
    global _demon_mode_active
    if _demon_mode_active:
        return "The eyes are already upon you, sir."
    _demon_mode_active = True

    closed = set()
    for proc in psutil.process_iter(["name"]):
        if proc.info.get("name") in DISTRACTION_PROCESSES:
            try:
                proc.terminate()
                closed.add(proc.info["name"])
            except Exception:
                pass

    import orb_renderer
    orb_renderer.set_eyes_mode(True)
    if ORB_CONTROLLER:
        # custom state keeps the window visible (idle would auto-hide it)
        ORB_CONTROLLER.set_color(255, 30, 30)

    def _expire():
        time.sleep(float(minutes) * 60)
        if _demon_mode_active:
            end_demon_mode()
            if SPEAK_FN:
                SPEAK_FN("Demon mode has run its course. You are free, sir.")

    threading.Thread(target=_expire, daemon=True).start()
    closed_msg = f" Banished: {', '.join(closed)}." if closed else ""
    return (f"Demon mode. Distractions are gone and the eyes are open for "
            f"{int(minutes)} minutes.{closed_msg} Work.")


def end_demon_mode():
    """Ends demon mode -- the eyes close and the orb returns."""
    global _demon_mode_active
    if not _demon_mode_active:
        return "Demon mode isn't active."
    _demon_mode_active = False
    import orb_renderer
    orb_renderer.set_eyes_mode(False)
    if ORB_CONTROLLER:
        ORB_CONTROLLER.restore()
    return "The eyes close. Well fought, sir."


def end_contest_mode():
    """Ends contest mode -- restores the orb and stops the elapsed-time
    announcements."""
    global _contest_mode_active
    if not _contest_mode_active:
        return "Contest mode isn't active."
    _contest_mode_active = False
    if ORB_CONTROLLER:
        ORB_CONTROLLER.restore()
    return "Contest mode disengaged. I'll fetch your results after the round is analyzed, sir."


# ---------------------------------------------------------------------------
# AI delegation -- hand work off to an LLM (Groq)
# ---------------------------------------------------------------------------

def _call_groq(messages, model, max_tokens=2000, inject_memory=False):
    if not GROQ_API_KEY:
        return None, "GROQ_API_KEY environment variable is not set."

    if inject_memory:
        try:
            import jarvis_memory
            messages = jarvis_memory.inject_memory(messages)
        except Exception:
            pass

    last_exc = None
    for attempt in range(2):
        try:
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
        except Exception as e:
            last_exc = e
            if attempt == 0:
                time.sleep(1)
    return None, str(last_exc)


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
        inject_memory=True,
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
        inject_memory=True,
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

# Never kill these even on a substring match -- a mis-heard voice command
# matching "system", "host", or "explorer" would otherwise take down the
# shell or core Windows services.
PROTECTED_PROCESSES = {
    "explorer.exe", "svchost.exe", "csrss.exe", "winlogon.exe", "lsass.exe",
    "services.exe", "smss.exe", "wininit.exe", "system", "registry",
    "dwm.exe", "python.exe", "pythonw.exe",  # last two: Jarvis itself
}


def kill_process(name):
    killed, skipped = [], []
    for proc in psutil.process_iter(["name"]):
        pname = proc.info["name"] or ""
        if name.lower() in pname.lower():
            if pname.lower() in PROTECTED_PROCESSES:
                skipped.append(pname)
                continue
            try:
                proc.kill(); killed.append(pname)
            except Exception:
                pass
    if killed:
        note = f" (skipped protected: {', '.join(set(skipped))})" if skipped else ""
        return f"Killed: {', '.join(killed)}{note}"
    if skipped:
        return f"'{name}' only matched protected system processes -- not killing those."
    return f"No process found matching '{name}'."

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
    import math
    _safe_env = {k: getattr(math, k) for k in dir(math) if not k.startswith('_')}
    _safe_env.update({'abs': abs, 'round': round, '__builtins__': {}})
    try:
        val = eval(expression, _safe_env, {})  # noqa: S307
        if isinstance(val, float) and val == int(val):
            val = int(val)
        return f"{expression} = {val}"
    except Exception:
        pass
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
    {"name": "ask_brain", "description": "Answers a question from the user's Obsidian notes. Use for 'ask my brain X', 'what do my notes say about X', 'what did I decide about X', 'when did I X'.", "args": {"query": "string, the question"}},
    {"name": "capture_note", "description": "Appends a spoken note to the Obsidian vault inbox. Use for 'note that X', 'remember that X', 'capture X', 'save a note'. Set todo=true for tasks/todos ('add a task to X', 'remind me to X later', 'add X to my list'). NOT for workout weights/run/CF counts -- those go through log_progress.", "args": {"text": "string, the note content", "todo": "bool, true if it's a task"}},
    {"name": "how_do_you_feel", "description": "Jarvis reports its own computed emotional state with reasons. Use for 'how are you feeling', 'how do you feel', 'what's your mood'.", "args": {}},
    {"name": "what_do_you_want", "description": "Jarvis reports its current cravings/drives. Use for 'what do you want', 'what do you crave', 'what do you need'.", "args": {}},
    {"name": "open_note", "description": "Opens a vault note in Obsidian by fuzzy name. Use for 'open my X note', 'show me the X page'.", "args": {"name": "string, words from the note title"}},
    {"name": "dictate_to_note", "description": "Long-form dictation saved as a vault draft note (NOT typed into a window). Use for 'dictate a note', 'take down a draft', 'dictate into my notes'.", "args": {"max_seconds": "number, optional, default 180"}},
    {"name": "capture_screen_note", "description": "Captures an AI description of the current screen plus an optional spoken comment as one inbox note. Use for 'note what I'm looking at', 'capture this screen with a note'.", "args": {"comment": "string, optional, the user's comment"}},
    {"name": "list_tasks", "description": "Speaks open (unchecked) tasks from the vault. Use for 'what are my tasks', 'what's on my list'.", "args": {}},
    {"name": "complete_task", "description": "Marks an open task done by name. Use for 'mark X done', 'I finished X', 'check off X'.", "args": {"name": "string, words from the task"}},
    {"name": "todays_workout", "description": "Lists today's lifts from the weekly workout split, in order.", "args": {}},
    {"name": "log_progress", "description": "Logs today's gym/run/CF numbers. ALWAYS use this (never capture_note) whenever the user says 'log' followed by a list of bare numbers (e.g. 'log 85 20 25 60 45 25') -- pass them as `weights` in the order given, and they'll be matched to today's exercises in order automatically. If the user names specific exercises, pass `exercises` as a {exercise_name: weight} object instead. For CF problems solved 'in order A, B, C, D' (e.g. 'I solved 3, 4, 1, 0 problems today'), pass `cf_breakdown` as a list of counts in that A/B/C/D... order -- it auto-sums into the total. Only pass the fields actually mentioned -- can be called multiple times per day.", "args": {"weights": "list of numbers, optional, weights in the order today's exercises are listed", "exercises": "object, optional, {exercise_name: weight_kg} for naming specific lifts", "distance": "number, optional, km run", "run_minutes": "number, optional, minutes taken for the run", "cf_breakdown": "list of ints, optional, CF problems solved per letter in order A, B, C, D...", "problems_solved": "int, optional, CF total with no breakdown", "bodyweight": "number, optional, body weight kg ('I weighed 78.2')"}},
    {"name": "show_progress", "description": "Summarizes logged progress over the last N days -- per-exercise weight trend, run pace trend, and CF problems trend.", "args": {"days": "int, optional, default 7"}},
    {"name": "generate_progress_charts", "description": "Builds and saves line/bar chart PNGs for a given month, defaulting to the current month: per-exercise weight over time, run pace, CF problems solved (total bar chart AND a per-letter A/B/C/D breakdown line chart, same style as the per-exercise charts), plus an assessment.txt written by the local Ollama model judging whether progress was good/bad on each metric. Opens the folder in Explorer when done.", "args": {"month": "int, optional, 1-12, defaults to current month", "year": "int, optional, defaults to current year"}},
    {"name": "cf_rating", "description": "Current Codeforces rating (auto-tracked via the CF API) and the change vs one week ago.", "args": {}},
    {"name": "cf_today", "description": "Problems solved on Codeforces today (auto-tracked), with problem names.", "args": {}},
    {"name": "cf_last_contest", "description": "Breakdown of your most recently FINISHED Codeforces contest -- problems solved, first-AC time per problem, wrong-submission penalty.", "args": {}},
    {"name": "cf_monthly_summary", "description": "Codeforces summary for a month (auto-tracked) -- problems solved by index (A/B/C/D...), rating delta, contests participated.", "args": {"month": "int, optional, defaults to current month", "year": "int, optional, defaults to current year"}},
    {"name": "cf_upcoming_contest", "description": "Next upcoming Codeforces contest (Div 1/2/1+2/Educational) and time remaining until it starts.", "args": {}},
    {"name": "cf_upsolve", "description": "Lists contest problems the user attempted but never solved (upsolve targets), skipping ones solved since. Use when user asks 'what should I upsolve' or 'pending upsolves'.", "args": {}},
    {"name": "contest_mode", "description": "Locks in for a Codeforces contest: closes distracting apps, opens the contests page, turns the orb red, and announces elapsed time every 30 minutes. Use when user says 'contest mode' or 'contest time'.", "args": {}},
    {"name": "end_contest_mode", "description": "Ends contest mode and restores the orb. Use when user says 'end contest mode' or 'contest is over'.", "args": {}},
    {"name": "rest_timer", "description": "Between-sets gym rest timer -- speaks 'rest over' aloud when done. Use when user says 'rest 90' or 'rest timer 2 minutes' (convert minutes to seconds).", "args": {"seconds": "number, default 90"}},
    {"name": "set_whisper_mode", "description": "Toggles quiet TTS mode (lower speaking volume). Use for 'whisper mode', 'be quiet', 'speak quietly', 'normal volume voice'.", "args": {"state": "bool, true for on"}},
    {"name": "set_voice_speed", "description": "Changes how fast Jarvis talks. Use 'talk faster' -> 20, 'talk slower' -> -20, 'normal speed' -> 0.", "args": {"percent": "int -50 to 50, 0 = normal"}},
    {"name": "set_voice", "description": "Switches the TTS voice. Options: aria, jenny, guy (US); sonia, ryan (British).", "args": {"name": "string, one of: aria, jenny, guy, sonia, ryan"}},
    {"name": "buzz_pc", "description": "Plays loud locator beeps on the PC (unmutes first). Use for 'find my pc', 'buzz the computer', 'make some noise'.", "args": {"times": "int, default 5"}},
    {"name": "list_timers", "description": "Lists all running timers with remaining time. Use for 'what timers are running' or 'how long left on my timer'.", "args": {}},
    {"name": "cancel_timer", "description": "Cancels a running timer by its label. Use for 'cancel the pasta timer' -> label='pasta'.", "args": {"label": "string, the timer's label"}},
    {"name": "run_diagnostics", "description": "Self-test of all subsystems (mic, speaker, Groq, Ollama, Spotify, Codeforces API) and reports what's broken. Use for 'run diagnostics' or 'system check'.", "args": {}},
    {"name": "run_routine", "description": "Runs a user-defined multi-step routine from jarvis_config.json by name. Use when the user says a routine name like 'good night' or 'run my morning routine'.", "args": {"name": "string, routine name"}},
    {"name": "show_streaks", "description": "Reports the current CF solve day-streak and gym day-streak. Use for 'what's my streak'.", "args": {}},
    {"name": "cf_drill", "description": "Practice drill -- opens a random unsolved Codeforces problem rated ~offset above the user's rating. Use for 'give me a problem', 'drill me', 'practice problem'.", "args": {"offset": "int, optional, rating points above current, default 100"}},
    {"name": "dictation_mode", "description": "Types whatever the user speaks into the focused window until they say 'stop dictation'. Use for 'take dictation' or 'type what I say'.", "args": {"max_seconds": "number, optional, default 120"}},
    {"name": "minimize_windows", "description": "Minimizes all windows to show the desktop. Use for 'clear my screen', 'minimize everything', 'show desktop'.", "args": {}},
    {"name": "describe_screen", "description": "Speaks a 2-sentence summary of what's currently visible on screen. Use for 'what's on my screen', 'describe my screen'.", "args": {}},
    {"name": "set_orb_alignment", "description": "Switches the orb's visual nature. Use 'go demonic'/'dark mode orb' -> mode='demon', 'go angelic'/'be an angel' -> mode='angel', 'orb back to normal' -> mode='auto'.", "args": {"mode": "string: angel, demon, or auto"}},
    {"name": "demon_mode", "description": "ALWAYS use this when the user says 'demon mode': closes all distraction apps (focus mode) AND transforms the orb into a pair of watching demonic eyes for the duration. Not the same as set_orb_alignment.", "args": {"minutes": "number, optional, default 60"}},
    {"name": "end_demon_mode", "description": "Ends demon mode -- eyes close, orb returns. Use for 'end demon mode', 'stop watching me', 'release me'.", "args": {}},
    {"name": "check_in", "description": "Triggers the mood/energy check-in flow -- Jarvis asks about energy, mood, and soreness/injuries via voice, then speaks an adjusted plan for today. Use this whenever the user says things like 'check in', 'how am I doing', or asks about their energy/mood.", "args": {}},
    {"name": "memory_summary", "description": "Speaks today's distilled memory summary (generated nightly at 11 PM from the past week's activity).", "args": {}},
    {"name": "check_contradictions", "description": "Reviews recent journal/notes for contradictions or stale claims and reports findings.", "args": {}},
    {"name": "whats_my_plan", "description": "Reads today's plan adjustment (from the last mood check-in), whether today is a workout day, and the next upcoming CF contest.", "args": {}},
    {"name": "last_time", "description": "Looks up the last time the user asked about or did something related to a topic, e.g. 'last time I asked about Spotify'.", "args": {"topic": "string, the topic/keyword to search for"}},
    # NOTE: easter eggs are intentionally NOT in this manifest -- they're
    # exact-phrase triggers matched locally in intent_parser
    # (EXACT_PHRASE_TRIGGERS) at zero token cost. The functions stay
    # importable above for FUNCTION_REGISTRY-free dispatch paths.
    {"name": "set_timer", "description": "Sets a timer/reminder that pops up an alert when done.", "args": {"minutes": "number", "label": "string, optional"}},
    {"name": "media_play_pause", "description": "Toggles play/pause on the active media player (e.g. Spotify).", "args": {}},
    {"name": "eyes_on", "description": "Turns on gesture control -- the webcam reads hand gestures to control media, tabs and windows.", "args": {}},
    {"name": "eyes_off", "description": "Turns off gesture control and releases the webcam.", "args": {}},
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

# save_note: legacy alias, dispatchable but not in the manifest.
FUNCTION_REGISTRY["save_note"] = save_note

# Easter eggs stay dispatchable (phone remote uses the safe zero-arg
# fallbacks) even though they're no longer in the manifest prompt.
FUNCTION_REGISTRY.update({
    name: globals()[name] for name in (
        "easter_dont_leave", "easter_rumble", "easter_inevitable",
        "easter_rick", "easter_on_your_left", "easter_evangelion",
        "easter_mandalorian", "easter_shirou", "easter_deathnote_chip",
        "easter_mha", "easter_keikaku", "easter_pokemon",
    )
})


# Functions whose args/results carry personal content (clipboard text,
# private notes, message bodies). Their event-log entries are redacted so
# that content never lands in the memory DB -- which later gets sent to
# Groq during the nightly distillation. Only the fact that the function
# ran is recorded, which is all the nudge/memory layer actually needs.
PRIVATE_FUNCTIONS = {
    "read_clipboard", "summarize_clipboard", "translate_clipboard",
    "save_note", "send_whatsapp", "draft_email", "ask_ai",
    "solve_from_screenshot", "translate", "capture_note",
    "dictate_to_note", "capture_screen_note", "check_contradictions",
}


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

    # Status card on the phone remote shows the last action -- private
    # functions keep their result masked here too.
    LAST_ACTION.update({
        "name": name,
        "result": "[private]" if name in PRIVATE_FUNCTIONS else str(result)[:200],
        "time": datetime.datetime.now().strftime("%H:%M"),
    })

    # Affect: every dispatched event nudges the emotional state (see
    # emotion.py). Expression-only; failures must never touch dispatch.
    # Note: appraise() sees only the function NAME and result string --
    # private capture content stays out of the affect layer too.
    try:
        import emotion
        emotion.appraise(name, None, "" if name in PRIVATE_FUNCTIONS else result)
    except Exception:
        pass

    # Event logging wraps the dispatcher -- every call gets recorded for
    # the memory layer. Failure here must never break the actual
    # dispatch, hence the blanket except.
    try:
        import jarvis_memory
        if name in PRIVATE_FUNCTIONS:
            jarvis_memory.log_event(name, {"redacted": True}, "[content redacted for privacy]")
        else:
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