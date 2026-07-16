"""Jarvis's eyes -- webcam hand-gesture control, fully local.

MediaPipe's pre-trained GestureRecognizer watches the webcam. Three kinds
of trigger:

  Held poses (hand must be still):
    Open palm        play/pause media
    Thumb up         next track
    Thumb down       previous track
    Pointing up      switch app window (alt+tab)

  Waves (hand sweeping across the frame, any shape):
    your right -> left   next tab (ctrl+tab)
    your left -> right   previous tab (ctrl+shift+tab)

  Transition:
    fist, then open it   close current tab (ctrl+w)

Privacy: frames are processed in-memory and dropped -- nothing is saved,
nothing leaves the machine (the model file is bundled in assets/).

Run standalone to see live detections WITHOUT sending any keys:
    python gesture_eyes.py          # prints what it sees
    python gesture_eyes.py --act    # actually fires the actions
"""

import logging
import os
import threading
import time
from collections import deque

import keyboard

logger = logging.getLogger("jarvis.eyes")

MODEL_PATH = os.path.join(os.path.dirname(__file__), "assets", "gesture_recognizer.task")
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
    "gesture_recognizer/float16/latest/gesture_recognizer.task"
)

# Held poses: category_name -> (hold seconds, keyboard.send() keys, label, orb RGB)
GESTURE_ACTIONS = {
    "Open_Palm":   (0.6, "play/pause media", "play/pause",     (80, 220, 120)),
    "Thumb_Up":    (0.5, "next track",       "next track",     (60, 210, 190)),
    "Thumb_Down":  (0.5, "previous track",   "previous track", (60, 160, 230)),
    "Pointing_Up": (0.6, "alt+tab",          "switch window",  (255, 200, 80)),
}

# Waves: the webcam image is un-mirrored, so a hand moving toward the
# USER'S left travels toward larger x in the frame. Keyed by sign(dx).
WAVE_ACTIONS = {
    +1: ("ctrl+tab",       "next tab",     (170, 120, 255)),   # your right -> left
    -1: ("ctrl+shift+tab", "previous tab", (120, 170, 255)),   # your left -> right
}

# Fist opening into a palm. NOT a fist *hold* -- an earlier draft mapped
# held-fist to alt+f4, but that turns "fist you meant to open" into a
# closed window if you hesitate; the release gesture replaced it.
FIST_RELEASE = ("ctrl+w", "close tab", (255, 80, 80))

MIN_SCORE = 0.55        # recognizer confidence gate
STABLE_FRAMES = 3       # frames before a pose counts as "settled" (transitions)
COOLDOWN_S = 1.2        # after any fire, ignore everything briefly
REARM_FRAMES = 4        # frames of no-gesture before the same hold can refire
SWIPE_MIN_DX = 0.32     # wave travel as a fraction of frame width
SWIPE_WINDOW_S = 0.7    # ...covered within this long
MOTION_WINDOW_S = 0.3   # hold gating: x-range window...
MOTION_EPS = 0.05       # ...and how much drift still counts as "still"
FPS_ACTIVE = 18         # processing cap while a hand is in frame
FPS_IDLE = 8            # lazy scan rate when no hand seen for a while
IDLE_AFTER_S = 3.0


def _ensure_model():
    """First-run model fetch, same pattern as openwakeword's ~5 MB
    download. No-op when assets/gesture_recognizer.task is present."""
    if os.path.exists(MODEL_PATH):
        return True
    try:
        import requests
        logger.info("Downloading gesture model (~8 MB, one time)...")
        r = requests.get(MODEL_URL, timeout=60)
        r.raise_for_status()
        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        with open(MODEL_PATH, "wb") as f:
            f.write(r.content)
        return True
    except Exception as e:
        logger.error(f"[eyes] model download failed: {e}")
        return False


class GestureEyes:
    """Owns the camera thread. start()/stop() are idempotent and safe
    from any thread; on_action(label, rgb) is called from the camera
    thread right after a gesture fires (main.py uses it to flash the orb).
    """

    def __init__(self, on_action=None, send_keys=True):
        self.on_action = on_action
        self.send_keys = send_keys
        self._thread = None
        self._stop = threading.Event()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return "Eyes are already on, sir."
        if not _ensure_model():
            return "Couldn't load the gesture model, sir."
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="gesture-eyes")
        self._thread.start()
        return "Eyes on. Show me a palm to play or pause."

    def stop(self):
        if not self.running:
            return "Eyes are already off, sir."
        self._stop.set()
        self._thread.join(timeout=3)
        self._thread = None
        return "Eyes off. Camera released."

    # ── camera loop ──────────────────────────────────────────────────────

    def _loop(self):
        try:
            import cv2
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions
            from mediapipe.tasks.python import vision

            recognizer = vision.GestureRecognizer.create_from_options(
                vision.GestureRecognizerOptions(
                    base_options=BaseOptions(model_asset_path=MODEL_PATH),
                    running_mode=vision.RunningMode.VIDEO,
                    num_hands=1,
                )
            )
            # CAP_DSHOW: the MSMF backend takes ~10s to open on some
            # Windows boxes; DirectShow opens near-instantly.
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            if not cap.isOpened():
                logger.error("[eyes] no webcam found.")
                return
            logger.info("[eyes] watching for gestures.")
        except Exception as e:
            logger.error(f"[eyes] init failed: {e}")
            return

        # held-pose state
        held_gesture = None      # pose currently being held toward a fire
        held_since = 0.0
        fired_gesture = None     # fired and not yet re-armed
        clear_frames = 0         # consecutive frames without the fired pose
        # pose-transition state (fist -> open)
        pose = None              # last raw pose seen
        pose_frames = 0          # how many consecutive frames it's been seen
        stable_pose = None       # pose confirmed for STABLE_FRAMES
        # wave state
        xhist = deque()          # (t, wrist x in 0..1), recent history
        # shared
        cooldown_until = 0.0
        last_hand_seen = 0.0
        # recognize_for_video demands strictly increasing timestamps;
        # a monotonic ms counter is simpler than trusting frame pacing
        ts_ms = 0

        try:
            while not self._stop.is_set():
                frame_budget = 1.0 / (
                    FPS_ACTIVE if time.time() - last_hand_seen < IDLE_AFTER_S else FPS_IDLE
                )
                t0 = time.time()

                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.5)
                    continue

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                ts_ms += max(1, int(frame_budget * 1000))
                result = recognizer.recognize_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts_ms
                )
                now = time.time()

                name, score = None, 0.0
                if result.gestures:
                    cat = result.gestures[0][0]
                    name, score = cat.category_name, cat.score
                hand_x = None
                if result.hand_landmarks:
                    last_hand_seen = now
                    hand_x = result.hand_landmarks[0][0].x  # wrist -- least jitter

                raw_pose = name if score >= MIN_SCORE else None
                self._step(raw_pose)

                # ── wave detection: net wrist travel inside a short window.
                # Hand leaving the frame resets it, so slow drifting around
                # the desk never accumulates into a phantom swipe.
                if hand_x is None:
                    xhist.clear()
                else:
                    xhist.append((now, hand_x))
                while xhist and now - xhist[0][0] > SWIPE_WINDOW_S:
                    xhist.popleft()

                swiped = False
                if xhist and now >= cooldown_until:
                    dx = xhist[-1][1] - xhist[0][1]
                    if abs(dx) >= SWIPE_MIN_DX:
                        keys, label, rgb_c = WAVE_ACTIONS[1 if dx > 0 else -1]
                        self._fire(keys, label, rgb_c)
                        cooldown_until = now + COOLDOWN_S
                        xhist.clear()
                        held_gesture = None
                        swiped = True

                # is the hand drifting right now? (gates the hold logic --
                # a palm sweeping through a wave must not fire play/pause)
                recent = [x for t, x in xhist if now - t <= MOTION_WINDOW_S]
                moving = bool(recent) and (max(recent) - min(recent) > MOTION_EPS)

                # ── pose transitions: fist settling into an open palm.
                # "Settled" = STABLE_FRAMES consecutive frames, so a flicker
                # mid-transition doesn't count. Dropping the hand makes None
                # the settled pose, which breaks the fist->palm chain.
                if raw_pose == pose:
                    pose_frames += 1
                else:
                    pose, pose_frames = raw_pose, 1
                if pose_frames == STABLE_FRAMES and pose != stable_pose:
                    prev_stable, stable_pose = stable_pose, pose
                    if (stable_pose == "Open_Palm" and prev_stable == "Closed_Fist"
                            and now >= cooldown_until and not swiped):
                        keys, label, rgb_c = FIST_RELEASE
                        self._fire(keys, label, rgb_c)
                        cooldown_until = now + COOLDOWN_S
                        # the palm now on screen came from the release --
                        # make it leave the frame before it can count as a
                        # play/pause hold
                        fired_gesture, held_gesture = "Open_Palm", None
                        clear_frames = 0

                # ── held poses ───────────────────────────────────────────
                gesture = raw_pose if raw_pose in GESTURE_ACTIONS else None

                if fired_gesture is not None:
                    # re-arm: the fired pose must leave the frame briefly
                    if gesture != fired_gesture:
                        clear_frames += 1
                        if clear_frames >= REARM_FRAMES:
                            fired_gesture = None
                            clear_frames = 0
                    else:
                        clear_frames = 0
                elif now >= cooldown_until and gesture is not None and not moving:
                    if gesture != held_gesture:
                        held_gesture, held_since = gesture, now
                    elif now - held_since >= GESTURE_ACTIONS[gesture][0]:
                        _, keys, label, rgb_c = GESTURE_ACTIONS[gesture]
                        self._fire(keys, label, rgb_c)
                        fired_gesture, held_gesture = gesture, None
                        clear_frames = 0
                        cooldown_until = now + COOLDOWN_S
                else:
                    held_gesture = None

                leftover = frame_budget - (time.time() - t0)
                if leftover > 0:
                    self._stop.wait(leftover)
        finally:
            cap.release()
            recognizer.close()
            logger.info("[eyes] camera released.")

    def _step(self, *_):
        """Hook point for the standalone debug mode (overridden there to
        print live detections); a no-op inside Jarvis."""

    def _fire(self, keys, label, rgb):
        logger.info(f"[eyes] {label} ({keys})")
        if self.send_keys:
            try:
                keyboard.send(keys)
            except Exception as e:
                logger.error(f"[eyes] key send failed: {e}")
        if self.on_action:
            try:
                self.on_action(label, rgb)
            except Exception:
                pass


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    act = "--act" in sys.argv

    class _DebugEyes(GestureEyes):
        _last_printed = None

        def _step(self, name, *_):
            if name != self._last_printed:
                self._last_printed = name
                print(f"  seeing: {name or '—'}", flush=True)

    eyes = _DebugEyes(send_keys=act,
                      on_action=lambda label, rgb: print(f">>> FIRED: {label}", flush=True))
    print(f"Gesture debug (keys {'LIVE' if act else 'suppressed -- pass --act to send'}). Ctrl+C to quit.")
    print(eyes.start(), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(eyes.stop())
