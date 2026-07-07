"""
test_vault_integration.py
--------------------------
Tests for the Obsidian vault integration (Stages 1-2 so far). Run:

    .venv/Scripts/python.exe -m pytest test_vault_integration.py -v

Everything runs against a temp vault -- the real vault is never touched.
"""

import datetime
import os
import tempfile

import pytest

import jarvis_actions
import jarvis_memory
import cf_tracker


TODAY = datetime.date.today().isoformat()


@pytest.fixture
def temp_vault():
    """Points the config at a throwaway vault for the duration of a test."""
    old = jarvis_actions._USER_CONFIG.get("vault_path")
    tmp = tempfile.mkdtemp(prefix="jarvis_vault_test_")
    jarvis_actions._USER_CONFIG["vault_path"] = tmp
    yield tmp
    jarvis_actions._USER_CONFIG["vault_path"] = old


# ── Stage 1: capture_note ────────────────────────────────────────────────────

def test_capture_creates_file_with_frontmatter(temp_vault):
    jarvis_actions.capture_note("first note")
    path = os.path.join(temp_vault, "inbox", f"{TODAY}.md")
    content = open(path, encoding="utf-8").read()
    assert content.startswith(f"---\ndate: {TODAY}\ntype: voice-inbox\n---\n")
    assert "(voice) first note" in content


def test_capture_appends_without_duplicating_frontmatter(temp_vault):
    jarvis_actions.capture_note("one")
    jarvis_actions.capture_note("two")
    content = open(os.path.join(temp_vault, "inbox", f"{TODAY}.md"),
                   encoding="utf-8").read()
    assert content.count("type: voice-inbox") == 1
    assert content.count("(voice)") == 2


def test_capture_survives_missing_trailing_newline(temp_vault):
    jarvis_actions.capture_note("one")
    path = os.path.join(temp_vault, "inbox", f"{TODAY}.md")
    # simulate a manual edit that dropped the final newline
    content = open(path, encoding="utf-8").read().rstrip("\n")
    open(path, "w", encoding="utf-8").write(content)
    jarvis_actions.capture_note("two")
    lines = open(path, encoding="utf-8").read().splitlines()
    voice_lines = [l for l in lines if "(voice)" in l]
    assert len(voice_lines) == 2, "entries glued onto one line"


def test_capture_unconfigured_vault_fails_gracefully():
    old = jarvis_actions._USER_CONFIG.get("vault_path")
    jarvis_actions._USER_CONFIG["vault_path"] = None
    try:
        result = jarvis_actions.capture_note("x")
        assert "not configured" in result.lower()
    finally:
        jarvis_actions._USER_CONFIG["vault_path"] = old


def test_save_note_is_alias_and_txt_not_written(temp_vault):
    assert "save_note" not in [f["name"] for f in jarvis_actions.FUNCTION_MANIFEST]
    assert "save_note" in jarvis_actions.FUNCTION_REGISTRY
    jarvis_actions.FUNCTION_REGISTRY["save_note"]("alias check")
    content = open(os.path.join(temp_vault, "inbox", f"{TODAY}.md"),
                   encoding="utf-8").read()
    assert "alias check" in content
    assert not os.path.exists(jarvis_actions.NOTES_FILE)


def test_capture_content_redacted_from_event_log(temp_vault):
    marker = "REDACTION-TEST-MARKER-8271"
    jarvis_actions.run_function("capture_note", {"text": marker})
    for e in jarvis_memory.get_recent_events(3):
        assert marker not in (e["entities"] or "")
        assert marker not in (e["outcome"] or "")


# ── Stage 2: journal mirror ──────────────────────────────────────────────────

def _make_agent():
    import heartbeat_agent
    return heartbeat_agent.HeartbeatAgent()


def test_journal_written_and_idempotent(temp_vault):
    agent = _make_agent()
    agent._write_vault_journal()
    path = os.path.join(temp_vault, "journal", f"{TODAY}.md")
    first = open(path, encoding="utf-8").read()
    assert "type: jarvis-journal" in first
    for section in ("Day Summary", "Codeforces", "Mood", "Streaks / PRs"):
        assert f"## {section}" in first, f"missing section {section}"

    agent._write_vault_journal()  # re-run must not duplicate sections
    second = open(path, encoding="utf-8").read()
    assert second.count("## Day Summary") == 1
    assert second.count("type: jarvis-journal") == 1


def test_journal_never_contains_raw_private_content(temp_vault):
    marker = "PRIVATE-VAULT-MARKER-5150"
    jarvis_actions.run_function("read_clipboard", {})  # a private fn ran today
    jarvis_memory.log_event("save_note", {"redacted": True}, "[content redacted for privacy]")
    agent = _make_agent()
    agent._write_vault_journal()
    content = open(os.path.join(temp_vault, "journal", f"{TODAY}.md"),
                   encoding="utf-8").read()
    assert marker not in content
    assert "redacted" not in content.lower() or "(no distillation" in content


def test_journal_unconfigured_vault_no_crash():
    old = jarvis_actions._USER_CONFIG.get("vault_path")
    jarvis_actions._USER_CONFIG["vault_path"] = None
    try:
        _make_agent()._write_vault_journal()  # must simply not raise
    finally:
        jarvis_actions._USER_CONFIG["vault_path"] = old


# ── September items: phone capture / dictation draft / screen note ──────────

def test_phone_capture_endpoint(temp_vault):
    import phone_server
    client = phone_server.app.test_client()
    headers = {"X-Jarvis-Token": phone_server.PHONE_SERVER_TOKEN}
    r = client.post("/capture", json={"text": "phone thought", "todo": False},
                    headers=headers)
    assert r.status_code == 200 and r.get_json()["status"] == "ok"
    r2 = client.post("/capture", json={"text": "phone task", "todo": True},
                     headers=headers)
    assert r2.status_code == 200
    content = open(os.path.join(temp_vault, "inbox", f"{TODAY}.md"),
                   encoding="utf-8").read()
    assert "(phone) phone thought" in content
    assert "- [ ] " in content and "phone task" in content
    # no token -> rejected
    assert client.post("/capture", json={"text": "x"}).status_code == 401


def test_dictate_to_note_creates_draft(temp_vault, monkeypatch):
    import jarvis_actions as ja
    utterances = iter(["First passage of the draft.",
                       "Second passage here.", "stop dictation"])
    import voice_input
    monkeypatch.setattr(voice_input, "listen",
                        lambda max_wait=None: next(utterances, None))
    result = ja.dictate_to_note()
    assert "2 passage(s)" in result
    drafts = [f for f in os.listdir(os.path.join(temp_vault, "inbox"))
              if f.startswith("draft-")]
    assert len(drafts) == 1
    content = open(os.path.join(temp_vault, "inbox", drafts[0]),
                   encoding="utf-8").read()
    assert "type: draft" in content
    assert "First passage of the draft.\n\nSecond passage here." in content
    assert "stop dictation" not in content


def test_capture_screen_note(temp_vault, monkeypatch):
    monkeypatch.setattr(jarvis_actions, "describe_screen",
                        lambda: "A code editor showing a Python file.")
    result = jarvis_actions.capture_screen_note(comment="this bug is cursed")
    assert "Screen noted" in result
    content = open(os.path.join(temp_vault, "inbox", f"{TODAY}.md"),
                   encoding="utf-8").read()
    assert "(screen) [screen] A code editor showing a Python file. | my comment: this bug is cursed" in content


def test_new_capture_functions_are_private():
    for fn in ("dictate_to_note", "capture_screen_note"):
        assert fn in jarvis_actions.PRIVATE_FUNCTIONS


# ── vault tasks ──────────────────────────────────────────────────────────────

def test_task_lifecycle(temp_vault):
    jarvis_actions.capture_note("buy chalk for the gym", todo=True)
    jarvis_actions.capture_note("finish the chess engine readme", todo=True)
    jarvis_actions.capture_note("a plain note, not a task")

    listed = jarvis_actions.list_tasks()
    assert "2 open task(s)" in listed
    assert "chalk" in listed and "chess engine" in listed
    assert "plain note" not in listed

    result = jarvis_actions.complete_task("chess engine readme")
    assert "Marked complete" in result

    content = open(os.path.join(temp_vault, "inbox", f"{TODAY}.md"),
                   encoding="utf-8").read()
    assert content.count("- [x]") == 1
    assert "chess engine readme ✅" in content.split("- [x]")[1].splitlines()[0] or \
           "chess engine" in [l for l in content.splitlines() if "- [x]" in l][0]
    assert "1 open task(s)" in jarvis_actions.list_tasks()


def test_complete_task_no_match(temp_vault):
    jarvis_actions.capture_note("water the plants", todo=True)
    assert "No open task matching" in jarvis_actions.complete_task("quantum flux capacitor")


# ── Stage 3: ask_brain / vault_search ────────────────────────────────────────

def _seed_vault(root):
    wiki = os.path.join(root, "wiki", "concepts")
    os.makedirs(wiki)
    with open(os.path.join(wiki, "Goals.md"), "w", encoding="utf-8") as f:
        f.write("---\ntype: concept\n---\n\n# Goals\n\n"
                "The plan is to ship two finished AI projects with public "
                "repos and demos before the end of the year.\n")
    journal = os.path.join(root, "journal")
    os.makedirs(journal)
    with open(os.path.join(journal, "2026-07-01.md"), "w", encoding="utf-8") as f:
        f.write("---\ntype: jarvis-journal\n---\n\n"
                "Decided the Spotify cooldown should be six seconds because "
                "the microphone kept hearing the music.\n")


@pytest.fixture
def searchable_vault(temp_vault, monkeypatch):
    import vault_search
    _seed_vault(temp_vault)
    # isolate the index cache from the real one
    monkeypatch.setattr(vault_search, "INDEX_PATH",
                        __import__("pathlib").Path(temp_vault) / "test_index.json")
    return temp_vault


def test_vault_search_finds_relevant_chunk(searchable_vault):
    import vault_search
    hits = vault_search.search(searchable_vault, "spotify cooldown six seconds")
    assert hits, "no hits for a known query"
    assert hits[0]["page_path"].startswith("journal/")
    assert "six seconds" in hits[0]["snippet"]

    hits2 = vault_search.search(searchable_vault, "ship AI projects public repos")
    assert hits2 and hits2[0]["page_path"] == "wiki/concepts/Goals.md"


def test_ask_brain_ollama_down_graceful(searchable_vault, monkeypatch):
    def _boom(*a, **k):
        raise ConnectionError("ollama down")
    monkeypatch.setattr(jarvis_actions.requests, "post", _boom)
    result = jarvis_actions.ask_brain("what did I decide about spotify")
    assert "local model" in result.lower() or "ollama" in result.lower()
    assert "Traceback" not in result


def test_ask_brain_prompt_shape(searchable_vault, monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"message": {"content": "<think>hmm</think>Six seconds, sir."}}

    def _fake_post(url, json=None, timeout=None, **k):
        captured["url"] = url
        captured["payload"] = json
        return FakeResp()

    monkeypatch.setattr(jarvis_actions.requests, "post", _fake_post)
    result = jarvis_actions.ask_brain("what did I decide about the spotify cooldown")
    assert captured["url"].startswith("http://127.0.0.1:11434")
    assert captured["payload"]["model"] == "qwen3:8b"
    system = captured["payload"]["messages"][0]["content"]
    assert "ONLY from the provided notes" in system
    assert "I don't have that in your notes" in system
    user_msg = captured["payload"]["messages"][1]["content"]
    assert "Notes:" in user_msg and "six seconds" in user_msg
    assert result == "Six seconds, sir."  # <think> stripped


def test_ask_brain_unconfigured_vault():
    old = jarvis_actions._USER_CONFIG.get("vault_path")
    jarvis_actions._USER_CONFIG["vault_path"] = None
    try:
        assert "not configured" in jarvis_actions.ask_brain("anything").lower()
    finally:
        jarvis_actions._USER_CONFIG["vault_path"] = old


# ── Loose End A: bodyweight + named metrics ─────────────────────────────────

@pytest.fixture
def temp_progress_log(monkeypatch, tmp_path):
    monkeypatch.setattr(jarvis_actions, "PROGRESS_LOG_PATH",
                        str(tmp_path / "progress.json"))
    return tmp_path


def test_positional_weights_still_work(temp_progress_log):
    import datetime as dt
    day = dt.date.today().strftime("%A")
    routine = jarvis_actions.WEEKLY_ROUTINE.get(day, [])
    if not routine:
        pytest.skip("rest day -- no positional routine to test")
    jarvis_actions.log_progress(weights=[50, 60])
    entry = jarvis_actions._load_progress_log()[dt.date.today().isoformat()]
    assert entry["exercises"][routine[0]] == 50.0
    assert entry["exercises"][routine[1]] == 60.0


def test_named_metric_and_bodyweight(temp_progress_log):
    import datetime as dt
    msg = jarvis_actions.log_progress(exercises={"Weighted dips": 30}, bodyweight=78.2)
    assert "bodyweight 78.2kg" in msg
    entry = jarvis_actions._load_progress_log()[dt.date.today().isoformat()]
    assert entry["exercises"]["Weighted dips"] == 30.0
    assert entry["bodyweight"] == 78.2
    # same-day update overwrites, doesn't duplicate
    jarvis_actions.log_progress(bodyweight=78.0)
    entry = jarvis_actions._load_progress_log()[dt.date.today().isoformat()]
    assert entry["bodyweight"] == 78.0
    assert entry["exercises"]["Weighted dips"] == 30.0  # untouched


def test_bodyweight_series_in_show_progress(temp_progress_log):
    import datetime as dt, json
    today = dt.date.today()
    data = {
        (today - dt.timedelta(days=2)).isoformat(): {"exercises": {}, "bodyweight": 79.0},
        (today - dt.timedelta(days=1)).isoformat(): {"exercises": {}, "bodyweight": 78.5},
        today.isoformat(): {"exercises": {}, "bodyweight": 78.2},
    }
    jarvis_actions._save_progress_log(data)
    out = jarvis_actions.show_progress(7)
    assert "Bodyweight (kg): down (79.0 -> 78.2)" in out


def test_journal_includes_bodyweight(temp_vault, temp_progress_log):
    import datetime as dt
    jarvis_actions.log_progress(bodyweight=78.2)
    import heartbeat_agent
    heartbeat_agent.HeartbeatAgent()._write_vault_journal()
    content = open(os.path.join(temp_vault, "journal", f"{TODAY}.md"),
                   encoding="utf-8").read()
    assert "Bodyweight: 78.2 kg" in content


def test_preroute_weighin():
    from intent_parser import _preroute_metrics
    assert _preroute_metrics("note that I weighed 78.2 today") == "I weighed 78.2 today"
    assert _preroute_metrics("remember that my weight is 78 kg") == "my weight is 78 kg"


# ── mood-aware persona ───────────────────────────────────────────────────────

def test_fresh_mood_staleness():
    import time as _t
    jarvis_memory.set_memory("last_mood", "rough")
    jarvis_memory.set_memory("last_energy", "low")
    jarvis_memory.set_memory("last_mood_checkin_at", str(_t.time()))
    assert jarvis_memory.get_fresh_mood() == ("rough", "low")
    jarvis_memory.set_memory("last_mood_checkin_at", str(_t.time() - 20 * 3600))
    assert jarvis_memory.get_fresh_mood() == (None, None)


def test_system_prompt_mood_injection():
    import time as _t, intent_parser
    jarvis_memory.set_memory("last_mood", "rough")
    jarvis_memory.set_memory("last_energy", "low")
    jarvis_memory.set_memory("last_mood_checkin_at", str(_t.time()))
    assert "mood=rough" in intent_parser._system_prompt()
    jarvis_memory.set_memory("last_mood_checkin_at", str(_t.time() - 20 * 3600))
    assert "mood=" not in intent_parser._system_prompt().split("Available functions")[0]


# ── opinion loop vault memory ────────────────────────────────────────────────

def test_opinion_vault_memory(searchable_vault):
    import heartbeat_agent
    memory = heartbeat_agent.HeartbeatAgent()._build_vault_memory()
    assert "Goals.md" in memory or "journal/" in memory
    assert len(memory) < 2500  # bounded -- snapshot prompt stays small


# ── routing: metric pre-route ────────────────────────────────────────────────

def test_preroute_strips_note_prefix_for_metrics():
    from intent_parser import _preroute_metrics
    assert _preroute_metrics("note that I benched 80 today") == "I benched 80 today"
    assert _preroute_metrics("remember that I ran 5 km in 30 minutes") == "I ran 5 km in 30 minutes"
    assert _preroute_metrics("note that I did 3 sets of squats at 100") == "I did 3 sets of squats at 100"


def test_preroute_leaves_real_notes_alone():
    from intent_parser import _preroute_metrics
    # no digits -> untouched even with workout words
    assert _preroute_metrics("remember that I need to run errands") == \
        "remember that I need to run errands"
    # digits but no metric keyword -> untouched
    assert _preroute_metrics("note that my locker code is 4521") == \
        "note that my locker code is 4521"
    # plain notes -> untouched
    assert _preroute_metrics("note that stage 2 is verified") == \
        "note that stage 2 is verified"
    # non-note phrasing -> untouched
    assert _preroute_metrics("I benched 80 today") == "I benched 80 today"


def test_cf_debrief_note(temp_vault):
    result = {
        "name": "Test Round 999 (Div. 2)",
        "solved_indexes": ["A", "B"],
        "first_ac_times": {"A": 300, "B": 1500},
        "penalty": 2,
        "attempted_unsolved": ["C", "D"],
    }
    cf_tracker._write_debrief_note(999, result)
    path = os.path.join(temp_vault, "journal", "cf-999-debrief.md")
    content = open(path, encoding="utf-8").read()
    assert "#codeforces" in content
    assert content.count("- [ ] Upsolve") == 2
    assert "https://codeforces.com/contest/999/problem/C" in content
    assert "Solved: A, B" in content
