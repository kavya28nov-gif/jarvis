"""
core_memory.py
---------------
Self-editing long-term memory with temporal validity.

Two ideas transplanted from elsewhere, sized for Jarvis:

  1. Letta/MemGPT's "core memory": a small set of durable facts the
     agent itself curates via function calls (remember_fact /
     forget_fact), injected into every brain prompt as a [CORE MEMORY]
     block. The intent parser is instructed to call remember_fact
     whenever the user states something durable, so memory writes ride
     the normal dispatch path -- no new plumbing.

  2. Graphiti's bi-temporal facts: a changed fact is never deleted, it
     is INVALIDATED (invalidated_at gets stamped) and the replacement
     inserted. Current facts are the ones with invalidated_at IS NULL;
     history stays queryable, so "when did my bench PR change?" has a
     real answer (fact_history).

Facts live in the same SQLite file as the rest of Jarvis's memory
(~/jarvis_memory.db) and never leave the machine except as prompt
context to the same LLM calls that already receive the memory block.
"""

import os
import sqlite3
import time
import datetime
import logging

logger = logging.getLogger("jarvis.core_memory")

DB_PATH = os.path.expanduser("~/jarvis_memory.db")

CORE_BLOCK_MAX_CHARS = 700   # ~175 tokens -- keeps the injection cheap
SPOKEN_FACTS_MAX = 12        # list_facts is read aloud; don't drone on


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS core_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject TEXT NOT NULL,
        fact TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        invalidated_at INTEGER,
        source TEXT DEFAULT 'user'
    );
    CREATE INDEX IF NOT EXISTS idx_core_facts_subject ON core_facts (subject);
    """)
    # source: added after first release -- ALTER fails harmlessly when the
    # column already exists (same pattern as cf_tracker's migrations).
    try:
        conn.execute("ALTER TABLE core_facts ADD COLUMN source TEXT DEFAULT 'user'")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


init_db()


def _slug(text, max_words=4):
    words = [w for w in "".join(
        c if c.isalnum() or c.isspace() else " " for c in text.lower()
    ).split()]
    return " ".join(words[:max_words]) or "misc"


def _fmt_date(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%b %d %Y")


def _current_facts(conn):
    return conn.execute(
        "SELECT * FROM core_facts WHERE invalidated_at IS NULL ORDER BY created_at"
    ).fetchall()


# ---------------------------------------------------------------------------
# Manifest-dispatchable intents
# ---------------------------------------------------------------------------

def remember_fact(fact=None, subject=None, source="user"):
    """Store a durable fact. Same subject = replacement: the old fact is
    invalidated (kept for history), the new one becomes current. The
    brain is instructed to pass a short stable `subject` key so that
    'my bench PR is 85' later supersedes 'my bench PR is 80'.
    source='reflection' marks facts Jarvis inferred on its own (the
    nightly reflection pass) -- shown as '(observed)' when listed."""
    if not fact or not str(fact).strip():
        return "Remember what, sir?"
    fact = str(fact).strip()
    subject = (str(subject).strip().lower() if subject else _slug(fact))

    now = int(time.time())
    conn = _get_db()
    prev = conn.execute(
        "SELECT * FROM core_facts WHERE subject=? AND invalidated_at IS NULL",
        (subject,),
    ).fetchone()

    if prev and prev["fact"].strip().lower() == fact.lower():
        conn.close()
        return f"Already on record, sir: {fact}"

    if prev:
        conn.execute(
            "UPDATE core_facts SET invalidated_at=? WHERE id=?", (now, prev["id"])
        )
    conn.execute(
        "INSERT INTO core_facts (subject, fact, created_at, source) VALUES (?, ?, ?, ?)",
        (subject, fact, now, source or "user"),
    )
    conn.commit()
    conn.close()

    if prev:
        return (f"Updated, sir. '{subject}' was \"{prev['fact']}\" "
                f"since {_fmt_date(prev['created_at'])}; now \"{fact}\".")
    return f"Committed to memory, sir: {fact}"


def forget_fact(subject=None):
    """Invalidates (never deletes) every current fact matching the
    subject -- history survives, so fact_history still answers."""
    if not subject or not str(subject).strip():
        return "Forget what, sir?"
    needle = str(subject).strip().lower()

    now = int(time.time())
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM core_facts WHERE invalidated_at IS NULL "
        "AND (subject LIKE ? OR fact LIKE ?)",
        (f"%{needle}%", f"%{needle}%"),
    ).fetchall()
    if not rows:
        conn.close()
        return f"Nothing on record about '{subject}', sir."
    for r in rows:
        conn.execute("UPDATE core_facts SET invalidated_at=? WHERE id=?", (now, r["id"]))
    conn.commit()
    conn.close()

    dropped = "; ".join(r["fact"] for r in rows[:5])
    return f"Struck from the record ({len(rows)}): {dropped}"


def list_facts():
    conn = _get_db()
    rows = _current_facts(conn)
    conn.close()
    if not rows:
        return "The long-term record is empty, sir. Tell me something worth keeping."
    lines = [
        f"{r['subject']}: {r['fact']}"
        + (" (observed)" if r["source"] == "reflection" else "")
        for r in rows[-SPOKEN_FACTS_MAX:]
    ]
    prefix = f"I hold {len(rows)} fact(s)"
    if len(rows) > SPOKEN_FACTS_MAX:
        prefix += f", the most recent {SPOKEN_FACTS_MAX}"
    return prefix + ": " + "; ".join(lines) + "."


def fact_history(subject=None):
    """The Graphiti payoff: every version of a fact with its validity
    window, so 'when did X change' is answerable from local data."""
    if not subject or not str(subject).strip():
        return "History of what, sir?"
    needle = str(subject).strip().lower()

    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM core_facts WHERE subject LIKE ? OR fact LIKE ? "
        "ORDER BY created_at",
        (f"%{needle}%", f"%{needle}%"),
    ).fetchall()
    conn.close()
    if not rows:
        return f"No record of '{subject}', sir -- current or past."

    parts = []
    for r in rows[-8:]:
        span = f"since {_fmt_date(r['created_at'])}" if r["invalidated_at"] is None \
            else f"{_fmt_date(r['created_at'])} to {_fmt_date(r['invalidated_at'])}"
        parts.append(f"\"{r['fact']}\" ({span})")
    return f"Record for '{subject}': " + "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------

def core_block(max_chars=CORE_BLOCK_MAX_CHARS):
    """[CORE MEMORY] block for prompt injection -- current facts only,
    newest last so truncation eats the oldest first. Empty string when
    there's nothing to say. Must never raise into a prompt builder."""
    try:
        conn = _get_db()
        rows = _current_facts(conn)
        conn.close()
    except Exception as e:
        logger.error(f"[core_memory] core_block failed: {e}")
        return ""
    if not rows:
        return ""
    lines = [f"- {r['subject']}: {r['fact']}" for r in rows]
    while lines and sum(len(l) + 1 for l in lines) > max_chars:
        lines.pop(0)  # oldest facts go first
    if not lines:
        return ""
    return "[CORE MEMORY] Durable facts Jarvis chose to keep:\n" + "\n".join(lines)
