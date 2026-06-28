# Jarvis

A personal voice-controlled desktop assistant for Windows: a glowing 3D orb UI, wake-word activation, voice command parsing via Groq, an autonomous background task heartbeat via local Ollama, and a phone-remote web UI.

## Setup

1. **Clone the repo and create a virtual environment**

   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Install Playwright's browser** (used for hands-free WhatsApp sending)

   ```
   playwright install chromium
   ```

3. **Install and run Ollama** (used by the background heartbeat agent)

   - Download from [ollama.com](https://ollama.com)
   - Pull the model: `ollama pull hermes3`
   - Make sure `ollama serve` is running before starting Jarvis

4. **Configure your secrets**

   Copy `.env.example` to `.env` and fill in your own values:

   ```
   copy .env.example .env
   ```

   - `GROQ_API_KEY` — required for voice transcription and intent parsing. Get a free key at [console.groq.com/keys](https://console.groq.com/keys).
   - `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` — optional, only needed for Spotify playback. Create an app at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) with redirect URI `http://localhost:8888/callback`.
   - `JARVIS_PHONE_TOKEN` — optional. Secures the phone-remote web UI. Leave blank to get a random token printed to the console on each startup, or set a fixed value for a stable link.

5. **Personalize the config dictionaries**

   At the top of `jarvis_actions.py`, fill in (all empty by default):
   - `PROJECT_PATHS` — local project folders for `open_project`
   - `WHATSAPP_CONTACTS` — name → phone number mapping for `send_whatsapp`
   - `WHATSAPP_DEFAULT_COUNTRY_CODE` — auto-prepended to bare 10-digit numbers

6. **Run it**

   ```
   python main.py
   ```

   First run will prompt a one-time WhatsApp Web QR login (visible browser window) if you use `send_whatsapp`. Jarvis runs as a system tray icon — right-click it for Wake Up / Sleep / Exit.

## Notes

- `jarvis.log`, `TASK_BOARD.md`, and the Playwright/Spotify auth caches are gitignored — they're local runtime state, not source.
- The phone remote (`phone_server.py`) requires the token from step 4 in the URL (`?token=...`) or as an `X-Jarvis-Token` header — the startup console output prints the full authenticated link.
- Voice transcription and intent parsing run on Groq's cloud API for speed; only the unattended background heartbeat uses local Ollama, since its latency doesn't matter for unattended work.
