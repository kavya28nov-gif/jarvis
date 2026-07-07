"""
test_emotion.py -- the affect layer: appraisal, decay, drives, caps,
and the design rules (expression-only, explainable, bounded).
State is isolated from the real memory DB via an in-memory KV.
"""

import time

import pytest

import emotion


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    kv = {}
    monkeypatch.setattr(emotion.jarvis_memory, "set_memory",
                        lambda k, v: kv.__setitem__(k, str(v)))
    monkeypatch.setattr(emotion.jarvis_memory, "get_memory",
                        lambda k, default=None: kv.get(k, default))
    return kv


def test_appraisal_moves_the_vector():
    st0 = emotion.load_state()
    st1 = emotion.appraise("complete_task", None, "Done: x. Marked complete.")
    assert st1["valence"] > st0["valence"]
    assert st1["order"] < st0["order"] + 0.01  # satisfied, not raised
    assert "a task got finished" in st1["reasons"][0]


def test_pr_bonus_and_error_penalty():
    happy = emotion.appraise("log_progress", None,
                             "Logged... NEW PERSONAL RECORD on bench. Outstanding, sir!")
    v_happy = happy["valence"]
    grumpy = emotion.appraise("set_volume", None, "Error running set_volume: boom")
    assert grumpy["valence"] < v_happy


def test_everything_is_clamped():
    for _ in range(50):
        st = emotion.appraise("complete_task", None, "done")
    assert st["valence"] <= 1.0 and st["order"] >= 0.0
    for _ in range(50):
        st = emotion.appraise("x", None, "Error running x: boom")
    assert st["valence"] >= -1.0


def test_emotions_decay_but_drives_grow():
    st = emotion.appraise("complete_task", None, "done")
    st["valence"] = 0.8
    st["curiosity"] = 0.2
    st["updated"] = time.time() - 12 * 3600  # 12h ago = 2 half-lives
    emotion.save_state(st)
    now = emotion.load_state()
    emotion._decay(now)
    assert now["valence"] < 0.25          # relaxed toward baseline
    assert now["curiosity"] > 0.3         # hunger grew
    assert now["curiosity"] <= 1.0


def test_capture_satisfies_curiosity():
    st = emotion.load_state()
    st["curiosity"] = 0.9
    emotion.save_state(st)
    after = emotion.appraise("capture_note", None, "")
    assert after["curiosity"] < 0.7


def test_tick_order_drive_tracks_world():
    st = emotion.tick(open_tasks=6, oldest_task_days=10)
    assert st["order"] > 0.4
    st = emotion.tick(open_tasks=0, oldest_task_days=0)
    st = emotion.tick(open_tasks=0, oldest_task_days=0)
    assert st["order"] < 0.3              # relaxes once the world is tidy


def test_rough_mood_earns_sympathy():
    before = emotion.load_state()
    after = emotion.tick(user_mood="rough")
    assert after["warmth"] > before["warmth"]
    assert after["valence"] < before["valence"]
    assert any("rough" in r for r in after["reasons"])


def test_voice_answers_are_explainable_and_bounded():
    emotion.appraise("ask_brain", None, "answered")
    feel = emotion.how_do_you_feel()
    assert feel.endswith(".") and "Lately:" in feel
    st = emotion.load_state()
    st.update(curiosity=0.1, order=0.1, growth=0.1)
    emotion.save_state(st)
    assert "well fed" in emotion.what_do_you_want()
    st.update(curiosity=0.95)
    emotion.save_state(st)
    want = emotion.what_do_you_want()
    assert "Feed me a thought" in want
    # rule 3: never guilt/leverage vocabulary
    for banned in ("you never", "if you don't", "unless you"):
        assert banned not in want.lower()


def test_disposition_phrases():
    st = dict(emotion._DEFAULT_STATE, valence=0.5, arousal=0.7, reasons=[])
    assert "spirited" in emotion.disposition(st)
    st.update(valence=-0.5, arousal=0.2)
    assert "subdued" in emotion.disposition(st)
    st.update(curiosity=0.9)
    assert "craving" in emotion.disposition(st)
