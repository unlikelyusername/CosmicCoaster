# /effects/plasma.py
#
# Classic demoscene plasma — overlapping sine waves producing a
# shimmering, morphing color field. Pure integer LUT math.
#
# Four sine waves with different spatial frequencies and speeds,
# summed per pixel and mapped through a cycling HSV-derived palette.
# No state per pixel — entirely computed from (x, y, t).
#
# Resolution-independent: the LUTs are 256-entry and indexed by phase, so
# only the per-column/per-row scratch arrays depend on the display size.
#
# Effect contract:
#   GEOMETRY = "any"
#   graphics = None
#   cu       = None
#   W, H     = injected by the loader
#   init(); draw(); deinit()

import math
import time
from micropython import const

GEOMETRY = "any"

graphics = None
cu = None
W = 0    # injected by the loader
H = 0

# LUT sizes
_TRIG_SIZE = const(256)
_PAL_SIZE  = const(256)

# Wave spatial frequency steps per pixel (LUT units)
# Four waves: two X-dominant, two diagonal
_WA_X = const(7)    # wave A: horizontal
_WA_Y = const(0)
_WB_X = const(0)    # wave B: vertical
_WB_Y = const(9)
_WC_X = const(5)    # wave C: diagonal
_WC_Y = const(5)
_WD_X = const(11)   # wave D: steep diagonal
_WD_Y = const(3)

# Time advance per frame (LUT units) per wave — controls speed
_TA = const(3)
_TB = const(4)
_TC = const(2)
_TD = const(5)

# ====================================================================
# Palette — full HSV cycle with extra saturation punch
# 256 entries cycling through vivid hues
# ====================================================================
def _make_palette():
    pal = []
    for i in range(_PAL_SIZE):
        h = i / _PAL_SIZE   # 0..1
        # HSV: S=1, V=1, full saturation
        h6 = h * 6.0
        hi = int(h6)
        f  = h6 - hi
        q  = int(255 * (1.0 - f))
        t  = int(255 * f)
        if   hi == 0: r,g,b = 255, t,   0
        elif hi == 1: r,g,b = q,   255, 0
        elif hi == 2: r,g,b = 0,   255, t
        elif hi == 3: r,g,b = 0,   q,   255
        elif hi == 4: r,g,b = t,   0,   255
        else:         r,g,b = 255, 0,   q
        # Boost contrast slightly
        r = min(255, int(r * 1.1))
        g = min(255, int(g * 1.1))
        b = min(255, int(b * 1.1))
        pal.append((r, g, b))
    return pal

# ====================================================================
# Module state
# ====================================================================
_sin_lut = None
_palette = None

# Per-row/col precomputed components (refilled each frame)
_xa = None   # wave A x-component
_xb = None   # wave B x-component
_xc = None   # wave C x-component
_xd = None   # wave D x-component
_ya = None   # wave A y-component
_yb = None   # wave B y-component
_yc = None   # wave C y-component
_yd = None   # wave D y-component

# Time phases (LUT index, wraps mod 256)
_tA = 0; _tB = 0; _tC = 0; _tD = 0


def init():
    global _sin_lut, _palette
    global _xa, _xb, _xc, _xd, _ya, _yb, _yc, _yd
    global _tA, _tB, _tC, _tD

    # Sin LUT: 256 entries, values 0..255 (unsigned, shifted)
    _sin_lut = [int(127.5 + 127.5 * math.sin(2 * math.pi * i / 256))
                for i in range(256)]

    _palette = _make_palette()

    _xa = [0] * W; _xb = [0] * W
    _xc = [0] * W; _xd = [0] * W
    _ya = [0] * H; _yb = [0] * H
    _yc = [0] * H; _yd = [0] * H

    _tA = 0; _tB = 64; _tC = 128; _tD = 192

    print("[plasma] init OK — {}x{}".format(W, H))


@micropython.native  # noqa: F821
def draw():
    global _tA, _tB, _tC, _tD

    lut = _sin_lut
    pal = _palette
    xa = _xa; xb = _xb; xc = _xc; xd = _xd
    ya = _ya; yb = _yb; yc = _yc; yd = _yd
    gfx = graphics
    w = W; h = H

    tA = _tA; tB = _tB; tC = _tC; tD = _tD

    # ---- Precompute x-components for each wave ----
    for x in range(w):
        xa[x] = lut[(x * _WA_X + tA) & 255]
        xb[x] = lut[(x * _WB_X + tB) & 255]
        xc[x] = lut[(x * _WC_X + tC) & 255]
        xd[x] = lut[(x * _WD_X + tD) & 255]

    # ---- Precompute y-components for each wave ----
    for y in range(h):
        ya[y] = lut[(y * _WA_Y + tA) & 255]
        yb[y] = lut[(y * _WB_Y + tB) & 255]
        yc[y] = lut[(y * _WC_Y + tC) & 255]
        yd[y] = lut[(y * _WD_Y + tD) & 255]

    # ---- Pixel loop: sum four waves, map to palette ----
    for y in range(h):
        vya = ya[y]; vyb = yb[y]; vyc = yc[y]; vyd = yd[y]
        for x in range(w):
            # Sum of four waves: 0..1020, scale to 0..255
            v = (xa[x] + xb[x] + xc[x] + xd[x] +
                 vya    + vyb   + vyc   + vyd) >> 3
            r, g, b = pal[v & 255]
            gfx.set_pen(gfx.create_pen(r, g, b))
            gfx.pixel(x, y)

    # ---- Advance time phases ----
    _tA = (tA + _TA) & 255
    _tB = (tB + _TB) & 255
    _tC = (tC + _TC) & 255
    _tD = (tD + _TD) & 255


def deinit():
    global _sin_lut, _palette, _xa, _xb, _xc, _xd, _ya, _yb, _yc, _yd
    _sin_lut = None
    _palette = None
    _xa = None; _xb = None; _xc = None; _xd = None
    _ya = None; _yb = None; _yc = None; _yd = None
