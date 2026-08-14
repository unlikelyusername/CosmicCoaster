# /effects/interference.py
#
# Cosine interference — 6 wave sources drift around the grid.
# Each pixel accumulates cos(dist_to_source * freq + phase + t) for all
# sources. The sum maps through a full HSV hue palette so interference
# bands are rainbow-colored. Sources bounce off walls; the pattern
# morphs continuously as sources cross and diverge.
#
# Tuning constants:
#   _FREQ      — spatial frequency (LUT steps per pixel of distance)
#                lower = broader bands, higher = tighter ripples
#                good range: 10–20
#   _T_STEP    — time advance per frame; controls animation speed
#                good range: 2–6
#   src speed  — in init(), randint(30, 75) in FP8 units
#                ÷256 = pixels per frame; 40/256 ≈ 0.16 px/frame at 20fps
#
# Effect contract:
#   graphics = None
#   cu       = None
#   init()
#   draw()
#   deinit()

import math
import random
from micropython import const

graphics = None
cu       = None

GEOMETRY = "any"

W = 0    # injected by the loader
H = 0

_W    = 32
_H    = 32
_NPIX = 1024   # W * H
_NSRC  = const(6)

# Spatial frequency: LUT steps per pixel of source distance.
# 256 LUT steps = one full cosine cycle.
# _FREQ=16 → cycle length = 256/16 = 16 pixels ≈ 2 full cycles across grid.
_FREQ   = const(16)

# Time advance per frame (0..255 wrapping).
_T_STEP = const(3)


# ------------------------------------------------------------------
# LUT: 256 entries.
# lut[i] = round((cos(2π·i/256) + 1) · 127.5)   → 0..255
# lut[0]=255 (max), lut[64]=128 (zero crossing), lut[128]=0 (min)
# ------------------------------------------------------------------
def _make_cos_lut():
    lut = bytearray(256)
    for i in range(256):
        lut[i] = int((math.cos(2.0 * math.pi * i / 256) + 1.0) * 127.5)
    return lut


# ------------------------------------------------------------------
# Palette: interference value 0..255 → RGB
# One full HSV hue rotation, S=1 V=1.
# Destructive interference (v≈0) → red, constructive (v≈255) → back to red.
# The continuous cycle means every band gets a distinct hue.
# ------------------------------------------------------------------
def _make_palette():
    pr = bytearray(256)
    pg = bytearray(256)
    pb = bytearray(256)
    for i in range(256):
        h  = i                        # hue 0..255 = 0..360°
        hi = (h * 6) >> 8             # sector 0..5
        f  = (h * 6) & 0xFF           # fractional 0..255
        q  = 255 - f
        if   hi == 0: r, g, b = 255, f,   0
        elif hi == 1: r, g, b = q,   255, 0
        elif hi == 2: r, g, b = 0,   255, f
        elif hi == 3: r, g, b = 0,   q,   255
        elif hi == 4: r, g, b = f,   0,   255
        else:         r, g, b = 255, 0,   q
        pr[i] = r; pg[i] = g; pb[i] = b
    return pr, pg, pb


# ------------------------------------------------------------------
# Module state
# ------------------------------------------------------------------
_cos_lut = None
_pal_r   = None
_pal_g   = None
_pal_b   = None
_buf     = None      # bytearray(NPIX) — interference value per pixel 0..255

# Source positions in FP8 (×256). Python list so they can hold
# negative velocities and positions > 127 without bytearray sign issues.
_sx  = [0] * _NSRC
_sy  = [0] * _NSRC
_svx = [0] * _NSRC   # velocity FP8; can be negative
_svy = [0] * _NSRC

# Per-source static phase offset — baked at init, not updated.
# Spreads initial phase so sources aren't in lockstep at t=0.
_sph = bytearray(_NSRC)

# Pixel-space positions for viper (updated each frame from FP8 state).
_vx = bytearray(_NSRC)
_vy = bytearray(_NSRC)

_t     = 0
_frame = 0


def init():
    global _cos_lut, _pal_r, _pal_g, _pal_b, _buf
    global _t, _frame

    _cos_lut         = _make_cos_lut()
    _pal_r, _pal_g, _pal_b = _make_palette()
    _buf             = bytearray(_NPIX)
    _t               = 0
    _frame           = 0

    for i in range(_NSRC):
        # Start sources scattered across the inner grid (4..27) so they
        # don't immediately pile up on walls.
        _sx[i]  = random.randint(4, 27) << 8
        _sy[i]  = random.randint(4, 27) << 8
        speed   = random.randint(30, 75)    # FP8; ÷256 ≈ 0.12–0.29 px/frame
        angle   = random.uniform(0.0, 2.0 * math.pi)
        _svx[i] = int(math.cos(angle) * speed)
        _svy[i] = int(math.sin(angle) * speed)
        # Space phases evenly with small random jitter
        _sph[i] = (i * 42 + random.randint(0, 20)) & 255
        _vx[i]  = _sx[i] >> 8
        _vy[i]  = _sy[i] >> 8

    print("[interference] init OK — {} sources, freq={}, t_step={}".format(
        _NSRC, _FREQ, _T_STEP))


# ------------------------------------------------------------------
# Inner render loop — viper for speed.
# Writes interference value 0..255 into buf for each pixel.
#
# Distance approximation: alpha-max-beta-min
#   dist ≈ max(|dx|,|dy|) + min(|dx|,|dy|)//2
#   error ≤ 12%, imperceptible in a cosine pattern.
#
# Cosine accumulator: sum of 6 lut values (0..255 each) → 0..1530.
# Divide by 6 → 0..255 for palette lookup.
#
# Source loop is unrolled (i=0..5) — viper range() works but unrolling
# avoids the loop overhead and keeps each source's ptr8 access explicit.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _render(buf: ptr8, lut: ptr8, vx: ptr8, vy: ptr8, ph: ptr8, t: int):
    W    = int(_W)
    H    = int(_H)
    FREQ = int(16)

    for y in range(H):
        for x in range(W):
            acc = int(0)

            # Source 0
            dx = int(x) - int(vx[0]); dy = int(y) - int(vy[0])
            adx = dx if dx >= 0 else -dx; ady = dy if dy >= 0 else -dy
            dist = (adx if adx > ady else ady) + ((ady if adx > ady else adx) >> 1)
            acc += int(lut[(dist * FREQ + int(ph[0]) + t) & 255])

            # Source 1
            dx = int(x) - int(vx[1]); dy = int(y) - int(vy[1])
            adx = dx if dx >= 0 else -dx; ady = dy if dy >= 0 else -dy
            dist = (adx if adx > ady else ady) + ((ady if adx > ady else adx) >> 1)
            acc += int(lut[(dist * FREQ + int(ph[1]) + t) & 255])

            # Source 2
            dx = int(x) - int(vx[2]); dy = int(y) - int(vy[2])
            adx = dx if dx >= 0 else -dx; ady = dy if dy >= 0 else -dy
            dist = (adx if adx > ady else ady) + ((ady if adx > ady else adx) >> 1)
            acc += int(lut[(dist * FREQ + int(ph[2]) + t) & 255])

            # Source 3
            dx = int(x) - int(vx[3]); dy = int(y) - int(vy[3])
            adx = dx if dx >= 0 else -dx; ady = dy if dy >= 0 else -dy
            dist = (adx if adx > ady else ady) + ((ady if adx > ady else adx) >> 1)
            acc += int(lut[(dist * FREQ + int(ph[3]) + t) & 255])

            # Source 4
            dx = int(x) - int(vx[4]); dy = int(y) - int(vy[4])
            adx = dx if dx >= 0 else -dx; ady = dy if dy >= 0 else -dy
            dist = (adx if adx > ady else ady) + ((ady if adx > ady else adx) >> 1)
            acc += int(lut[(dist * FREQ + int(ph[4]) + t) & 255])

            # Source 5
            dx = int(x) - int(vx[5]); dy = int(y) - int(vy[5])
            adx = dx if dx >= 0 else -dx; ady = dy if dy >= 0 else -dy
            dist = (adx if adx > ady else ady) + ((ady if adx > ady else adx) >> 1)
            acc += int(lut[(dist * FREQ + int(ph[5]) + t) & 255])

            buf[y * W + x] = acc // 6


# ------------------------------------------------------------------
# draw() — called every frame by main.py
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def draw():
    global _t, _frame

    # -- Update source positions (FP8, wall bounce) --
    # Separate X and Y limits. This was a single `31 << 8` for both axes —
    # identical at 32x32, but it would have bounced sources off the wrong wall
    # on a non-square panel.
    limx = int(_W - 1) << 8
    limy = int(_H - 1) << 8
    for i in range(_NSRC):
        _sx[i] += _svx[i]
        _sy[i] += _svy[i]
        if _sx[i] < 0:
            _sx[i]  = 0;    _svx[i] = -_svx[i]
        elif _sx[i] > limx:
            _sx[i]  = limx; _svx[i] = -_svx[i]
        if _sy[i] < 0:
            _sy[i]  = 0;    _svy[i] = -_svy[i]
        elif _sy[i] > limy:
            _sy[i]  = limy; _svy[i] = -_svy[i]
        _vx[i] = _sx[i] >> 8
        _vy[i] = _sy[i] >> 8

    # -- Advance time --
    _t = (_t + _T_STEP) & 255

    # -- Compute interference into _buf --
    _render(_buf, _cos_lut, _vx, _vy, _sph, _t)

    # -- Paint pixels --
    gfx = graphics
    pr  = _pal_r
    pg  = _pal_g
    pb  = _pal_b
    buf = _buf
    w = _W; h = _H
    for y in range(h):
        row = y * w
        for x in range(w):
            v = buf[row + x]
            gfx.set_pen(gfx.create_pen(pr[v], pg[v], pb[v]))
            gfx.pixel(x, y)

    _frame += 1


def deinit():
    global _cos_lut, _pal_r, _pal_g, _pal_b, _buf
    _cos_lut = None
    _pal_r   = None
    _pal_g   = None
    _pal_b   = None
    _buf     = None