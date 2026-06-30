"""
orb_renderer.py
----------------
Sci-fi glowing orb: multiple tilted rotating rings with arc fragments,
a dense particle cloud, and a radial center bloom — all with RGBA glow
compositing. Inspired by the amber reactor-core aesthetic.
"""

import math
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

_BG    = (1, 1, 1)   # #010101 magic transparent — must match main.py
_N_PTS = 180         # points per ring arc
_N_PAR = 280         # particles

# Settable override color for the "custom" state (easter eggs) -- kept
# as plain module state, not threaded through render()'s signature, so
# nothing about the existing per-state palette logic has to change.
_override_color = (255, 180, 60)
_custom_smul = 1.0


def set_override_color(r, g, b):
    global _override_color
    _override_color = (r, g, b)


def set_custom_ring_speed(smul):
    """Ring-rotation speed multiplier for the "custom" state -- separate
    from the phase-step pulse speed JarvisOrb controls (that one drives
    color breathing; this one drives ring rotation)."""
    global _custom_smul
    _custom_smul = max(0.0, smul)


def _safe(r, g, b):
    return (max(2, min(255, r)), max(2, min(255, g)), max(2, min(255, b)))


def _hsv(h, s, v):
    h6 = h * 6.0
    i  = int(h6) % 6
    f  = h6 - int(h6)
    p, q, t = v*(1-s), v*(1-f*s), v*(1-(1-f)*s)
    r, g, b = [(v,t,p),(q,v,p),(p,v,t),(p,q,v),(t,p,v),(v,p,q)][i]
    return (int(r*255), int(g*255), int(b*255))


class OrbRenderer:
    def __init__(self, size=180):
        self.size = size
        rng = np.random.default_rng(7)

        # (radius_frac, tilt_rad, rot_speed, arc_fill)
        self.rings = [
            (0.74, 0.28,  0.009,  0.80),
            (0.54, 0.50, -0.013,  0.65),
            (0.90, 0.14,  0.005,  0.55),
            (0.38, 0.65,  0.022,  0.50),
            (0.62, 0.38, -0.007,  0.72),
        ]
        self.rot  = np.zeros(len(self.rings))

        # Particles ride near ring paths
        n = _N_PAR
        self.p_ring  = rng.integers(0, len(self.rings), n)
        self.p_angle = rng.uniform(0, math.tau, n)
        self.p_roff  = rng.normal(0, 0.025, n)
        self.p_speed = rng.uniform(0.003, 0.010, n) * rng.choice([-1,1], n)
        self.p_phase = rng.uniform(0, math.tau, n)

    # ── palette ────────────────────────────────────────────────────────────────

    @staticmethod
    def _palette(state, phase):
        p = (math.sin(phase * math.tau) + 1) / 2
        if state == "idle":
            core   = _safe(255, int(155+60*p), int(15*p))
            bright = _safe(255, int(210+35*p), int(40*p))
            glow   = _safe(int(190+50*p), int(85+40*p), 5)
        elif state == "listening":
            core   = _safe(int(220+35*p), int(230+25*p), 255)
            bright = (255, 255, 255)
            glow   = _safe(int(100+80*p), int(130+80*p), int(180+60*p))
        elif state == "processing":
            core   = _safe(255, int(130+90*p), int(10*p))
            bright = _safe(255, int(180+60*p), int(20*p))
            glow   = _safe(int(210+40*p), int(70+50*p), 5)
        elif state == "speaking":
            core   = _safe(int(30*p), 255, int(90+80*p))
            bright = _safe(int(80*p), 255, int(160+70*p))
            glow   = _safe(5, int(160+70*p), int(70+60*p))
        elif state == "error":
            core   = _safe(255, int(40+60*p), int(40+60*p))
            bright = _safe(255, int(70+80*p), int(70+80*p))
            glow   = _safe(int(210+40*p), 5, 5)
        elif state == "background_processing":
            # deep breathing gold/violet -- distinguishes unattended
            # heartbeat work from the amber idle/processing states
            core   = _safe(int(150+60*p), int(60+40*p), int(190+50*p))
            bright = _safe(int(190+40*p), int(100+50*p), int(230+25*p))
            glow   = _safe(int(120+50*p), int(30+30*p), int(160+60*p))
        elif state == "custom":
            # easter eggs -- breathes around whatever color was set via
            # set_override_color(), same p-based pulsing as every other
            # state, just driven by an external color instead of a fixed
            # palette
            r, g, b = _override_color
            core   = _safe(r, g, b)
            bright = _safe(min(255, r + 60), min(255, g + 60), min(255, b + 60))
            glow   = _safe(int(r * 0.7), int(g * 0.7), int(b * 0.7))
        else:
            core = bright = glow = (110, 110, 110)
        return core, bright, glow

    # ── geometry ───────────────────────────────────────────────────────────────

    def _project_ring(self, ring_idx, angle_offset):
        r_frac, tilt, _, _ = self.rings[ring_idx]
        R    = self.size * 0.44 * r_frac
        half = self.size * 0.5
        angs = np.linspace(0, math.tau, _N_PTS, endpoint=False) + angle_offset
        ct, st = math.cos(tilt), math.sin(tilt)
        x3 = R * np.cos(angs)
        y3 = R * np.sin(angs) * ct
        z3 = R * np.sin(angs) * st
        scale = 1.0 + z3 / (self.size * 0.85)
        x2 = half + x3 * scale
        y2 = half + y3 * scale
        depth = (z3 / R + 1.0) * 0.5   # 0=back 1=front
        return x2, y2, depth

    def _project_particle(self, ri, angle):
        r_frac, tilt, _, _ = self.rings[ri]
        R    = self.size * 0.44 * r_frac
        half = self.size * 0.5
        ct, st = math.cos(tilt), math.sin(tilt)
        x3 = R * math.cos(angle)
        y3 = R * math.sin(angle) * ct
        z3 = R * math.sin(angle) * st
        scale = 1.0 + z3 / (self.size * 0.85)
        return half + x3*scale, half + y3*scale, (z3/R + 1.0)*0.5

    # ── render ─────────────────────────────────────────────────────────────────

    def render(self, state, phase):
        p      = (math.sin(phase * math.tau) + 1) / 2
        if state == "idle":
            smul = 1.0
        elif state == "background_processing":
            smul = 1.2
        elif state == "custom":
            smul = _custom_smul
        else:
            smul = 1.8
        core_c, bright_c, glow_c = self._palette(state, phase)

        # Advance simulation
        for i, (_, _, spd, _) in enumerate(self.rings):
            self.rot[i] += spd * smul
        self.p_angle += self.p_speed * smul

        S = self.size
        half = S * 0.5

        # Two RGBA layers: glow (blurred later) + sharp detail
        glow_img  = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        sharp_img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_img)
        sd = ImageDraw.Draw(sharp_img)

        # ── rings ─────────────────────────────────────────────────────────────
        for ri, (_, _, _, fill) in enumerate(self.rings):
            x2, y2, depth = self._project_ring(ri, self.rot[ri])
            pts = list(zip(x2.tolist(), y2.tolist()))
            n = len(pts)

            # Break ring into arc + gap based on fill ratio
            seg = max(4, int(n * fill * (0.9 + 0.1*p)))
            gap = max(2, n - seg)

            for start in range(0, n, seg + gap):
                end = min(start + seg, n)
                chunk = pts[start:end]
                if len(chunk) < 2:
                    continue
                for k in range(len(chunk) - 1):
                    idx = start + k
                    d   = float((depth[idx] + depth[min(idx+1, n-1)]) * 0.5)
                    # glow line
                    ga  = int(100 * d * fill)
                    gd.line([chunk[k], chunk[k+1]],
                            fill=(*glow_c, ga),
                            width=max(1, int(6 * d)))
                    # bright line
                    sa  = int(210 * d)
                    sd.line([chunk[k], chunk[k+1]],
                            fill=(*bright_c, sa), width=1)

                # Bright node at arc endpoints
                ex, ey = chunk[-1]
                d_end  = float(depth[min(end-1, n-1)])
                nr = max(1, int(4 * d_end))
                sd.ellipse([ex-nr, ey-nr, ex+nr, ey+nr],
                           fill=(*bright_c, int(220 * d_end)))

        # ── particles ─────────────────────────────────────────────────────────
        for i in range(_N_PAR):
            ri  = int(self.p_ring[i])
            r_frac, tilt, _, _ = self.rings[ri]
            R   = self.size * 0.44 * (r_frac + float(self.p_roff[i]))
            ang = float(self.p_angle[i])
            ct, st = math.cos(tilt), math.sin(tilt)
            x3  = R * math.cos(ang)
            y3  = R * math.sin(ang) * ct
            z3  = R * math.sin(ang) * st
            sc  = 1.0 + z3 / (S * 0.85)
            px  = half + x3 * sc
            py  = half + y3 * sc
            d   = (z3 / max(R, 1) + 1.0) * 0.5

            flk = (math.sin(float(self.p_phase[i]) + phase * math.tau * 4) + 1) * 0.5
            a   = int(190 * d * (0.55 + 0.45 * flk))
            rp  = max(1, int(3 * d))
            rg  = max(2, int(9 * d))

            gd.ellipse([px-rg, py-rg, px+rg, py+rg],
                       fill=(*glow_c, int(a * 0.35)))
            sd.ellipse([px-rp, py-rp, px+rp, py+rp],
                       fill=(*core_c, a))

        # ── center bloom ──────────────────────────────────────────────────────
        bloom = [
            (0.24, 20 + int(8*p)),
            (0.15, 45 + int(15*p)),
            (0.09, 90 + int(25*p)),
            (0.04, 160 + int(40*p)),
            (0.015, 230 + int(20*p)),
            (0.005, 255),
        ]
        for frac, alpha in bloom:
            r = int(S * frac)
            gd.ellipse([half-r, half-r, half+r, half+r],
                       fill=(*glow_c, min(255, alpha)))
        for frac, alpha in bloom[-3:]:
            r = int(S * frac)
            sd.ellipse([half-r, half-r, half+r, half+r],
                       fill=(*bright_c, min(255, int(alpha * 0.8))))

        # ── listening center white pulse ───────────────────────────────────────
        if state == "listening":
            for frac, alpha in [(0.12, int(15+5*p)), (0.06, int(30+10*p)),
                                (0.025, int(55+15*p)), (0.008, int(80+20*p))]:
                r = int(S * frac)
                sd.ellipse([half-r, half-r, half+r, half+r],
                           fill=(255, 255, 255, min(255, alpha)))

        # ── composite ─────────────────────────────────────────────────────────
        blur_r = max(3, S // 55)
        glow_blurred = glow_img.filter(ImageFilter.GaussianBlur(radius=blur_r))

        # Start fully transparent (magic bg color), paint only where alpha > 0
        out = Image.new("RGB", (S, S), _BG)
        out.paste(glow_blurred.convert("RGB"), mask=glow_blurred.split()[3])
        out.paste(sharp_img.convert("RGB"),    mask=sharp_img.split()[3])
        return out
