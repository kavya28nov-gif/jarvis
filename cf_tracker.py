"""
cf_tracker.py
--------------
Codeforces intelligence module. Tracks daily submissions/rating via the
public CF API (no auth needed -- handle is public data), monitors
upcoming/finished contests, analyzes post-contest performance, and
exposes manifest-dispatchable intents. All state lives in a local
SQLite DB (~/jarvis_cf.db) -- never committed to the repo.
"""

import os
import json
import sqlite3
import threading
import time
import datetime
import logging
import webbrowser

import requests

logger = logging.getLogger("jarvis.cf_tracker")

CF_API_BASE = "https://codeforces.com/api"
CF_RATE_LIMIT_SECONDS = 1.2  # CF documents a 1 req/sec cap -- stay safely under it

DB_PATH = os.path.expanduser("~/jarvis_cf.db")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "jarvis_config.json")

DAILY_FETCH_INTERVAL_SECONDS = 24 * 60 * 60

# Only these contest types trigger reminders/auto-open -- skips Div 3/4,
# Kotlin Heroes, April Fools rounds, etc. Edit to taste.
RELEVANT_CONTEST_KEYWORDS = ["Div. 1", "Div. 2", "Div. 1 + Div. 2", "Educational"]

_last_api_call = 0.0
_api_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Config + low-level API plumbing
# ---------------------------------------------------------------------------

def _get_cf_handle():
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return cfg.get("cf_handle") or None


def _rate_limited_get(endpoint, params=None):
    """Fails silently on any error (network, bad handle, CF downtime) --
    callers just treat None as 'nothing to do this cycle, try again next
    heartbeat/daily fetch'."""
    global _last_api_call
    with _api_lock:
        wait = CF_RATE_LIMIT_SECONDS - (time.time() - _last_api_call)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = requests.get(f"{CF_API_BASE}/{endpoint}", params=params or {}, timeout=15)
            _last_api_call = time.time()
            data = resp.json()
            if data.get("status") != "OK":
                logger.error(f"[cf api] {endpoint} non-OK: {data.get('comment')}")
                return None
            return data["result"]
        except Exception as e:
            _last_api_call = time.time()
            logger.error(f"[cf api error] {endpoint}: {e}")
            return None


def _is_relevant_contest(name):
    return any(kw in name for kw in RELEVANT_CONTEST_KEYWORDS)


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# DB migration -- safe to call every startup, only creates what's missing
# ---------------------------------------------------------------------------

def init_db():
    conn = _get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS cf_submissions (
        submission_id INTEGER PRIMARY KEY,
        contest_id INTEGER,
        problem_index TEXT,
        problem_name TEXT,
        rating INTEGER,
        tags TEXT,
        solved_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS cf_rating_history (
        fetched_at INTEGER PRIMARY KEY,
        rating INTEGER,
        delta INTEGER
    );

    CREATE TABLE IF NOT EXISTS cf_contests (
        contest_id INTEGER PRIMARY KEY,
        name TEXT,
        start_time INTEGER,
        participated INTEGER DEFAULT 0,
        notified_60 INTEGER DEFAULT 0,
        notified_15 INTEGER DEFAULT 0,
        opened_at_start INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS cf_contest_results (
        contest_id INTEGER PRIMARY KEY,
        name TEXT,
        solved_indexes TEXT,
        first_ac_times TEXT,
        wrong_submission_count INTEGER,
        analyzed_at INTEGER
    );
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. Daily submission/rating fetch
# ---------------------------------------------------------------------------

def fetch_submissions_and_rating():
    handle = _get_cf_handle()
    if not handle:
        logger.warning("[cf_tracker] no cf_handle set in jarvis_config.json -- skipping fetch")
        return

    conn = _get_db()

    submissions = _rate_limited_get("user.status", {"handle": handle, "from": 1, "count": 200})
    if submissions is not None:
        cur = conn.execute("SELECT MAX(submission_id) FROM cf_submissions")
        last_id = cur.fetchone()[0] or 0
        new_acs = 0
        for sub in submissions:
            if sub.get("verdict") != "OK":
                continue
            sid = sub["id"]
            if sid <= last_id:
                continue
            problem = sub.get("problem", {})
            conn.execute(
                "INSERT OR IGNORE INTO cf_submissions "
                "(submission_id, contest_id, problem_index, problem_name, rating, tags, solved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    problem.get("contestId"),
                    problem.get("index"),
                    problem.get("name"),
                    problem.get("rating"),
                    json.dumps(problem.get("tags", [])),
                    sub.get("creationTimeSeconds"),
                ),
            )
            new_acs += 1
        conn.commit()
        logger.info(f"[cf_tracker] fetched submissions -- {new_acs} new AC(s)")

    rating_info = _rate_limited_get("user.rating", {"handle": handle})
    if rating_info:
        new_rating = rating_info[-1]["newRating"]
        row = conn.execute(
            "SELECT rating FROM cf_rating_history ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()
        prev_rating = row["rating"] if row else new_rating
        delta = new_rating - prev_rating
        conn.execute(
            "INSERT INTO cf_rating_history (fetched_at, rating, delta) VALUES (?, ?, ?)",
            (int(time.time()), new_rating, delta),
        )
        conn.commit()
        logger.info(f"[cf_tracker] rating now {new_rating} (delta {delta:+d})")

    conn.close()


def start_daily_fetch_thread():
    """Separate from the 30-min heartbeat -- runs once immediately, then
    every 24h. Daemon thread, dies with the process."""
    init_db()

    def _loop():
        while True:
            try:
                fetch_submissions_and_rating()
            except Exception as e:
                logger.error(f"[cf_tracker] daily fetch error: {e}")
            time.sleep(DAILY_FETCH_INTERVAL_SECONDS)

    threading.Thread(target=_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# 2 + 3. Heartbeat-driven contest monitor + post-contest analysis
# ---------------------------------------------------------------------------

def analyze_contest(contest_id):
    """Pulls this user's submissions for one specific contest via
    contest.status (more precise than filtering user.status), computes
    solved indexes, first-AC time per problem (relative to contest
    start), and penalty (wrong submissions before that problem's AC)."""
    handle = _get_cf_handle()
    if not handle:
        return None

    subs = _rate_limited_get("contest.status", {"contestId": contest_id, "handle": handle})
    if subs is None:
        return None

    conn = _get_db()
    contest_row = conn.execute(
        "SELECT * FROM cf_contests WHERE contest_id=?", (contest_id,)
    ).fetchone()
    contest_name = contest_row["name"] if contest_row else str(contest_id)
    start_time = contest_row["start_time"] if contest_row else None

    first_ac, wrong_before_ac, solved = {}, {}, set()
    for sub in sorted(subs, key=lambda s: s["creationTimeSeconds"]):
        idx = sub["problem"]["index"]
        if idx in first_ac:
            continue  # already solved -- later submissions don't count
        if sub.get("verdict") == "OK":
            first_ac[idx] = sub["creationTimeSeconds"]
            solved.add(idx)
        else:
            wrong_before_ac[idx] = wrong_before_ac.get(idx, 0) + 1

    penalty = sum(wrong_before_ac.get(idx, 0) for idx in solved)
    times_relative = {
        idx: t - start_time for idx, t in first_ac.items()
    } if start_time else first_ac

    conn.execute(
        "INSERT OR REPLACE INTO cf_contest_results "
        "(contest_id, name, solved_indexes, first_ac_times, wrong_submission_count, analyzed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (contest_id, contest_name, json.dumps(sorted(solved)), json.dumps(times_relative),
         penalty, int(time.time())),
    )
    if contest_row:
        conn.execute("UPDATE cf_contests SET participated=1 WHERE contest_id=?", (contest_id,))
    conn.commit()
    conn.close()

    return {
        "name": contest_name,
        "solved_indexes": sorted(solved),
        "first_ac_times": times_relative,
        "penalty": penalty,
    }


def run_heartbeat_check(on_notify=None, play_alert=None):
    """Called every heartbeat tick (30 min). One contest.list call covers
    all three jobs below to stay within the rate limit:
    (a) T-60/T-15 reminders for upcoming relevant contests
    (b) auto-open the problem set once start time has passed (checked as
        "has start_time passed and not yet opened" rather than an exact
        T=0 match, since a 30-min tick can't land exactly on start)
    (c) analyze any tracked contest that's now FINISHED and not yet
        analyzed
    """
    contests = _rate_limited_get("contest.list", {"gym": "false"})
    if not contests:
        return

    contests_by_id = {c["id"]: c for c in contests}
    conn = _get_db()
    now = int(time.time())

    # (a) upcoming reminders
    upcoming = [
        c for c in contests
        if c.get("phase") == "BEFORE" and _is_relevant_contest(c.get("name", ""))
    ]
    for c in upcoming:
        cid, name, start = c["id"], c["name"], c.get("startTimeSeconds")
        if start is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO cf_contests (contest_id, name, start_time) VALUES (?, ?, ?)",
            (cid, name, start),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM cf_contests WHERE contest_id=?", (cid,)).fetchone()
        seconds_until = start - now

        if 0 < seconds_until <= 3600 and not row["notified_60"]:
            if on_notify:
                on_notify(f"Contest in 1 hour: {name}")
            conn.execute("UPDATE cf_contests SET notified_60=1 WHERE contest_id=?", (cid,))
            conn.commit()

        if 0 < seconds_until <= 900 and not row["notified_15"]:
            if on_notify:
                on_notify(f"Contest in 15 minutes: {name}")
            if play_alert:
                play_alert()
            conn.execute("UPDATE cf_contests SET notified_15=1 WHERE contest_id=?", (cid,))
            conn.commit()

    # (b) auto-open once start time has passed
    due = conn.execute(
        "SELECT * FROM cf_contests WHERE start_time <= ? AND opened_at_start = 0", (now,)
    ).fetchall()
    for row in due:
        webbrowser.open(f"https://codeforces.com/contest/{row['contest_id']}")
        conn.execute(
            "UPDATE cf_contests SET opened_at_start=1 WHERE contest_id=?", (row["contest_id"],)
        )
        conn.commit()
        if on_notify:
            on_notify(f"Opening problem set for {row['name']}.")

    # (c) analyze newly-finished tracked contests
    tracked_ids = {r["contest_id"] for r in conn.execute("SELECT contest_id FROM cf_contests")}
    analyzed_ids = {r["contest_id"] for r in conn.execute("SELECT contest_id FROM cf_contest_results")}
    conn.close()

    for cid in tracked_ids - analyzed_ids:
        live = contests_by_id.get(cid)
        if live and live.get("phase") == "FINISHED":
            result = analyze_contest(cid)
            if result and on_notify:
                on_notify(
                    f"Contest {result['name']} finished. "
                    f"You solved {len(result['solved_indexes'])} problem(s)."
                )


# ---------------------------------------------------------------------------
# 4. Manifest-dispatchable intents
# ---------------------------------------------------------------------------

def cf_rating():
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM cf_rating_history ORDER BY fetched_at DESC LIMIT 30"
    ).fetchall()
    conn.close()
    if not rows:
        return ("No rating data yet -- set cf_handle in jarvis_config.json "
                "and wait for the daily fetch to run at least once.")
    latest = rows[0]
    week_ago = latest["fetched_at"] - 7 * 24 * 3600
    week_row = next((r for r in rows if r["fetched_at"] <= week_ago), rows[-1])
    delta_week = latest["rating"] - week_row["rating"]
    return f"Current CF rating: {latest['rating']} ({delta_week:+d} vs last week)."


def cf_today():
    conn = _get_db()
    today_start = int(datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp())
    rows = conn.execute(
        "SELECT * FROM cf_submissions WHERE solved_at >= ? ORDER BY solved_at", (today_start,)
    ).fetchall()
    conn.close()
    if not rows:
        return "No problems solved today yet (per the CF tracker)."
    names = [f"{r['problem_index']} - {r['problem_name']}" for r in rows]
    return f"Solved {len(rows)} problem(s) today: " + ", ".join(names)


def cf_last_contest():
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM cf_contest_results ORDER BY analyzed_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return "No contest results recorded yet."
    solved = json.loads(row["solved_indexes"])
    times = json.loads(row["first_ac_times"])
    time_strs = [f"{idx} in {int(t // 60)} min" for idx, t in sorted(times.items())]
    return (
        f"Last contest: {row['name']}. Solved {len(solved)} problem(s)"
        f"{(': ' + ', '.join(solved)) if solved else ''}. "
        f"{'; '.join(time_strs) + '. ' if time_strs else ''}"
        f"Penalty (wrong submissions before AC): {row['wrong_submission_count']}."
    )


def cf_upcoming_contest():
    conn = _get_db()
    now = int(time.time())
    row = conn.execute(
        "SELECT * FROM cf_contests WHERE start_time > ? ORDER BY start_time ASC LIMIT 1", (now,)
    ).fetchone()
    conn.close()
    if not row:
        return "No upcoming contest tracked yet -- the heartbeat checks every 30 minutes."
    seconds_left = row["start_time"] - now
    hours, rem = divmod(seconds_left, 3600)
    minutes = rem // 60
    return f"Next contest: {row['name']} in {int(hours)}h {int(minutes)}m."


def seconds_until_next_contest():
    """Public helper for other modules (e.g. jarvis_memory's nudge
    checker) that need the raw number, not the formatted sentence."""
    conn = _get_db()
    row = conn.execute(
        "SELECT start_time FROM cf_contests WHERE start_time > ? ORDER BY start_time ASC LIMIT 1",
        (int(time.time()),),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return row["start_time"] - int(time.time())


def build_cf_monthly_payload(month=None, year=None):
    """Returns {"codeforces": {...}} matching the schema fed to Hermes's
    monthly assessment. `rank` is left null -- getting real contest rank
    requires an extra contest.standings call per contest, which isn't
    worth the added API load for a "nice to have" field."""
    today = datetime.date.today()
    month = int(month) if month else today.month
    year = int(year) if year else today.year

    month_start = int(datetime.datetime(year, month, 1).timestamp())
    next_month = datetime.datetime(year + 1, 1, 1) if month == 12 else datetime.datetime(year, month + 1, 1)
    month_end = int(next_month.timestamp())

    conn = _get_db()
    rating_rows = conn.execute(
        "SELECT * FROM cf_rating_history WHERE fetched_at >= ? AND fetched_at < ? ORDER BY fetched_at",
        (month_start, month_end),
    ).fetchall()
    rating_start = rating_rows[0]["rating"] if rating_rows else None
    rating_end = rating_rows[-1]["rating"] if rating_rows else None
    rating_delta = (rating_end - rating_start) if rating_rows else 0

    sub_rows = conn.execute(
        "SELECT * FROM cf_submissions WHERE solved_at >= ? AND solved_at < ?",
        (month_start, month_end),
    ).fetchall()
    by_index, by_tag = {}, {}
    for r in sub_rows:
        by_index[r["problem_index"]] = by_index.get(r["problem_index"], 0) + 1
        for tag in json.loads(r["tags"] or "[]"):
            by_tag[tag] = by_tag.get(tag, 0) + 1

    contest_rows = conn.execute(
        "SELECT cr.* FROM cf_contest_results cr JOIN cf_contests c ON cr.contest_id = c.contest_id "
        "WHERE c.start_time >= ? AND c.start_time < ?",
        (month_start, month_end),
    ).fetchall()
    contest_results = [
        {
            "name": cr["name"],
            "solved": json.loads(cr["solved_indexes"]),
            "penalty": cr["wrong_submission_count"],
            "rank": None,
        }
        for cr in contest_rows
    ]
    conn.close()

    return {
        "codeforces": {
            "month": f"{year}-{month:02d}",
            "rating_start": rating_start,
            "rating_end": rating_end,
            "rating_delta": rating_delta,
            "contests_participated": len(contest_rows),
            "problems_solved": len(sub_rows),
            "by_index": by_index,
            "by_tag": by_tag,
            "contest_results": contest_results,
        }
    }


def cf_monthly_summary(month=None, year=None):
    payload = build_cf_monthly_payload(month, year)["codeforces"]
    by_index_str = ", ".join(f"{k}: {v}" for k, v in payload["by_index"].items()) or "none"
    rs, re_ = payload["rating_start"], payload["rating_end"]
    rating_str = f"{rs} -> {re_} ({payload['rating_delta']:+d})" if rs is not None else "no data"
    return (
        f"CF summary for {payload['month']}: rating {rating_str}, "
        f"{payload['contests_participated']} contest(s), "
        f"{payload['problems_solved']} problem(s) solved ({by_index_str})."
    )
