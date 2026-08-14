# /effects/attractor.py
#
# Strange attractor — iterated trajectory plotted into a pixel buffer.
# Accumulates hits over many frames. Buffer decays slowly so old paths
# fade out as the attractor evolves (parameter drift).
#
# Supports two attractor types, auto-cycling between them:
#   Clifford: x' = sin(a*y) + c*cos(a*x)
#             y' = sin(b*x) + d*cos(b*y)
#
#   de Jong:  x' = sin(a*y) - cos(b*x)
#             y' = sin(c*x) - cos(d*y)
#
# Parameters drift slowly over time, morphing the attractor shape.
# Colorized by hit density: dark purple → electric blue → white core.
#
# Resolution-independent: the attractor is scaled to fit the short axis.
#
# Effect contract:
#   GEOMETRY = "any"
#   graphics = None
#   cu       = None
#   W, H     = injected by the loader
#   init(); draw(); deinit()

import math
import time
import random
from micropython import const

GEOMETRY = "any"

graphics = None
cu = None
W = 0    # injected by the loader
H = 0

_W = 32
_H = 32
_N = 1024

# Iterations per frame — tuned for ~20fps on RP2040 at 200MHz
_ITERS_PER_FRAME = const(400)

# Buffer decay: each frame, hits >>= 1 (half-life ~1 frame if no new hits)
# Set slower for longer trails
_DECAY_SHIFT = const(1)   # decay every N frames
_DECAY_EVERY = const(4)

# Max accumulator value before clamping
_MAX_ACC = const(255)

# Parameter drift speed, in parameter-units per frame (L1 across all four).
#
# This used to be an exponential approach — `_a += (_ta - _a) * 0.003` — which
# moved fast for a moment then crawled asymptotically, so the attractor spent
# almost all of its time nearly-converged and looked frozen. It only retargeted
# once the remaining distance fell under 0.05, which an exponential reaches
# very slowly. Constant-rate travel keeps it visibly moving the whole time.
_DRIFT_STEP = 0.06

# ====================================================================
# Color palette: density 0..255 → RGB
# 0=black, low=deep purple, mid=electric blue, high=cyan/white
# ====================================================================
def _make_palette():
    pal = [(0, 0, 0)]   # index 0 = black
    for i in range(1, 256):
        t = i / 255.0
        if t < 0.25:
            t2 = t / 0.25
            r = int(20  * t2)
            g = int(0)
            b = int(40  * t2)
        elif t < 0.5:
            t2 = (t - 0.25) / 0.25
            r = int(20  + 30  * t2)
            g = int(0   + 20  * t2)
            b = int(40  + 160 * t2)
        elif t < 0.75:
            t2 = (t - 0.5) / 0.25
            r = int(50  + 80  * t2)
            g = int(20  + 160 * t2)
            b = int(200 + 40  * t2)
        else:
            t2 = (t - 0.75) / 0.25
            r = int(130 + 125 * t2)
            g = int(180 + 75  * t2)
            b = int(240 + 15  * t2)
        pal.append((r, g, b))
    return pal

# ====================================================================
# Module state
# ====================================================================
_buf   = None    # bytearray(N) — hit accumulator 0..255
_pal   = None
_frame = 0

# Attractor state
_ax = 0.0; _ay = 0.0   # current trajectory point
_a = 0.0; _b = 0.0; _c = 0.0; _d = 0.0   # parameters
_ta = 0.0; _tb = 0.0; _tc = 0.0; _td = 0.0  # target params
_mode = 0   # 0=Clifford, 1=de Jong

# View scale: attractor coords → pixel coords. Clifford and de Jong both live
# in roughly a +/-2.5 range, so the whole attractor fits when the short axis
# covers 5 units. Set from the display size in init().
_SCALE = 6.0
_CX    = 16.0
_CY    = 16.0


def _random_params():
    """Pick new target parameters. Avoid near-zero for interesting shapes."""
    def rp():
        v = random.uniform(1.2, 2.8)
        return v if random.random() > 0.5 else -v
    return rp(), rp(), rp(), rp()


def init():
    global _buf, _pal, _frame
    global _ax, _ay, _a, _b, _c, _d, _ta, _tb, _tc, _td, _mode
    global _W, _H, _N, _SCALE, _CX, _CY

    _W = W if W else 32
    _H = H if H else 32
    _N = _W * _H

    # Fit the +/-2.5 attractor range into the short axis, and centre it.
    _SCALE = min(_W, _H) / 5.0
    _CX    = _W / 2.0
    _CY    = _H / 2.0

    _buf   = bytearray(_N)
    _pal   = _make_palette()
    _frame = 0
    _mode  = 0

    _a, _b, _c, _d = _random_params()
    _ta, _tb, _tc, _td = _random_params()
    _ax, _ay = 0.1, 0.1

    print("[attractor] init OK — mode: Clifford  a={:.2f} b={:.2f} c={:.2f} d={:.2f}".format(
        _a, _b, _c, _d))


@micropython.native  # noqa: F821
def draw():
    global _frame, _ax, _ay, _a, _b, _c, _d, _ta, _tb, _tc, _td, _mode

    buf  = _buf

    # ---- Parameter drift: constant speed along a straight line in parameter
    # space, so all four arrive together and the shape morphs at a steady rate.
    da = _ta - _a; db = _tb - _b
    dc = _tc - _c; dd = _td - _d
    dist = abs(da) + abs(db) + abs(dc) + abs(dd)

    if dist <= _DRIFT_STEP:
        # Arrived — snap exactly, then pick a new target (and sometimes a new
        # attractor family). No asymptotic tail to sit in.
        _a = _ta; _b = _tb; _c = _tc; _d = _td
        _ta, _tb, _tc, _td = _random_params()
        if _frame % 3 == 0:
            _mode = 1 - _mode
            print("[attractor] switching to", "de Jong" if _mode else "Clifford")
    else:
        k = _DRIFT_STEP / dist
        _a += da * k
        _b += db * k
        _c += dc * k
        _d += dd * k

    a = _a; b = _b; c = _c; d = _d
    ax = _ax; ay = _ay
    mode = _mode

    # Bind to locals — the iteration loop below runs _ITERS_PER_FRAME times and
    # would otherwise pay a global lookup per axis per iteration.
    w = _W; h = _H
    sc = _SCALE; cx = _CX; cy = _CY

    # ---- Iterate attractor ----
    for _ in range(_ITERS_PER_FRAME):
        if mode == 0:  # Clifford
            nx = math.sin(a * ay) + c * math.cos(a * ax)
            ny = math.sin(b * ax) + d * math.cos(b * ay)
        else:           # de Jong
            nx = math.sin(a * ay) - math.cos(b * ax)
            ny = math.sin(c * ax) - math.cos(d * ay)

        ax = nx; ay = ny

        # Map to pixel coords
        px = int(ax * sc + cx)
        py = int(ay * sc + cy)

        if 0 <= px < w and 0 <= py < h:
            idx = py * w + px
            v = buf[idx] + 12
            buf[idx] = v if v < _MAX_ACC else _MAX_ACC

    _ax = ax; _ay = ay

    # ---- Decay buffer every N frames ----
    if _frame % _DECAY_EVERY == 0:
        for i in range(w * h):
            v = buf[i]
            if v > 0:
                buf[i] = v - 1 if v < 4 else v >> _DECAY_SHIFT

    # ---- Render ----
    gfx = graphics
    pal = _pal
    for y in range(h):
        row = y * w
        for x in range(w):
            r, g, b = pal[buf[row + x]]
            gfx.set_pen(gfx.create_pen(r, g, b))
            gfx.pixel(x, y)

    _frame += 1


def deinit():
    global _buf, _pal
    _buf = None
    _pal = None