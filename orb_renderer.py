"""
orb_renderer.py
----------------
"Seraph / Fallen" duality core -- a divine 3D orb with two natures:

  ANGELIC (default): a burning white-gold star with a lens-flare cross,
  wrapped in counter-rotating 3D halos seen at three-quarter angle, a
  crown of pulsing light rays, and motes of light ascending around it.

  DEMONIC (error state, red easter-egg overrides, or set_alignment):
  the core eclipses -- a black sphere with a burning crimson corona --
  the halos fracture into jagged broken arcs, the crown rays flicker
  like flame, and the motes sink like dying embers.

  The whole halo assembly precesses in 3D (global yaw), and halos are
  depth-composited around the core: back half behind, front half in
  front, so they genuinely orbit a solid center.

Public API (superset of v1/v2 -- main.py needs no changes):
  OrbRenderer(size).render(state, phase) -> RGB Image
  set_override_color(r,g,b), set_custom_ring_speed(smul),
  set_idle_tint(rgb|None), set_alignment("angel"|"demon"|"auto")
"""

import math
import random
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

_BG    = (1, 1, 1)   # #010101 magic transparent — must match main.py
_N_MOTES = 170       # ascending/sinking light motes
_HALO_PTS = 140      # points per halo ellipse

_override_color = (255, 180, 60)
_custom_smul = 1.0
_idle_tint = None
_alignment = "auto"   # "angel" | "demon" | "auto"


def set_override_color(r, g, b):
    global _override_color
    _override_color = (r, g, b)


def set_custom_ring_speed(smul):
    global _custom_smul
    _custom_smul = max(0.0, smul)


def set_idle_tint(rgb):
    """rgb tuple to tint the idle orb with, or None for stock gold."""
    global _idle_tint
    _idle_tint = rgb


def set_alignment(mode):
    """Forces the orb's nature: 'angel', 'demon', or 'auto' (state-driven)."""
    global _alignment
    if mode in ("angel", "demon", "auto"):
        _alignment = mode


_eyes_mode = False


def set_eyes_mode(on):
    """Demon-mode overlay: replaces the orb entirely with a pair of
    watching demonic eyes (blinking, wandering gaze) until turned off."""
    global _eyes_mode
    _eyes_mode = bool(on)


def _safe(r, g, b):
    return (max(2, min(255, int(r))), max(2, min(255, int(g))), max(2, min(255, int(b))))


def _is_reddish(rgb):
    r, g, b = rgb
    return r > 150 and r > g * 1.8 and r > b * 1.8


class OrbRenderer:
    def __init__(self, size=180):
        self.size = size
        rng = np.random.default_rng(7)

        # (radius_frac, tilt_rad, rot_speed) -- three halos
        self.halos = [
            (0.80, 0.42,  0.010),
            (0.62, 0.95, -0.014),
            (0.94, 0.22,  0.006),
        ]
        self.rot = np.zeros(len(self.halos))
        self.yaw = 0.0
        self.tick = 0

        n = _N_MOTES
        self.m_r      = rng.uniform(0.18, 0.46, n)        # orbit radius frac
        self.m_angle  = rng.uniform(0, math.tau, n)
        self.m_speed  = rng.uniform(0.002, 0.008, n) * rng.choice([-1, 1], n)
        self.m_vert   = rng.uniform(0, 1, n)              # vertical cycle pos
        self.m_vspd   = rng.uniform(0.0018, 0.0045, n)    # vertical speed
        self.m_flick  = rng.uniform(0, math.tau, n)

        # per-ray phase offsets for the crown
        self.ray_seed = rng.uniform(0, math.tau, 16)

        # demon-eyes state: blink scheduling + smooth wandering gaze
        self._next_blink = 90
        self._blink_t = 0
        self._rng = random.Random(13)

    # ── nature / palette ───────────────────────────────────────────────────────

    @staticmethod
    def _demonic(state):
        if _alignment == "demon":
            return True
        if _alignment == "angel":
            return False
        if state == "error":
            return True
        if state == "custom" and _is_reddish(_override_color):
            return True
        return False

    @staticmethod
    def _palette(state, phase, demonic):
        """Returns (core, bright, glow). Angelic states are white-gold
        variations; demonic flips everything to blood-and-ember."""
        p = (math.sin(phase * math.tau) + 1) / 2
        if demonic:
            core   = _safe(255, 60 + 50 * p, 25 * p)
            bright = _safe(255, 110 + 70 * p, 40 + 40 * p)
            glow   = _safe(160 + 60 * p, 10 + 20 * p, 8)
            return core, bright, glow
        if state == "idle":
            if _idle_tint:
                r, g, b = _idle_tint
                core   = _safe(r*(0.85+0.15*p), g*(0.85+0.15*p), b*(0.85+0.15*p))
                bright = _safe(min(255, r+70), min(255, g+70), min(255, b+70))
                glow   = _safe(r*0.7, g*0.7, b*0.7)
            else:
                core   = _safe(255, 225 + 25 * p, 150 + 40 * p)
                bright = _safe(255, 250, 220 + 30 * p)
                glow   = _safe(230 + 25 * p, 175 + 30 * p, 60)
        elif state == "listening":
            core   = _safe(230 + 25 * p, 240, 255)
            bright = (255, 255, 255)
            glow   = _safe(140 + 60 * p, 170 + 60 * p, 230 + 25 * p)
        elif state == "processing":
            core   = _safe(255, 190 + 50 * p, 60 + 40 * p)
            bright = _safe(255, 230, 130 + 60 * p)
            glow   = _safe(235, 130 + 50 * p, 20)
        elif state == "speaking":
            core   = _safe(190 + 50 * p, 255, 200 + 40 * p)
            bright = _safe(230, 255, 240)
            glow   = _safe(60, 190 + 50 * p, 120 + 50 * p)
        elif state == "background_processing":
            core   = _safe(200 + 40 * p, 170 + 40 * p, 255)
            bright = _safe(235, 215, 255)
            glow   = _safe(140 + 40 * p, 90 + 40 * p, 210)
        elif state == "custom":
            r, g, b = _override_color
            core   = _safe(r, g, b)
            bright = _safe(min(255, r+70), min(255, g+70), min(255, b+70))
            glow   = _safe(r*0.7, g*0.7, b*0.7)
        else:  # error handled by demonic branch above; fallback gray
            core = bright = glow = (150, 150, 150)
        return core, bright, glow

    # ── geometry ───────────────────────────────────────────────────────────────

    def _halo_points(self, idx, angle_offset):
        r_frac, tilt, _ = self.halos[idx]
        R    = self.size * 0.44 * r_frac
        half = self.size * 0.5
        angs = np.linspace(0, math.tau, _HALO_PTS, endpoint=False) + angle_offset
        ct, st = math.cos(tilt), math.sin(tilt)
        x3 = R * np.cos(angs)
        y3 = R * np.sin(angs) * ct
        z3 = R * np.sin(angs) * st
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        xr = x3 * cy + z3 * sy
        zr = -x3 * sy + z3 * cy
        scale = 1.0 + zr / (self.size * 0.85)
        return half + xr * scale, half + y3 * scale, (zr / R + 1.0) * 0.5

    # ── painters ───────────────────────────────────────────────────────────────

    def _paint_halos(self, gd, sd, p, bright_c, glow_c, front, demonic):
        """Halos: continuous rings of light (angelic) or fractured jagged
        arcs (demonic), split by depth around the core."""
        rng = random.Random(self.tick // 4 if demonic else 0)
        for hi in range(len(self.halos)):
            x2, y2, depth = self._halo_points(hi, self.rot[hi])
            n = _HALO_PTS
            for k in range(n - 1):
                d = float((depth[k] + depth[k+1]) * 0.5)
                if (d >= 0.5) != front:
                    continue
                if demonic:
                    # fracture: drop ~35% of segments, jitter the rest
                    if rng.random() < 0.35:
                        continue
                    jx, jy = rng.uniform(-1.5, 1.5), rng.uniform(-1.5, 1.5)
                    a = (x2[k] + jx, y2[k] + jy)
                    b = (x2[k+1] + jx, y2[k+1] + jy)
                else:
                    a, b = (x2[k], y2[k]), (x2[k+1], y2[k+1])
                shimmer = 0.75 + 0.25 * math.sin(p * math.tau + k * 0.22 + hi)
                gd.line([a, b], fill=(*glow_c, int(160 * d * shimmer)),
                        width=max(2, int(10 * d)))
                sd.line([a, b], fill=(*bright_c, int(235 * d * shimmer)),
                        width=2 if d > 0.55 else 1)

    def _paint_motes(self, gd, sd, phase, core_c, glow_c, front, demonic):
        """Motes orbit the core while cycling vertically -- ascending in
        angelic form, sinking like embers in demonic form."""
        S = self.size
        half = S * 0.5
        span = S * 0.34
        for i in range(_N_MOTES):
            R = S * 0.44 * float(self.m_r[i])
            ang = float(self.m_angle[i])
            cy, sy = math.cos(self.yaw), math.sin(self.yaw)
            x3 = R * math.cos(ang)
            z3 = R * math.sin(ang) * 0.55
            xr = x3 * cy + z3 * sy
            zr = -x3 * sy + z3 * cy
            d = (zr / max(R, 1) + 1.0) * 0.5
            if (d >= 0.5) != front:
                continue
            t = float(self.m_vert[i])          # 0..1 vertical cycle
            vy = (t - 0.5) * span
            if not demonic:
                vy = -vy                       # angelic motes rise
            px = half + xr * (1.0 + zr / (S * 0.85))
            py = half + vy
            edge_fade = 1.0 - abs(t - 0.5) * 2.0   # fade at cycle ends
            flk = (math.sin(float(self.m_flick[i]) + phase * math.tau * 3) + 1) * 0.5
            a = int(200 * d * edge_fade * (0.5 + 0.5 * flk))
            if a < 6:
                continue
            rp = max(1, int(2.6 * d))
            rg = max(2, int(7 * d))
            gd.ellipse([px-rg, py-rg, px+rg, py+rg], fill=(*glow_c, int(a * 0.35)))
            sd.ellipse([px-rp, py-rp, px+rp, py+rp], fill=(*core_c, a))

    def _paint_crown(self, gd, sd, phase, bright_c, glow_c, demonic):
        """Crown of light rays radiating from the core. Angelic: slow
        majestic pulse, upward-biased. Demonic: fast flame flicker."""
        S = self.size
        half = S * 0.5
        n_rays = 12
        rng = random.Random(self.tick // 2) if demonic else None
        for i in range(n_rays):
            # offset by half a step so no ray sits exactly on a cardinal
            # axis (12 evenly spaced rays otherwise read as a crosshair)
            a = (i + 0.5) * math.tau / n_rays - math.pi / 2 + self.yaw * 0.15
            seed = float(self.ray_seed[i])
            if demonic:
                pulse = 0.4 + 0.6 * rng.random()
            else:
                pulse = 0.45 + 0.55 * math.sin(phase * math.tau * 2 + seed)
            major = (i % 4 == 0)                             # 3 majors, 120° apart
            up_bias = 0.6 + 0.4 * max(0.0, -math.sin(a))     # longer upward
            r0 = S * 0.115
            reach = (0.16 if major else 0.075) + 0.10 * pulse
            r1 = r0 + S * reach * up_bias
            x0, y0 = half + r0 * math.cos(a), half + r0 * math.sin(a)
            xm, ym = half + (r0 + (r1 - r0) * 0.5) * math.cos(a), half + (r0 + (r1 - r0) * 0.5) * math.sin(a)
            x1, y1 = half + r1 * math.cos(a), half + r1 * math.sin(a)
            alpha = int(190 * pulse * up_bias)
            # tapered ray: bright thick base, faint thin tip
            sd.line([(x0, y0), (xm, ym)], fill=(*bright_c, alpha),
                    width=2 if major else 1)
            sd.line([(xm, ym), (x1, y1)], fill=(*bright_c, int(alpha * 0.45)), width=1)
            gd.line([(x0, y0), (xm, ym)], fill=(*glow_c, min(255, alpha)), width=4)

    def _paint_core_angel(self, gd, sd, p, core_c, bright_c, glow_c):
        """Burning star: soft halo, shaded ball toward an upper-left
        highlight, plus a thin lens-flare cross."""
        S = self.size
        half = S * 0.5
        for frac, alpha in [(0.27, 24 + int(8*p)), (0.18, 50 + int(15*p)),
                            (0.11, 100 + int(25*p))]:
            r = int(S * frac)
            gd.ellipse([half-r, half-r, half+r, half+r], fill=(*glow_c, alpha))
        R0 = S * 0.088
        hx, hy = -R0 * 0.36, -R0 * 0.36
        for i in range(12):
            t = i / 11.0
            r = R0 * (1.0 - 0.82 * t)
            cx, cy = half + hx * t, half + hy * t
            col = (
                int(core_c[0] * 0.6 + (bright_c[0] - core_c[0] * 0.6) * t),
                int(core_c[1] * 0.6 + (bright_c[1] - core_c[1] * 0.6) * t),
                int(core_c[2] * 0.6 + (bright_c[2] - core_c[2] * 0.6) * t),
            )
            sd.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(*col, int(165 + 90 * t)))
        r = max(2, int(R0 * 0.16))
        sd.ellipse([half+hx-r, half+hy-r, half+hx+r, half+hy+r],
                   fill=(255, 255, 255, 240))
        # star-glint -- vertical-dominant divine flare, tapered ends
        L = S * (0.115 + 0.03 * p)
        for (dx, dy, ll) in ((0, 1, L), (1, 0, L * 0.45)):
            # faint full length, brighter half length -- fakes a taper
            sd.line([(half - dx*ll, half - dy*ll), (half + dx*ll, half + dy*ll)],
                    fill=(*bright_c, 70), width=1)
            sd.line([(half - dx*ll*0.5, half - dy*ll*0.5),
                     (half + dx*ll*0.5, half + dy*ll*0.5)],
                    fill=(*bright_c, 160), width=1)

    def _paint_core_demon(self, gd, sd, p, core_c, bright_c, glow_c):
        """Eclipse: a black sphere edged by a burning corona -- the light
        is behind the darkness."""
        S = self.size
        half = S * 0.5
        R0 = S * 0.095
        # corona glow (blurred layer)
        for frac, alpha in [(0.24, 60 + int(20*p)), (0.16, 110 + int(30*p)),
                            (0.115, 170 + int(30*p))]:
            r = int(S * frac)
            gd.ellipse([half-r, half-r, half+r, half+r], fill=(*glow_c, alpha))
        # licking corona flames -- short radial ticks around the rim
        rng = random.Random(self.tick // 2)
        for i in range(26):
            a = i * math.tau / 26 + rng.uniform(-0.06, 0.06)
            r1 = R0 * (1.05 + 0.28 * rng.random())
            sd.line([(half + R0 * 0.98 * math.cos(a), half + R0 * 0.98 * math.sin(a)),
                     (half + r1 * math.cos(a), half + r1 * math.sin(a))],
                    fill=(*core_c, rng.randint(120, 220)), width=2)
        # bright rim ring
        sd.ellipse([half-R0, half-R0, half+R0, half+R0],
                   outline=(*bright_c, 235), width=3)
        # the void itself (near-black, NOT the magic bg color)
        rv = R0 - 2
        sd.ellipse([half-rv, half-rv, half+rv, half+rv], fill=(10, 4, 6, 255))

    def _paint_energy_arcs(self, sd, bright_c, demonic):
        S = self.size
        half = S * 0.5
        rng = random.Random(self.tick // 3)
        n_arcs = rng.randint(2, 4) if demonic else rng.randint(1, 2)
        for _ in range(n_arcs):
            ang = rng.uniform(0, math.tau)
            r_end = S * rng.uniform(0.24, 0.42)
            segs = 6 if demonic else 5
            pts = []
            for s in range(segs + 1):
                t = s / segs
                rr = S * 0.09 + (r_end - S * 0.09) * t
                j = S * (0.03 if demonic else 0.02)
                pts.append((half + rr * math.cos(ang) + rng.uniform(-j, j),
                            half + rr * math.sin(ang) + rng.uniform(-j, j)))
            alpha = rng.randint(110, 220) if demonic else rng.randint(80, 170)
            for k in range(segs):
                sd.line([pts[k], pts[k+1]], fill=(*bright_c, alpha), width=1)

    def _paint_voice_ripples(self, sd, phase, bright_c):
        S = self.size
        half = S * 0.5
        for offset in (0.0, 0.5):
            t = (phase * 2 + offset) % 1.0
            r = S * (0.13 + 0.33 * t)
            alpha = int(110 * (1.0 - t))
            if alpha > 8:
                sd.ellipse([half-r, half-r, half+r, half+r],
                           outline=(*bright_c, alpha), width=1)

    # ── demon eyes ─────────────────────────────────────────────────────────────

    def _render_eyes(self, phase):
        """A pair of slanted demonic eyes that watch, wander, and blink.
        Replaces the whole orb while eyes mode is on (demon focus mode)."""
        S = self.size
        half = S * 0.5
        self.tick += 1
        t = self.tick

        core_c   = (255, 70, 25)
        bright_c = (255, 150, 60)
        glow_c   = (165, 18, 8)

        # blink scheduling: fast 10-frame close/open, 3-10s apart
        if self._blink_t > 0:
            self._blink_t -= 1
        elif t >= self._next_blink:
            self._blink_t = 10
            self._next_blink = t + self._rng.randint(90, 300)
        blink = abs(self._blink_t - 5) / 5.0 if self._blink_t > 0 else 1.0
        blink = blink ** 1.5  # snappier close

        # wandering gaze -- layered slow sines feel deliberate, and it
        # periodically re-centers as if looking straight at you
        gx = 0.45 * math.sin(t * 0.017) + 0.25 * math.sin(t * 0.0071 + 2.1)
        gy = 0.30 * math.sin(t * 0.011 + 1.0)
        stare = (math.sin(t * 0.004) + 1) / 2       # 0..1, 1 = locked on you
        if stare > 0.72:
            gx *= 0.15
            gy *= 0.15
        breathe = 1.0 + 0.03 * math.sin(phase * math.tau)

        glow_img  = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        sharp_img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_img)
        sd = ImageDraw.Draw(sharp_img)

        # smoky aura behind both eyes
        for frac, alpha in [(0.42, 26), (0.30, 46), (0.20, 70)]:
            rx, ry = int(S * frac), int(S * frac * 0.55)
            gd.ellipse([half - rx, S * 0.46 - ry, half + rx, S * 0.46 + ry],
                       fill=(*glow_c, alpha))

        # sinking embers around the eyes (reuse mote arrays)
        span = S * 0.36
        for i in range(0, _N_MOTES, 3):
            tt = float(self.m_vert[i])
            ex = half + math.cos(float(self.m_angle[i])) * S * 0.40 * float(self.m_r[i]) * 2.0
            ey = S * 0.46 + (tt - 0.5) * span
            fade = 1.0 - abs(tt - 0.5) * 2.0
            flk = (math.sin(float(self.m_flick[i]) + phase * math.tau * 3) + 1) * 0.5
            a = int(150 * fade * (0.4 + 0.6 * flk))
            if a > 8:
                sd.ellipse([ex-1.5, ey-1.5, ex+1.5, ey+1.5], fill=(*core_c, a))
        self.m_vert = (self.m_vert + self.m_vspd * 1.4) % 1.0

        for side in (-1, 1):
            cx = half + side * S * 0.19
            cy = S * 0.46
            w = S * 0.155 * breathe
            h = S * 0.082 * breathe * max(0.04, blink)
            slant = S * 0.045   # outer corners raised = angry

            # eye outline: pointed almond, tilted so the outer corner
            # rides higher than the inner one
            top_pts, bot_pts = [], []
            n = 15
            for k in range(n):
                x = -1.0 + 2.0 * k / (n - 1)
                lid = (1.0 - x * x) ** 0.75
                tilt = -slant * (x * side)   # raises the outer end
                px = cx + x * w
                top_pts.append((px, cy - h * lid + tilt))
                bot_pts.append((px, cy + h * 0.8 * lid + tilt))
            poly = top_pts + bot_pts[::-1]

            # glow silhouette, dark sclera, burning rim
            gd.polygon(poly, fill=(*glow_c, 150))
            sd.polygon(poly, fill=(28, 4, 4, 245))
            sd.line(poly + [poly[0]], fill=(*bright_c, 220), width=2)

            if blink > 0.15:
                # ember iris with a black vertical slit pupil, shifted by gaze
                ix = cx + gx * w * 0.42
                iy = cy + gy * h * 0.55
                ir = h * 0.85
                for rr, col, al in [(ir, glow_c, 120), (ir * 0.7, core_c, 190),
                                    (ir * 0.45, bright_c, 235)]:
                    sd.ellipse([ix-rr, iy-rr, ix+rr, iy+rr], fill=(*col, al))
                pw = max(2, ir * 0.22)
                ph_ = ir * 1.05 * blink
                sd.ellipse([ix-pw, iy-ph_, ix+pw, iy+ph_], fill=(6, 2, 2, 255))
                # pin glint above the pupil
                sd.ellipse([ix - 2, iy - ir * 0.5 - 2, ix + 2, iy - ir * 0.5 + 2],
                           fill=(255, 230, 200, 220))

        blur_r = max(3, S // 55)
        glow_blurred = glow_img.filter(ImageFilter.GaussianBlur(radius=blur_r))
        out = Image.new("RGB", (S, S), _BG)
        out.paste(glow_blurred.convert("RGB"), mask=glow_blurred.split()[3])
        out.paste(sharp_img.convert("RGB"), mask=sharp_img.split()[3])
        return out

    # ── render ─────────────────────────────────────────────────────────────────

    def render(self, state, phase):
        if _eyes_mode:
            return self._render_eyes(phase)
        p = (math.sin(phase * math.tau) + 1) / 2
        if state == "idle":
            smul = 1.0
        elif state == "background_processing":
            smul = 1.2
        elif state == "custom":
            smul = _custom_smul
        else:
            smul = 1.8
        demonic = self._demonic(state)
        if demonic:
            smul *= 1.35   # hell is restless
        core_c, bright_c, glow_c = self._palette(state, phase, demonic)

        # advance simulation
        for i, (_, _, spd) in enumerate(self.halos):
            self.rot[i] += spd * smul
        self.m_angle += self.m_speed * smul
        self.m_vert = (self.m_vert + self.m_vspd * smul) % 1.0
        self.yaw = (self.yaw + 0.0035 * smul) % math.tau
        self.tick += 1

        S = self.size
        glow_img  = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        sharp_img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_img)
        sd = ImageDraw.Draw(sharp_img)

        # 1 — behind the core
        self._paint_halos(gd, sd, p, bright_c, glow_c, front=False, demonic=demonic)
        self._paint_motes(gd, sd, phase, core_c, glow_c, front=False, demonic=demonic)

        # 2 — crown rays sit just behind the core's face
        self._paint_crown(gd, sd, phase, bright_c, glow_c, demonic)

        # 3 — the core itself
        if demonic:
            self._paint_core_demon(gd, sd, p, core_c, bright_c, glow_c)
        else:
            self._paint_core_angel(gd, sd, p, core_c, bright_c, glow_c)

        # 4 — arcs/ripples above the core
        if demonic or state in ("listening", "processing", "speaking",
                                "background_processing", "custom"):
            self._paint_energy_arcs(sd, bright_c, demonic)
        if state == "speaking":
            self._paint_voice_ripples(sd, phase, bright_c)

        # 5 — in front of the core
        self._paint_halos(gd, sd, p, bright_c, glow_c, front=True, demonic=demonic)
        self._paint_motes(gd, sd, phase, core_c, glow_c, front=True, demonic=demonic)

        # listening: white center pulse
        if state == "listening" and not demonic:
            half = S * 0.5
            for frac, alpha in [(0.12, int(15+5*p)), (0.06, int(30+10*p)),
                                (0.025, int(55+15*p)), (0.008, int(80+20*p))]:
                r = int(S * frac)
                sd.ellipse([half-r, half-r, half+r, half+r],
                           fill=(255, 255, 255, min(255, alpha)))

        blur_r = max(3, S // 55)
        glow_blurred = glow_img.filter(ImageFilter.GaussianBlur(radius=blur_r))
        out = Image.new("RGB", (S, S), _BG)
        out.paste(glow_blurred.convert("RGB"), mask=glow_blurred.split()[3])
        out.paste(sharp_img.convert("RGB"), mask=sharp_img.split()[3])
        return out
