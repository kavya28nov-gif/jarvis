"""
test_core_memory.py -- self-editing core memory: remember/replace/forget,
temporal invalidation (facts are superseded, never deleted), history
queries, and the prompt-injection block. Isolated from the real memory
DB via a per-test temp file.
"""

import pytest

import core_memory


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(core_memory, "DB_PATH", str(tmp_path / "core.db"))
    core_memory.init_db()


def test_remember_and_list():
    out = core_memory.remember_fact("My bench PR is 80 kg", subject="bench pr")
    assert "Committed" in out
    listed = core_memory.list_facts()
    assert "bench pr" in listed and "80" in listed


def test_same_subject_replaces_but_keeps_history():
    core_memory.remember_fact("My bench PR is 80 kg", subject="bench pr")
    out = core_memory.remember_fact("My bench PR is 85 kg", subject="bench pr")
    assert "Updated" in out and "80" in out  # announces what it replaced

    # only the new fact is current...
    block = core_memory.core_block()
    assert "85" in block and "80" not in block
    # ...but the old one survives in history with its validity window
    hist = core_memory.fact_history("bench pr")
    assert "80" in hist and "85" in hist and "since" in hist


def test_duplicate_fact_is_a_noop():
    core_memory.remember_fact("Exam on August 3rd", subject="exam date")
    out = core_memory.remember_fact("Exam on August 3rd", subject="exam date")
    assert "Already on record" in out
    assert core_memory.fact_history("exam date").count("Exam on August") == 1


def test_forget_invalidates_not_deletes():
    core_memory.remember_fact("Sister's name is Ana", subject="sister name")
    out = core_memory.forget_fact("sister")
    assert "Struck" in out
    assert "sister" not in core_memory.core_block()
    # history still answers
    assert "Ana" in core_memory.fact_history("sister")


def test_missing_args_ask_back():
    assert "Remember what" in core_memory.remember_fact("")
    assert "Forget what" in core_memory.forget_fact(None)
    assert "History of what" in core_memory.fact_history("  ")
    assert "empty" in core_memory.list_facts()


def test_subject_defaults_to_fact_slug():
    core_memory.remember_fact("Favorite editor is Vim")
    assert "favorite editor is vim" in core_memory.core_block().lower()


def test_core_block_bounded_and_drops_oldest():
    for i in range(60):
        core_memory.remember_fact(f"Fact number {i} with some padding text",
                                  subject=f"subject {i}")
    block = core_memory.core_block(max_chars=300)
    assert len(block) <= 300 + len("[CORE MEMORY] Durable facts Jarvis chose to keep:\n")
    assert "Fact number 59" in block      # newest survives
    assert "Fact number 0 " not in block  # oldest truncated away


def test_core_block_empty_when_no_facts():
    assert core_memory.core_block() == ""


def test_reflection_facts_are_marked_observed():
    core_memory.remember_fact("User skips Friday gym", subject="friday gym",
                              source="reflection")
    core_memory.remember_fact("Bench PR is 85", subject="bench pr")
    listed = core_memory.list_facts()
    assert "User skips Friday gym (observed)" in listed
    assert "Bench PR is 85;" in listed or listed.endswith("Bench PR is 85.")
