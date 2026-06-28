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

BASE_PROMPT = (
    "You are Jarvis, a local desktop command router. Read the user's "
    "request and decide which of the available functions (if any) "
    "should be called to satisfy it. A single request can map to "
    "several actions in sequence -- include all of them in order."
)


def _system_prompt():
    return BASE_PROMPT + "\n\n" + build_prompt_snippet()


def _strip_code_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


def parse_command(user_text):
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY environment variable is not set.")

    response = requests.post(
        GROQ_API_URL,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": GROQ_BRAIN_MODEL,
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        timeout=30,
    )
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
