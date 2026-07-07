"""
phone_server.py
----------------
Flask server that lets you control Jarvis from any browser on your local
WiFi — no app install needed. Run via start_phone_server() from main.py.
"""

import os
import secrets
import socket
import threading
import logging

from dotenv import load_dotenv

# Loaded here too (not just main.py) so running this file standalone for
# testing still picks up .env before intent_parser/jarvis_actions read
# their secrets at import time below.
load_dotenv()

from flask import Flask, request, jsonify
import intent_parser
import jarvis_actions

app = Flask(__name__)

# Silence Flask request logs; keep error logs
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

# Without this, anyone on the same WiFi/network can hit /command and run
# any Jarvis action (lock the PC, send WhatsApp messages, open sites,
# etc.) with zero authentication. Set JARVIS_PHONE_TOKEN in .env for a
# stable token across restarts; otherwise a random one is generated each
# run and printed/logged so you can still use the remote.
PHONE_SERVER_TOKEN = os.environ.get("JARVIS_PHONE_TOKEN") or secrets.token_urlsafe(16)

# Brute-force lockout: after MAX_FAILED_ATTEMPTS bad tokens from one IP,
# that IP is blocked for LOCKOUT_SECONDS. Without this, anyone on the
# WiFi could hammer /command guessing tokens indefinitely.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
_failed_attempts = {}  # ip -> (count, first_failure_time)
_attempts_lock = threading.Lock()


@app.before_request
def _require_token():
    # Every route, including "/", requires a valid token -- anyone who
    # finds this server on the WiFi should get a 401 immediately, not a
    # rendered UI (even a non-functional one reveals that Jarvis is
    # running here, which is more than a stranger should learn).
    import time as _time
    ip = request.remote_addr or "?"

    with _attempts_lock:
        count, first = _failed_attempts.get(ip, (0, 0))
        if count >= MAX_FAILED_ATTEMPTS:
            if _time.time() - first < LOCKOUT_SECONDS:
                return jsonify({"status": "error", "error": "Too many failed attempts -- try again later."}), 429
            del _failed_attempts[ip]

    token = request.args.get("token") or request.headers.get("X-Jarvis-Token") or ""
    # compare_digest: constant-time comparison, immune to timing attacks
    if not secrets.compare_digest(token, PHONE_SERVER_TOKEN):
        with _attempts_lock:
            count, first = _failed_attempts.get(ip, (0, _time.time()))
            _failed_attempts[ip] = (count + 1, first)
        return jsonify({"status": "error", "error": "Unauthorized -- missing or invalid token."}), 401

    with _attempts_lock:
        _failed_attempts.pop(ip, None)


@app.after_request
def _security_headers(resp):
    # Results can contain personal data (notes, clipboard, statuses) --
    # make sure the browser never caches them, and lock down framing/MIME.
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp

# ── HTML UI ───────────────────────────────────────────────────────────────────

_UI = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>Jarvis Remote</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #080c14;
    color: #e0e8ff;
    font-family: 'Segoe UI', system-ui, sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 20px 16px 40px;
  }

  /* ── header ── */
  .header {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 28px;
  }
  .title {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 3px;
    color: #ffa040;
    text-transform: uppercase;
  }
  .status-wrap { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #6a7a99; }
  .status-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: #ff4444;
    transition: background 0.4s;
    box-shadow: 0 0 6px #ff4444;
  }
  .status-dot.online { background: #44ff88; box-shadow: 0 0 8px #44ff88; }

  /* ── mic button ── */
  .mic-wrap { display: flex; flex-direction: column; align-items: center; gap: 16px; margin-bottom: 24px; }
  .mic-btn {
    width: 110px; height: 110px; border-radius: 50%;
    border: none; cursor: pointer;
    background: radial-gradient(circle, #ff8800 0%, #cc4400 60%, #1a0800 100%);
    box-shadow: 0 0 30px #ff6600aa, 0 0 60px #ff440055;
    font-size: 36px;
    display: flex; align-items: center; justify-content: center;
    transition: transform 0.1s, box-shadow 0.2s;
    position: relative;
    animation: pulse-idle 3s ease-in-out infinite;
  }
  .mic-btn:active { transform: scale(0.93); }
  .mic-btn.listening {
    animation: pulse-listen 0.8s ease-in-out infinite;
    box-shadow: 0 0 50px #ff6600cc, 0 0 100px #ff440088;
  }
  @keyframes pulse-idle {
    0%, 100% { box-shadow: 0 0 30px #ff6600aa, 0 0 60px #ff440033; }
    50%       { box-shadow: 0 0 45px #ff8800cc, 0 0 80px #ff660055; }
  }
  @keyframes pulse-listen {
    0%, 100% { box-shadow: 0 0 60px #ff6600ff, 0 0 120px #ff440099; transform: scale(1.0); }
    50%       { box-shadow: 0 0 90px #ffaa00ff, 0 0 160px #ff6600bb; transform: scale(1.05); }
  }
  .mic-hint { font-size: 12px; color: #4a5a77; letter-spacing: 1px; }

  /* ── input row ── */
  .input-row {
    width: 100%; max-width: 420px;
    display: flex; gap: 8px; margin-bottom: 20px;
  }
  .cmd-input {
    flex: 1; padding: 12px 14px;
    background: #0e1624; border: 1px solid #1e3050;
    border-radius: 10px; color: #e0e8ff; font-size: 15px;
    outline: none;
    transition: border-color 0.2s;
  }
  .cmd-input:focus { border-color: #ff6600; }
  .send-btn {
    padding: 12px 18px; border: none; border-radius: 10px;
    background: #ff6600; color: #fff; font-size: 15px;
    font-weight: 600; cursor: pointer;
    transition: background 0.2s, transform 0.1s;
  }
  .send-btn:active { background: #cc4400; transform: scale(0.96); }

  /* ── quick actions ── */
  .quick-label { font-size: 11px; letter-spacing: 2px; color: #3a4a66; margin-bottom: 10px; align-self: flex-start; }
  .quick-grid {
    width: 100%; max-width: 420px;
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 8px; margin-bottom: 24px;
  }
  .quick-btn {
    padding: 11px 8px; border: 1px solid #1e3050;
    border-radius: 10px; background: #0e1624;
    color: #8ab0e0; font-size: 13px; cursor: pointer;
    transition: background 0.2s, border-color 0.2s, color 0.2s;
    text-align: center;
  }
  .quick-btn:active { background: #162030; border-color: #ff6600; color: #ffa040; }

  /* ── results ── */
  .results-label { font-size: 11px; letter-spacing: 2px; color: #3a4a66; margin-bottom: 10px; align-self: flex-start; }
  .results {
    width: 100%; max-width: 420px;
    display: flex; flex-direction: column; gap: 10px;
  }
  .card {
    background: #0e1624; border: 1px solid #1e3050;
    border-radius: 12px; padding: 14px 16px;
    animation: slide-in 0.25s ease;
  }
  @keyframes slide-in {
    from { opacity: 0; transform: translateY(-8px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .card-cmd { font-size: 12px; color: #ff8040; margin-bottom: 6px; font-weight: 600; }
  .card-result { font-size: 14px; color: #c0d0f0; line-height: 1.5; white-space: pre-wrap; }
  .card.error { border-color: #5a1010; }
  .card.error .card-result { color: #ff6060; }

  /* ── spinner ── */
  .spinner {
    display: none; width: 20px; height: 20px;
    border: 2px solid #1e3050; border-top-color: #ff6600;
    border-radius: 50%; animation: spin 0.7s linear infinite;
    margin: 0 auto;
  }
  .spinner.active { display: block; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<div class="header">
  <div class="title">JARVIS</div>
  <div class="status-wrap">
    <div class="status-dot" id="dot"></div>
    <span id="status-text">connecting…</span>
  </div>
</div>

<div class="mic-wrap">
  <button class="mic-btn" id="micBtn" onclick="toggleMic()">🎤</button>
  <div class="mic-hint" id="micHint">TAP TO SPEAK</div>
</div>

<div class="input-row">
  <input class="cmd-input" id="cmdInput" type="text" placeholder="Type a command…" onkeydown="if(event.key==='Enter') sendText()">
  <button class="send-btn" onclick="sendText()">Send</button>
</div>

<div class="quick-label">CAPTURE TO INBOX</div>
<div class="input-row">
  <input class="cmd-input" id="captureInput" type="text" placeholder="Thought to capture…" onkeydown="if(event.key==='Enter') captureNote(false)">
  <button class="send-btn" onclick="captureNote(false)">Note</button>
  <button class="send-btn" style="background:#3399ff" onclick="captureNote(true)">Task</button>
</div>

<div class="quick-label">STATUS</div>
<div class="card" id="infoCard" style="width:100%;max-width:420px;margin-bottom:20px;">
  <div class="card-result" id="infoText">loading…</div>
</div>

<div class="quick-label">QUICK ACTIONS</div>
<div class="quick-grid">
  <button class="quick-btn" onclick="sendCommand('Lock the screen')">🔒 Lock PC</button>
  <button class="quick-btn" onclick="sendCommand('System status')">📊 System Status</button>
  <button class="quick-btn" onclick="sendCommand('Play pause media')">⏯ Play / Pause</button>
  <button class="quick-btn" onclick="sendCommand('Start focus session for 25 minutes')">🎯 Focus 25 min</button>
  <button class="quick-btn" onclick="buzzPC()">📢 Find My PC</button>
  <button class="quick-btn" onclick="sendCommand('Show todays workout')">🏋️ Today's Workout</button>
  <button class="quick-btn" onclick="sendCommand('What is my CF rating')">📈 CF Rating</button>
  <button class="quick-btn" onclick="sendCommand('What should I upsolve')">🧩 Upsolve List</button>
</div>

<div class="results-label">RESULTS</div>
<div class="spinner" id="spinner"></div>
<div class="results" id="results"></div>

<script>
const dot         = document.getElementById('dot');
const statusText  = document.getElementById('status-text');
const micBtn      = document.getElementById('micBtn');
const micHint     = document.getElementById('micHint');
const cmdInput    = document.getElementById('cmdInput');
const spinner     = document.getElementById('spinner');
const resultsDiv  = document.getElementById('results');

// ── auth token ─────────────────────────────────────────────────────────────────
// Captured once from the URL (?token=...) printed at server startup, then
// persisted so you don't need the query param on every visit.
(function () {
  const urlToken = new URLSearchParams(window.location.search).get('token');
  if (urlToken) {
    localStorage.setItem('jarvisToken', urlToken);
    // Scrub the token out of the address bar / browser history.
    history.replaceState(null, '', window.location.pathname);
  }
})();
function authedFetch(url, opts) {
  const token = localStorage.getItem('jarvisToken') || '';
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers, { 'X-Jarvis-Token': token });
  return fetch(url, opts);
}

// ── status polling ────────────────────────────────────────────────────────────
function pollStatus() {
  authedFetch('/status')
    .then(r => { if (!r.ok) throw new Error('unauthorized'); return r.json(); })
    .then(() => {
      dot.classList.add('online');
      statusText.textContent = 'online';
    })
    .catch(() => {
      dot.classList.remove('online');
      statusText.textContent = localStorage.getItem('jarvisToken') ? 'offline' : 'no token -- open the link from the server log';
    });
}
pollStatus();
setInterval(pollStatus, 3000);

// ── live status card ──────────────────────────────────────────────────────────
const infoText = document.getElementById('infoText');
function pollInfo() {
  authedFetch('/info')
    .then(r => r.json())
    .then(d => {
      let parts = ['CPU ' + d.cpu + '%'];
      if (d.battery !== undefined) parts.push('🔋 ' + d.battery + '%' + (d.plugged ? ' ⚡' : ''));
      if (d.last_action && d.last_action.name)
        parts.push('Last: ' + d.last_action.name + ' @ ' + d.last_action.time);
      infoText.textContent = parts.join('  ·  ');
    })
    .catch(() => { infoText.textContent = 'status unavailable'; });
}
pollInfo();
setInterval(pollInfo, 10000);

// ── capture to inbox ──────────────────────────────────────────────────────────
const captureInput = document.getElementById('captureInput');
async function captureNote(todo) {
  const text = captureInput.value.trim();
  if (!text) return;
  try {
    const res = await authedFetch('/capture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, todo })
    });
    const data = await res.json();
    addCard((todo ? 'Task' : 'Note') + ' → inbox', (data.results || [data.error]).join('\\n'), data.status !== 'ok');
    if (data.status === 'ok') captureInput.value = '';
  } catch (e) {
    addCard('Capture', "Can't reach Jarvis — check WiFi.", true);
  }
}

// ── find my PC ────────────────────────────────────────────────────────────────
async function buzzPC() {
  try {
    const res = await authedFetch('/buzz', { method: 'POST' });
    const data = await res.json();
    addCard('Find My PC', (data.results || ['Buzzing…']).join('\\n'), false);
  } catch (e) {
    addCard('Find My PC', "Can't reach Jarvis — check WiFi.", true);
  }
}

// ── send command ──────────────────────────────────────────────────────────────
async function sendCommand(text) {
  if (!text.trim()) return;
  cmdInput.value = text;
  spinner.classList.add('active');

  try {
    const res = await authedFetch('/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text })
    });
    const data = await res.json();
    if (data.status === 'ok') {
      addCard(text, data.results.join('\\n'), false);
    } else {
      addCard(text, data.error || 'Unknown error', true);
    }
  } catch (e) {
    addCard(text, "Can't reach Jarvis — check WiFi.", true);
  } finally {
    spinner.classList.remove('active');
    cmdInput.value = '';
  }
}

function sendText() {
  sendCommand(cmdInput.value.trim());
}

// ── result cards ──────────────────────────────────────────────────────────────
function addCard(cmd, result, isError) {
  const card = document.createElement('div');
  card.className = 'card' + (isError ? ' error' : '');
  card.innerHTML = '<div class="card-cmd">' + escHtml(cmd) + '</div>'
                 + '<div class="card-result">' + escHtml(result) + '</div>';
  resultsDiv.insertBefore(card, resultsDiv.firstChild);
  if (resultsDiv.children.length > 10) resultsDiv.lastChild.remove();
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── voice input ───────────────────────────────────────────────────────────────
let recog = null;
let listening = false;

function toggleMic() {
  if (!('webkitSpeechRecognition' in window || 'SpeechRecognition' in window)) {
    addCard('Voice', 'Speech recognition not supported in this browser. Use text input.', true);
    return;
  }
  if (listening) {
    recog && recog.stop();
    return;
  }
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  recog = new SR();
  recog.lang = 'en-US';
  recog.continuous = false;
  recog.interimResults = false;

  recog.onstart = () => {
    listening = true;
    micBtn.classList.add('listening');
    micHint.textContent = 'LISTENING…';
  };
  recog.onresult = (e) => {
    const transcript = e.results[0][0].transcript;
    cmdInput.value = transcript;
    sendCommand(transcript);
  };
  recog.onerror = (e) => {
    addCard('Voice', 'Mic error: ' + e.error + '. Try text input.', true);
  };
  recog.onend = () => {
    listening = false;
    micBtn.classList.remove('listening');
    micHint.textContent = 'TAP TO SPEAK';
  };
  recog.start();
}
</script>
</body>
</html>"""


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return _UI, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/status")
def status():
    return jsonify({"status": "online", "jarvis": "ready"})


@app.route("/info")
def info():
    """Live status card data -- battery, CPU, and the last action Jarvis
    ran (private actions are already masked in LAST_ACTION)."""
    import psutil
    payload = {"cpu": psutil.cpu_percent(interval=None)}
    battery = psutil.sensors_battery()
    if battery:
        payload["battery"] = battery.percent
        payload["plugged"] = battery.power_plugged
    payload["last_action"] = jarvis_actions.LAST_ACTION
    return jsonify(payload)


@app.route("/buzz", methods=["POST"])
def buzz():
    """Find-my-PC: loud locator beeps."""
    return jsonify({"status": "ok", "results": [jarvis_actions.buzz_pc()]})


@app.route("/capture", methods=["POST"])
def capture():
    """Phone capture: appends a note (or todo) straight to the vault
    inbox -- no intent parsing, no LLM, the text lands verbatim. Same
    privacy path as voice captures (content redacted from the event
    log via run_function's PRIVATE_FUNCTIONS)."""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"status": "error", "error": "No text provided."}), 400
    result = jarvis_actions.run_function(
        "capture_note", {"text": text, "todo": bool(data.get("todo")), "source": "phone"})
    return jsonify({"status": "ok", "results": [result]})


@app.route("/command", methods=["POST"])
def command():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"status": "error", "error": "No command text provided."}), 400

    try:
        parsed  = intent_parser.parse_command(text)
        actions = parsed.get("actions", [])
        if not actions:
            return jsonify({"status": "ok", "results": ["I couldn't figure out what to do with that."]}), 200

        results = []
        for action in actions:
            name = action.get("function")
            args = action.get("args", {})
            if name == "chat":
                # conversational mode -- answer text comes straight from
                # the parser, nothing to dispatch
                reply = (args.get("response") or "").strip()
                if reply:
                    results.append(reply)
                continue
            outcome = jarvis_actions.run_function(name, args)
            results.append(outcome or f"Done: {name}")

        return jsonify({"status": "ok", "results": results})

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ── startup ───────────────────────────────────────────────────────────────────

def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def start_phone_server(port=5000):
    ip = _local_ip()
    url = f"https://{ip}:{port}/?token={PHONE_SERVER_TOKEN}"
    print(f"\n{'='*46}")
    print(f"  Jarvis Phone Remote -> {url}")
    print("  Open that URL in Chrome on your phone")
    print("  (token included -- don't share this link)")
    print(f"{'='*46}\n")

    thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False,
                               use_reloader=False, ssl_context="adhoc"),
        daemon=True,
    )
    thread.start()


if __name__ == "__main__":
    start_phone_server()
    input("Server running. Press Enter to stop.\n")
