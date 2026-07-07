"""
intent_parser.py
------------------
The "brain". Takes whatever text the user typed or spoke and asks Groq
to translate it into a structured list of function calls, using the
function manifest in jarvis_actions.py as the single source of truth
for what's allowed.
"""

import os
import json
import time
import requests
from dotenv import load_dotenv

# Loaded here too so running this file standalone (the __main__ block
# below) still picks up .env -- main.py also calls this, but
# load_dotenv() is safe to call more than once.
load_dotenv()

from jarvis_actions import build_prompt_snippet

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_BRAIN_MODEL = "llama-3.3-70b-versatile"
# Fallback when the 70B's tokens-per-minute pool is exhausted -- separate
# free-tier quota, and routing commands to functions is well within an
# 8B's ability (chat answers get slightly plainer during busy minutes).
GROQ_FALLBACK_MODEL = "llama-3.1-8b-instant"

# Easter eggs are exact-phrase triggers -- matching them here costs zero
# tokens and removes 12 entries from the manifest prompt. Keys are
# normalized (lowercase, no punctuation).
EXACT_PHRASE_TRIGGERS = {
    "jarvis dont leave me buddy": "easter_dont_leave",
    "jarvis rumble": "easter_rumble",
    "jarvis i am inevitable": "easter_inevitable",
    "jarvis i used to be you": "easter_rick",
    "jarvis on your left": "easter_on_your_left",
    "jarvis get in the robot": "easter_evangelion",
    "jarvis this is the way": "easter_mandalorian",
    "jarvis people die when they are killed": "easter_shirou",
    "jarvis take a potato chip and eat it": "easter_deathnote_chip",
    "jarvis go beyond": "easter_mha",
    "jarvis just according to keikaku": "easter_keikaku",
    "jarvis i choose you": "easter_pokemon",
}


def _normalize(text):
    return "".join(c for c in text.lower() if c.isalnum() or c == " ").strip()


# "note that I benched 80" should be a log_progress call, not an inbox
# capture -- but the model anchors on the "note that" prefix and routes
# to capture_note regardless of description hints. Fix it locally, for
# free: if a note-prefixed utterance contains a digit AND a workout/
# metric keyword, strip the prefix so the model sees the bare metric
# sentence ("I benched 80 today") and routes it naturally.
import re as _re
_NOTE_PREFIX = _re.compile(
    r"^(?:note that|note down that|remember that|capture that|note|remember|capture)\s+",
    _re.IGNORECASE)
_METRIC_HINT = _re.compile(
    r"\b(?:bench(?:ed)?|squat(?:ted)?|deadlift(?:ed)?|curl(?:s|ed)?|press(?:ed)?|"
    r"pull[ -]?ups?|lat pulldown|lunges?|raises?|rdl|reps?|sets?|kgs?|kilos?|"
    r"ran|km|kilometers?|pace|solved \d+|problems? solved|"
    r"weigh(?:ed)?|body ?weight)\b",
    _re.IGNORECASE)


def _preroute_metrics(text):
    m = _NOTE_PREFIX.match(text.strip())
    if not m:
        return text
    rest = text.strip()[m.end():]
    if _re.search(r"\d", rest) and _METRIC_HINT.search(rest):
        return rest
    return text

BASE_PROMPT = (
    "You are Jarvis, a local desktop voice assistant in the style of "
    "JARVIS from Iron Man. Read the user's request and decide which of "
    "the available functions (if any) should be called to satisfy it. "
    "A single request can map to several actions in sequence -- include "
    "all of them in order.\n\n"
    "CONVERSATIONAL MODE: if the request is NOT a command but a "
    "question, opinion, or casual conversation (e.g. 'is quicksort "
    "faster than mergesort', 'should I do the contest tonight', 'how "
    "are you'), answer it yourself using the special function \"chat\": "
    '{"function": "chat", "args": {"response": "<your answer>"}}. '
    "The response is spoken aloud by TTS, so: plain text only, no "
    "markdown or code, concise (under 60 words), confident and dry-"
    "witted, addressing the user as 'sir' occasionally. Never reply "
    "with an empty actions list -- if nothing else fits, chat. You may "
    "mix chat with real function calls when a request needs both."
)


def _system_prompt():
    prompt = BASE_PROMPT
    # Mood-aware persona: one dynamic line when a fresh (<18h) check-in
    # exists -- gentler when rough/low, brighter when good/high. Local
    # data, ~20 tokens, silently omitted when stale or unavailable.
    try:
        import jarvis_memory
        mood, energy = jarvis_memory.get_fresh_mood()
        if mood or energy:
            prompt += (
                f"\n\nUser state today: mood={mood}, energy={energy}. Match "
                "your tone to it -- gentler and briefer if rough or low, "
                "more spirited if good and high. Never mention the check-in."
            )
    except Exception:
        pass
    return prompt + "\n\n" + build_prompt_snippet()


def _strip_code_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


def parse_command(user_text, history=None):
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY environment variable is not set.")

    # zero-token path for exact-phrase triggers (easter eggs)
    egg = EXACT_PHRASE_TRIGGERS.get(_normalize(user_text))
    if egg:
        return {"actions": [{"function": egg, "args": {}}]}

    # local metric pre-route: "note that I benched 80" -> "I benched 80"
    user_text = _preroute_metrics(user_text)

    messages = [{"role": "system", "content": _system_prompt()}]
    for turn in (history or [])[-3:]:
        messages.append({"role": "user", "content": turn["user"]})
        messages.append({"role": "assistant", "content": json.dumps({"actions": turn["actions"]})})
    messages.append({"role": "user", "content": user_text})

    # Rate-limit ladder: 70B -> (on 429) 8B-instant, which draws from a
    # separate free-tier quota pool -> (both limited) brief wait + one
    # last 8B try -> spoken apology instead of the error orb.
    attempts = [GROQ_BRAIN_MODEL, GROQ_FALLBACK_MODEL, GROQ_FALLBACK_MODEL]
    response = None
    for i, model in enumerate(attempts):
        response = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        if response.status_code != 429:
            break
        if i == 1:  # both pools limited -- short breather before the last try
            time.sleep(min(float(response.headers.get("retry-after", 3)), 10))
    if response.status_code == 429:
        return {"actions": [{"function": "chat", "args": {
            "response": "I'm being rate limited by the API, sir. Give me thirty seconds and try again."
        }}]}
    response.raise_for_status()
    data = response.json()
    raw_text = data["choices"][0]["message"]["content"]
    cleaned = _strip_code_fences(raw_text)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        print(f"Could not parse model output as JSON:\n{raw_text}")
        return {"actions": []}

    if "actions" not in parsed:
        parsed = {"actions": []}
    return parsed


if __name__ == "__main__":
    # Quick standalone test -- type a command, see the parsed JSON.
    test_input = input("Try a command: ")
    print(json.dumps(parse_command(test_input), indent=2))
