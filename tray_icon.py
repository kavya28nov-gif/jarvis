"""
tray_icon.py
-------------
System tray anchor for Jarvis. Runs on the main thread (pystray owns a
Win32 message loop on Windows, same as Tkinter does, so it can't share a
thread with the orb window). The tray icon stays alive for the whole
process lifetime; "Exit" tears everything down cleanly.
"""

import logging

import pystray
from PIL import Image, ImageDraw

logger = logging.getLogger("jarvis.tray")


def _build_icon_image():
    """Small amber orb glyph -- static, just for the tray; the real 3D
    rings only render in the orb window."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    half = size // 2
    for r, alpha in [(28, 60), (20, 120), (13, 200), (7, 255)]:
        draw.ellipse(
            [half - r, half - r, half + r, half + r],
            fill=(255, 170, 40, alpha),
        )
    return img


def build_tray_icon(on_wake, on_sleep, on_exit):
    """on_wake/on_sleep/on_exit are zero-arg callables supplied by main.py.
    They run on pystray's own thread, so anything touching Tkinter must
    be marshalled back via root.after(0, ...) inside those callbacks."""

    def _wake(icon, item):
        try:
            on_wake()
        except Exception as e:
            logger.error(f"[tray] wake error: {e}")

    def _sleep(icon, item):
        try:
            on_sleep()
        except Exception as e:
            logger.error(f"[tray] sleep error: {e}")

    def _exit(icon, item):
        try:
            on_exit()
        except Exception as e:
            logger.error(f"[tray] exit error: {e}")
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Wake Up", _wake),
        pystray.MenuItem("Sleep", _sleep),
        pystray.MenuItem("Exit", _exit),
    )

    return pystray.Icon("jarvis", _build_icon_image(), "Jarvis", menu)
