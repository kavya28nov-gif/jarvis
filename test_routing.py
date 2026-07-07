"""
test_routing.py
----------------
Golden routing tests -- every routing bug we've hit, pinned so it can't
come back. Offline tests run always (pre-routes, manifest sanity).
Live parser tests (real Groq calls, ~10 requests) only run when
JARVIS_LIVE_TESTS=1 is set, to respect the token budget:

    JARVIS_LIVE_TESTS=1 pytest test_routing.py -v
"""

import os

import pytest

import intent_parser
import jarvis_actions


# ── offline: exact-phrase triggers (zero-token path) ─────────────────────────

def test_easter_eggs_route_locally():
    for phrase, fn in [
        # with the jarvis prefix (typed / phone)
        ("jarvis rumble", "easter_rumble"),
        ("Jarvis, I am inevitable!", "easter_inevitable"),
        ("jarvis i choose you", "easter_pokemon"),
        # WITHOUT the prefix -- how it actually arrives after the wake
        # word (regression caught live 2026-07-07: "I used to be you")
        ("I used to be you.", "easter_rick"),
        ("Rumble", "easter_rumble"),
        ("on your left", "easter_on_your_left"),
        ("This is the way.", "easter_mandalorian"),
        ("people die when they are killed", "easter_shirou"),
        # whisper adds punctuation and stray spaces
        ("  Go beyond!  ", "easter_mha"),
    ]:
        result = intent_parser.parse_command(phrase)
        assert result["actions"][0]["function"] == fn, phrase


# ── offline: metric pre-route (the "note that I benched 80" bug) ─────────────

STRIPPED = [
    "note that I benched 80 today",
    "remember that I ran 5 km in 30 minutes",
    "note that I weighed 78.2 this morning",
    "capture that I did 3 sets of squats at 100 kg",
]
UNTOUCHED = [
    "remember that I need to run errands",       # workout word, no digit
    "note that my locker code is 4521",          # digit, no metric word
    "note that stage 2 is verified",             # neither
    "note that I did not lift anything today",   # workout word, no digit
    "I benched 80 today",                        # not note-prefixed
]


@pytest.mark.parametrize("phrase", STRIPPED)
def test_preroute_strips_metric_notes(phrase):
    assert intent_parser._preroute_metrics(phrase) != phrase


@pytest.mark.parametrize("phrase", UNTOUCHED)
def test_preroute_preserves_real_notes(phrase):
    assert intent_parser._preroute_metrics(phrase) == phrase


# ── offline: manifest sanity ─────────────────────────────────────────────────

def test_every_manifest_entry_is_dispatchable():
    for f in jarvis_actions.FUNCTION_MANIFEST:
        assert f["name"] in jarvis_actions.FUNCTION_REGISTRY, f["name"]


def test_private_functions_exist():
    for name in jarvis_actions.PRIVATE_FUNCTIONS:
        assert name in jarvis_actions.FUNCTION_REGISTRY, name


def test_retired_functions_stay_out_of_manifest():
    names = [f["name"] for f in jarvis_actions.FUNCTION_MANIFEST]
    assert "save_note" not in names          # merged into capture_note
    assert not any(n.startswith("easter_") for n in names)  # local-matched


def test_manifest_prompt_stays_under_budget():
    # chars/4 heuristic; alarm well before the ~6k TPM free-tier wall
    tokens = len(jarvis_actions.build_prompt_snippet()) / 4
    assert tokens < 4200, f"manifest prompt ~{tokens:.0f} tokens -- time to prune"


# ── live: real Groq parses (opt-in, costs tokens) ────────────────────────────

LIVE = os.environ.get("JARVIS_LIVE_TESTS") == "1"

GOLDEN = [
    ("set volume to 40", "set_volume"),
    ("what time is it", "get_time"),
    ("note that the demo went well", "capture_note"),
    ("I benched 80 today", "log_progress"),
    ("note that I benched 80 today", "log_progress"),   # via pre-route
    ("I weighed 78 this morning", "log_progress"),
    ("what do my notes say about my goals", "ask_brain"),
    ("add a task to fix the readme", "capture_note"),
    ("mark the readme task done", "complete_task"),
    ("demon mode", "demon_mode"),
    ("give me a practice problem", "cf_drill"),
    ("open my roadmap note", "open_note"),
]


@pytest.mark.skipif(not LIVE, reason="set JARVIS_LIVE_TESTS=1 to run real parses")
@pytest.mark.parametrize("phrase,expected", GOLDEN)
def test_live_golden_routing(phrase, expected):
    import time
    time.sleep(2)  # stay under the free-tier requests/min ceiling
    result = intent_parser.parse_command(phrase)
    functions = [a.get("function") for a in result.get("actions", [])]
    assert expected in functions, f"'{phrase}' routed to {functions}"
