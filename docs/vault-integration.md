# Jarvis ↔ Obsidian Vault Integration

Written 2026-07-07 for future-me, who will have forgotten all of this.

## Architecture — who writes where

```
                     ┌──────────────────────────────────────────┐
                     │  Vault (C:\Users\...\claude-obsidian)    │
 voice/phone ──────► │  inbox/    Jarvis-owned. Daily capture   │
                     │            files (YYYY-MM-DD.md), drafts │
 heartbeat 11PM ───► │  journal/  Jarvis-owned. Nightly mirror  │
                     │            + cf-<id>-debrief.md          │
 librarian only ───► │  wiki/     NEVER touched by Jarvis.      │
                     │            Ingest promotes inbox → wiki  │
                     └──────────────────────────────────────────┘
```

**The one law:** Jarvis appends to (or fully rewrites) only files it
created. `wiki/` belongs to the librarian (a manual Claude ingest pass
so far — the vault's own scripts need `flock`, absent on Windows).
Dropped-in inbox documents (e.g. the roadmap) are NOT Jarvis-owned:
`complete_task` deliberately scans only `\d{4}-\d{2}-\d{2}\.md` dailies
plus CF debriefs.

Config keys (gitignored `jarvis_config.json`): `vault_path`,
`vault_inbox`, `vault_journal`, `vault_name`, `vault_retrieve_script`.
Everything degrades to a spoken "vault not configured" when unset.

## Capture surfaces

| Surface | Function | Marker | Notes |
| --- | --- | --- | --- |
| Voice "note that…" | `capture_note` | `(voice)` | LLM may paraphrase |
| Voice "add a task…" | `capture_note(todo=True)` | `- [ ]` checkbox | completable later |
| Phone remote text box | `/capture` endpoint | `(phone)` | verbatim, no LLM |
| "dictate a note" | `dictate_to_note` | own `draft-*.md` file | paragraph per utterance |
| "note what I'm looking at" | `capture_screen_note` | `(screen)` | Groq vision describe + comment |

All of these are in `PRIVATE_FUNCTIONS` — content never reaches the
SQLite event log (which feeds Groq's nightly distillation).

## Pre-route rules (intent_parser.py, zero tokens)

1. `EXACT_PHRASE_TRIGGERS`: easter eggs are exact-string matched
   locally, never sent to the API.
2. `_preroute_metrics`: a note-prefixed utterance ("note that / remember
   that / capture") containing **a digit AND a workout/metric keyword**
   (bench/squat/kg/ran/km/weighed/…) gets its prefix stripped so the
   model routes it to `log_progress`, not `capture_note`. Both
   conditions required: "remember that I need to run errands" (no
   digit) and "note that my locker code is 4521" (no metric word) stay
   notes.

Lesson learned the hard way: **deterministic code beats prompt
sharpening for routing.** The model ignored "NOT for workout weights"
in the manifest; the pre-route fixed it for free.

## ask_brain flow

```
"what do my notes say about X"
  → speak ack ("Checking your notes…")            [immediate]
  → try vault's scripts/retrieve.py (subprocess)  [15s cap]
      └─ currently always fails on Windows: bm25-index.py imports fcntl
  → fallback: vault_search.py (pure-Python BM25 over wiki+journal+inbox)
  → top 4 chunks (500 chars each) → Ollama qwen3:8b @ 127.0.0.1:11434
      strict prompt: answer ONLY from notes, <60 words, JARVIS persona,
      "I don't have that in your notes" when absent
      *** think=False is MANDATORY (see below) ***  [120s cap, keep_alive 30m]
  → answer spoken as the normal outcome
```

Latency on this machine (CPU-only): ~6s warm / ~30-40s after a cold
model load. Deviations from the original spec, both deliberate:
work runs on the dispatch thread (a detached worker would race the
follow-up-mode microphone), and the timeout is 120s not 30s (cold load
alone exceeds 30s here).

## Index maintenance

- Cache: `~/.jarvis_vault_index.json` (outside the vault, on purpose).
- Refreshed by the heartbeat every tick via `ensure_index()` — no-op
  when <24h old, full rebuild otherwise. Never on the voice path;
  `search()` self-heals if the cache is missing.

## The qwen3 trap

Every Ollama call to qwen3 MUST pass `think=False` (the API parameter —
`/no_think` in the prompt does NOT work). Otherwise the model burns its
entire output budget on hidden reasoning and returns empty content.
This silently broke ask_brain, the opinion loop, the heartbeat
scheduler, and the monthly assessment before being found. If output
from a local model is mysteriously empty: check this first.

## Standing rules

1. **Restart Jarvis after every code change. Kill ALL python/pythonw
   processes first** (double-launch happened twice; there is now a
   single-instance lock on port 47823, but the habit stands).
2. Run `pytest test_vault_integration.py` (+ `test_routing.py`) after
   touching capture/routing/journal code.
3. Manifest token budget: every new entry is ONE terse line; prefer
   zero-token pre-routes. Full accounting: see git history of this file.

## Manifest token accounting (final, whole integration)

Additions: ask_brain (~45) + capture_note incl. todo extension (~55) +
list_tasks (~25) + complete_task (~28) + open_note (~28) +
dictate_to_note (~35) + capture_screen_note (~38) + log_progress
bodyweight arg (~15) ≈ **+269 tokens**.
Removals: save_note entry (~70) ≈ **−70 tokens**.
**Net: ≈ +200 tokens** against the original 120 budget. The overrun is
entirely the features added beyond the original spec (tasks trio,
dictation, screen notes, open_note ≈ +154); the original Stages 0–3
scope alone came in at ~55. Phone capture cost zero (bypasses the
parser). If the budget ever bites again: prune rarely-used manifest
entries (September roadmap item) or split the parse into two stages.
