import tkinter as tk
import threading
import asyncio
import tempfile
import os
import ctypes
import logging
import time
import socket

from dotenv import load_dotenv

# Must run before any of this project's modules are imported below --
# they read secrets (GROQ_API_KEY, SPOTIFY_CLIENT_ID, etc.) via
# os.environ.get() at import time, so .env has to be loaded first.
load_dotenv()

# This network's IPv6 routing is broken (IPv6 addresses resolve but never
# connect, so every HTTPS call stalls ~40s on a dead IPv6 attempt before
# falling back to IPv4). Forcing getaddrinfo to only return IPv4 results
# skips that dead path entirely for every outbound connection this
# process makes (Groq, Ollama, yt_dlp, Playwright, etc.).
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

# Tell Windows this process is DPI-aware so Tkinter geometry coords are
# physical pixels, matching winfo_screenwidth/height and window placement.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from PIL import ImageTk

import intent_parser
import jarvis_actions
import voice_input
import heartbeat_agent
import tray_icon
import cf_tracker
import jarvis_memory
import easter_eggs
from orb_renderer import OrbRenderer

SIZE = 360
MAGIC_BG_HEX = "#010101"   # transparentcolor key — must not appear in the drawn orb
_MAGIC_BG_RGB = (1, 1, 1)  # same key as an RGB tuple, for the brightness-preserving mask in _draw()
FRAME_MS = 33              # ~30 fps animation

OLLAMA_HOST = "http://localhost:11434"

# Under pythonw.exe there is no console, so route everything that used
# to be a bare print() into a log file as well.
LOG_PATH = os.path.join(os.path.dirname(__file__), "jarvis.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("jarvis.main")


def _wait_for_ollama(timeout=15):
    """Pre-flight check -- make sure the local Ollama service is actually
    reachable before booting the UI, since intent parsing and the
    heartbeat agent both depend on it."""
    import requests
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=2)
            if r.ok:
                logger.info("Ollama service is up.")
                return True
        except Exception:
            pass
        time.sleep(1)
    logger.error(
        f"Ollama not reachable at {OLLAMA_HOST} after {timeout}s -- "
        "start it with 'ollama serve' (and 'ollama pull hermes3' if needed)."
    )
    return False


# ── easter-egg orb adapter ───────────────────────────────────────────────────────

class OrbController:
    """Thin adapter handed to easter_eggs.py handlers -- gives them
    set_color/set_pulse_speed/set_brightness/freeze/restore without
    exposing the rest of JarvisOrb. All mutations go through root.after()
    since they touch Tk-rendered state from a non-Tk thread (easter eggs
    run on their own background thread, same as _talk_flow)."""

    def __init__(self, jarvis_orb):
        self._orb = jarvis_orb

    def set_color(self, r, g, b):
        self._orb.root.after(0, lambda: self._orb._set_custom_color(r, g, b))

    def set_pulse_speed(self, speed):
        def _apply():
            import orb_renderer
            self._orb._pulse_speed_mult = speed
            orb_renderer.set_custom_ring_speed(speed)
        self._orb.root.after(0, _apply)

    def set_brightness(self, level):
        level = max(0.0, min(1.0, level))
        self._orb.root.after(0, lambda: setattr(self._orb, "_brightness", level))

    def freeze(self):
        self._orb.root.after(0, lambda: setattr(self._orb, "_frozen", True))

    def restore(self):
        self._orb.root.after(0, self._orb._restore_from_easter_egg)


# ── main class ────────────────────────────────────────────────────────────────

class JarvisOrb:
    IDLE_HIDE_DELAY_MS = 4000  # keep the orb visible briefly after returning to idle

    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        self.root.wm_attributes("-transparentcolor", MAGIC_BG_HEX)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        # Cap the orb so it always fits on screen
        actual = min(SIZE, sw - 48, sh - 80)
        x = max(0, sw - actual - 24)
        y = max(0, sh - actual - 60)
        self.root.geometry(f"{actual}x{actual}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.root, width=actual, height=actual,
            bg=MAGIC_BG_HEX, highlightthickness=0
        )
        self.canvas.pack()

        self._renderer = OrbRenderer(size=actual)

        self.state = "idle"
        self._phase = 0.0
        self._photo = None

        self._press_x = self._press_y = 0
        self._win_x = self._win_y = 0
        self._is_drag = False
        self._busy = False
        self._sleep_mode = False
        self._hide_after_id = None

        # Easter-egg orb controls -- additive on top of the existing
        # state machine, never touches orb_renderer's projection/particle
        # math. _frozen halts phase advancement; _brightness is a
        # post-render scale applied in _draw(); _pulse_speed_mult scales
        # the phase step while in the "custom" state.
        self._frozen = False
        self._brightness = 1.0
        self._pulse_speed_mult = 1.0
        self.orb_controller = OrbController(self)

        self._animate()

        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",       self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

        # start hidden -- only shows on listening/processing/speaking/
        # background_processing, or via the tray's "Wake Up" menu item
        self.root.withdraw()

        # start wake word listener in background
        self._wake_active = True
        threading.Thread(target=self._wake_word_loop, daemon=True).start()

        # autonomous heartbeat -- runs independent of mic/voice state
        self.heartbeat = heartbeat_agent.HeartbeatAgent(
            on_state_change=self._on_heartbeat_state,
            on_notify=self._on_heartbeat_notify,
            on_checkin_trigger=self._on_checkin_trigger,
        )
        self.heartbeat.start()

        # Codeforces daily submission/rating fetch -- separate cadence
        # (24h) from the 30-min heartbeat, per the CF tracker spec.
        cf_tracker.start_daily_fetch_thread()

    # ── drawing ───────────────────────────────────────────────────────────────

    def _draw(self):
        frame = self._renderer.render(self.state, self._phase)
        if self._brightness != 1.0:
            from PIL import ImageEnhance
            import numpy as np
            arr = np.array(frame)
            # Darkening the whole frame shifts the untouched background
            # pixels away from the exact magic color Tk uses for
            # -transparentcolor (#010101) -- at low brightness the window
            # stops being click-through/transparent and becomes a solid
            # opaque square instead. Remember which pixels were exactly
            # the background before enhancing, then force them back.
            bg_mask = np.all(arr == np.array(_MAGIC_BG_RGB), axis=-1)
            enhanced = np.array(ImageEnhance.Brightness(frame).enhance(self._brightness))
            enhanced[bg_mask] = _MAGIC_BG_RGB
            from PIL import Image as _Image
            frame = _Image.fromarray(enhanced)
        self._photo = ImageTk.PhotoImage(frame)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self._photo, anchor="nw")

    def _animate(self):
        if not self._frozen:
            if self.state == "custom":
                step = 0.02 * self._pulse_speed_mult
            else:
                step = 0.014 if self.state == "idle" else 0.032
            self._phase = (self._phase + step) % 1.0
        self._draw()
        self.root.after(FRAME_MS, self._animate)

    # ── state ─────────────────────────────────────────────────────────────────

    def _set_state(self, state):
        self.state = state
        self._phase = 0.0

        if self._hide_after_id is not None:
            self.root.after_cancel(self._hide_after_id)
            self._hide_after_id = None

        if state == "idle":
            self._hide_after_id = self.root.after(self.IDLE_HIDE_DELAY_MS, self._hide_window)
        elif not self._sleep_mode:
            self.root.deiconify()

    def _hide_window(self):
        self._hide_after_id = None
        self.root.withdraw()

    def _show_error(self, duration_ms=2200):
        self._set_state("error")
        self.root.after(duration_ms, lambda: self._set_state("idle"))

    # ── easter-egg orb controls ──────────────────────────────────────────────────

    def _set_custom_color(self, r, g, b):
        import orb_renderer
        orb_renderer.set_override_color(r, g, b)
        if self.state != "custom":
            self._set_state("custom")  # only on first entry -- _set_state
            # already handles deiconify; avoids resetting phase/restarting
            # the ring rotation on every subsequent color change

    def _restore_from_easter_egg(self):
        import orb_renderer
        self._frozen = False
        self._brightness = 1.0
        self._pulse_speed_mult = 1.0
        orb_renderer.set_custom_ring_speed(1.0)
        self._set_state("idle")

    # ── heartbeat / tray callbacks ───────────────────────────────────────────────

    def _on_heartbeat_state(self, state):
        """Bridges heartbeat_agent's background thread into the Tk thread.
        Skipped while a voice interaction owns the orb, so the two never
        fight over the same UI state."""
        if not self._busy:
            self.root.after(0, lambda: self._set_state(state))

    def _on_heartbeat_notify(self, text):
        """Speaks something out loud from the heartbeat thread (e.g. the
        daily progress reminder) -- skipped while a voice interaction is
        already in progress, same guard as _on_heartbeat_state."""
        if self._busy:
            return
        self.root.after(0, lambda: self._set_state("speaking"))
        self._speak(text)
        self.root.after(0, lambda: self._set_state("idle"))

    def _on_checkin_trigger(self):
        """Called from the heartbeat thread (a proactive nudge or the
        9 AM auto-trigger) to start the mood/energy check-in flow. Runs
        on its own thread since it's a multi-turn blocking voice
        interaction (3x speak+listen) -- can't run on the heartbeat
        thread itself without stalling its 30-min tick loop, and can't
        run on the Tk thread either since voice_input.listen() blocks on
        the mic."""
        if self._busy:
            return
        threading.Thread(target=self._run_mood_checkin, daemon=True).start()

    def _run_mood_checkin(self):
        self._busy = True
        try:
            self.root.after(0, lambda: self._set_state("speaking"))
            result = jarvis_memory.run_mood_checkin(speak_fn=self._speak, listen_fn=voice_input.listen)
            logger.info(result)
        except Exception as e:
            logger.error(f"[mood checkin error] {e}")
        finally:
            self.root.after(0, lambda: self._set_state("idle"))
            self._busy = False

    def wake_up(self):
        self._sleep_mode = False
        self.root.after(0, self.root.deiconify)

    def sleep(self):
        self._sleep_mode = True
        self.root.after(0, self.root.withdraw)

    def shutdown(self):
        logger.info("Shutting down Jarvis...")
        self._wake_active = False
        self.heartbeat.stop()
        self.root.after(0, self.root.destroy)

    # ── drag / click ──────────────────────────────────────────────────────────

    def _on_press(self, event):
        self._press_x  = event.x_root
        self._press_y  = event.y_root
        self._win_x    = self.root.winfo_x()
        self._win_y    = self.root.winfo_y()
        self._is_drag  = False

    def _on_motion(self, event):
        dx = event.x_root - self._press_x
        dy = event.y_root - self._press_y
        if abs(dx) > 5 or abs(dy) > 5:
            self._is_drag = True
            self.root.geometry(f"+{self._win_x + dx}+{self._win_y + dy}")

    def _on_release(self, event):
        if not self._is_drag and self.state == "idle" and not self._busy:
            threading.Thread(target=self._talk_flow, daemon=True).start()

    # ── wake word ─────────────────────────────────────────────────────────────

    def _wake_word_loop(self):
        try:
            from openwakeword.model import Model as OWWModel
            import sounddevice as sd
            import numpy as np

            logger.info("Loading wake word model (first run downloads ~5 MB)...")
            oww = OWWModel(wakeword_models=["hey_jarvis"], inference_framework="onnx")
            logger.info("Wake word active — say 'Hey Jarvis' to activate.")

            CHUNK = 1280  # 80 ms at 16 kHz
            with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                                 blocksize=CHUNK) as stream:
                while self._wake_active:
                    audio, _ = stream.read(CHUNK)
                    if self._busy:
                        oww.reset()
                        continue
                    score = max(oww.predict(audio.flatten()).values(), default=0)
                    if score > 0.5:
                        logger.info(f"Hey Jarvis! (score {score:.2f})")
                        oww.reset()
                        time.sleep(0.4)
                        if not self._busy:
                            self.root.after(0, self._trigger_from_wake_word)

        except ImportError:
            logger.warning("openwakeword not installed — wake word disabled.")
        except Exception as e:
            logger.error(f"[wake word error] {e}")

    def _trigger_from_wake_word(self):
        if self.state == "idle" and not self._busy:
            threading.Thread(target=self._talk_flow, daemon=True).start()

    # ── voice pipeline ────────────────────────────────────────────────────────

    def _talk_flow(self):
        self._busy = True
        try:
            # 1 — listen
            self.root.after(0, lambda: self._set_state("listening"))
            try:
                text = voice_input.listen()
            except Exception as e:
                logger.error(f"[listen error] {e}")
                self.root.after(0, self._show_error)
                return

            if not text:
                logger.info("Didn't catch that.")
                self.root.after(0, lambda: self._set_state("idle"))
                return

            logger.info(f"> {text}")

            # easter eggs
            clean = text.lower().strip()
            if "who is a good boy" in clean:
                self.root.after(0, lambda: self._set_state("speaking"))
                self._speak("ME SIR MEEE!")
                self.root.after(0, lambda: self._set_state("idle"))
                return
            if "are you there" in clean:
                self.root.after(0, lambda: self._set_state("speaking"))
                self._speak("For you sir, always.")
                self.root.after(0, lambda: self._set_state("idle"))
                return

            # 2 — parse
            self.root.after(0, lambda: self._set_state("processing"))
            try:
                result = intent_parser.parse_command(text)
            except Exception as e:
                logger.error(f"[parse error] {e}")
                self.root.after(0, self._show_error)
                return

            actions = result.get("actions", [])
            if not actions:
                self.root.after(0, lambda: self._set_state("speaking"))
                self._speak("Sorry sir, I couldn't figure out what to do with that.")
                self.root.after(0, lambda: self._set_state("idle"))
                return

            # check_in needs a real multi-turn voice flow (3x speak+listen),
            # not a single string return like every other action -- run it
            # directly here (already off the Tk thread, already _busy)
            # instead of through the normal run_function dispatch below.
            checkin_actions = [a for a in actions if a.get("function") == "check_in"]
            actions = [a for a in actions if a.get("function") != "check_in"]
            if checkin_actions:
                self.root.after(0, lambda: self._set_state("speaking"))
                try:
                    checkin_result = jarvis_memory.run_mood_checkin(
                        speak_fn=self._speak, listen_fn=voice_input.listen
                    )
                    logger.info(checkin_result)
                except Exception as e:
                    logger.error(f"[checkin error] {e}")
                if not actions:
                    self.root.after(0, lambda: self._set_state("idle"))
                    return

            # easter eggs need live orb control + multi-line scripted TTS,
            # not a single string return -- same interception pattern as
            # check_in above. Only the first matched egg per command runs
            # (firing two scripted sequences back-to-back makes no sense).
            egg_actions = [a for a in actions if a.get("function") in easter_eggs.EASTER_EGG_HANDLERS]
            actions = [a for a in actions if a.get("function") not in easter_eggs.EASTER_EGG_HANDLERS]
            if egg_actions:
                egg_name = egg_actions[0]["function"]
                try:
                    egg_result = easter_eggs.run_easter_egg(
                        egg_name, speak_fn=self._speak, orb=self.orb_controller
                    )
                    logger.info(egg_result)
                except Exception as e:
                    logger.error(f"[easter egg error] {e}")
                # Always do a full restore here, not just _set_state("idle")
                # -- some eggs (easter_dont_leave) intentionally end with
                # brightness faded to 0 and the orb frozen, right before
                # sleep_pc(), without calling orb.restore() themselves.
                # Once control returns here (the PC has woken back up),
                # the visual overrides need to be reset or the orb stays
                # frozen and black forever -- _set_state alone doesn't
                # touch _brightness/_frozen/_pulse_speed_mult.
                if not actions:
                    self.root.after(0, self._restore_from_easter_egg)
                    return

            # 3 — execute
            outcomes = []
            for action in actions:
                name = action.get("function")
                args = action.get("args", {})
                try:
                    outcome = jarvis_actions.run_function(name, args)
                    logger.info(outcome)
                    outcomes.append(outcome)
                except Exception as e:
                    logger.error(f"[action error] {e}")
                    outcomes.append(f"Error: {e}")

            # 4 — speak
            self.root.after(0, lambda: self._set_state("speaking"))
            for outcome in outcomes:
                if not outcome:
                    continue
                if outcome.lower().startswith("error"):
                    self._speak(f"Sorry sir, {outcome}")
                else:
                    self._speak(outcome)

            self.root.after(0, lambda: self._set_state("idle"))

            # If we just started music, hold busy for 6s so the mic doesn't
            # pick up the song and re-trigger the same command.
            spotify_fns = {"play_spotify_search", "play_spotify_playlist"}
            if any(a.get("function") in spotify_fns for a in actions):
                time.sleep(6)

        finally:
            self._busy = False

    def _speak(self, text):
        try:
            import edge_tts

            async def _synthesize():
                communicate = edge_tts.Communicate(text, voice="en-US-AriaNeural")
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    tmp = f.name
                await communicate.save(tmp)
                return tmp

            # Run in a fresh thread -- Playwright's sync API (used by
            # send_whatsapp) leaves this thread's asyncio state in a way
            # that makes a later asyncio.run() here raise "cannot be
            # called from a running event loop", even though nothing is
            # actually running. A brand-new thread has clean asyncio state.
            tmp_holder = {}
            def _run_synth():
                tmp_holder["path"] = asyncio.run(_synthesize())
            t = threading.Thread(target=_run_synth)
            t.start()
            t.join()
            tmp = tmp_holder["path"]

            # Play MP3 via Windows MCI — no extra packages needed
            mci = ctypes.windll.winmm.mciSendStringW
            mci(f'open "{tmp}" type mpegvideo alias jarvis_tts', None, 0, None)
            mci('play jarvis_tts wait', None, 0, None)
            mci('close jarvis_tts', None, 0, None)
            os.unlink(tmp)

        except Exception as e:
            logger.error(f"[tts error] {e}")
            try:
                import pyttsx3
                engine = pyttsx3.init()
                engine.say(text)
                engine.runAndWait()
                engine.stop()
            except Exception as e2:
                logger.error(f"[tts fallback error] {e2}")


def _run_orb_thread(ready_event, orb_holder):
    """Tkinter requires the root to be created AND mainloop()'d on the
    same thread -- so JarvisOrb() itself (which builds self.root) has to
    be constructed here, not on the main thread, since the main thread is
    reserved for pystray's message loop."""
    orb = JarvisOrb()
    orb_holder["orb"] = orb
    ready_event.set()
    orb.root.mainloop()


if __name__ == "__main__":
    from phone_server import start_phone_server

    logger.info("Starting Jarvis...")

    # Intent parsing runs on Groq now, not Ollama -- only the background
    # heartbeat needs Ollama, and it already retries every tick on its own,
    # so don't block startup waiting on it. Just check in the background
    # and log a heads-up if it's not reachable.
    threading.Thread(
        target=lambda: _wait_for_ollama() or logger.warning(
            "Ollama not reachable -- the autonomous heartbeat will keep "
            "retrying each cycle, but background tasks won't run until it's up."
        ),
        daemon=True,
    ).start()

    start_phone_server()

    _ready = threading.Event()
    _orb_holder = {}
    tk_thread = threading.Thread(target=_run_orb_thread, args=(_ready, _orb_holder), daemon=False)
    tk_thread.start()
    _ready.wait()
    orb = _orb_holder["orb"]

    icon = tray_icon.build_tray_icon(
        on_wake=orb.wake_up,
        on_sleep=orb.sleep,
        on_exit=orb.shutdown,
    )
    icon.run()  # blocks the main thread until "Exit" is clicked
