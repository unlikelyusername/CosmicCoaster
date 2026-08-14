# /effects/waving_cell.py
#
# Port of WLED "Waving Cell" 2D effect.
# Original by Stepko: https://editor.soulmatelights.com/gallery/1704-wavingcells
# Adapted for WLED by @blazoncek. Ported to MicroPython / Cosmic Unicorn.
#
# Algorithm (per pixel):
#   inner = sin8(y + t)
#   wave  = sin8(x * aX + inner) + cos8(y * aZ)
#   color = palette[wave + t]
#
#   The nested sin creates a cellular interference pattern. aX controls how
#   many cells appear horizontally; aZ adds a vertical brightness gradient.
#   t advances each frame, making the cells shift and breathe over time.
#   With default aX=9 there are ~4–5 visible cell columns across the display.
#
# Original defaults: aX=9, aY=1, aZ=1, no blur, no flow (color offset = t>>8).
#
# Porting notes:
#   sin8(x) = cos8((x+192)&255) — single cos8 LUT covers both.
#   t_byte = (t_ms >> 4) & 255 — advances ~2 steps/frame at 32fps.
#   Combined buffer (3072 bytes) allows viper with only 3 arguments.
#
# Effect contract: graphics=None, cu=None, init(), draw(), deinit()

import math
import time
from micropython import const

graphics = None
cu       = None

GEOMETRY = "any"

W = 0    # injected by the loader
H = 0

_W    = 32
_H    = 32
_N    = 1024          # W*H — channel stride within _buf
_NBUF = 3072          # 3 * _N
# Horizontal cell frequency. 32px at step 12 shows ~1.5 cells; keep that count
# on any width rather than letting cells shrink or stretch with the panel.
_XSTEP = 12

_buf     = None
_cos8lut = bytearray(256)
_state   = bytearray(1)   # [0] = t_byte


def _build_lut():
    for i in range(256):
        _cos8lut[i] = int((math.cos(i * math.pi / 128.0) + 1.0) * 127.5)


# ------------------------------------------------------------------
# Waving Cell render (viper)
#
# state[0] = t_byte: time counter 0–255, advances ~2 steps/frame.
#
# With aX=9 (9 steps per pixel column) and sin period=256:
#   visible cell width ≈ 256/9 ≈ 28px, giving ~1 cell per column pair.
#   Combined with the y-varying inner term, cells wave and ripple.
#
# cos8(y): for y=0..31 covers the first ~12% of the cosine wave,
#   adding a gentle top-bright / bottom-dim vertical gradient.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _render_wc(buf: ptr8, lut: ptr8, state: ptr8):
    t  = int(state[0])
    w  = int(_W); h = int(_H)
    n  = int(_N); n2 = n << 1
    xs = int(_XSTEP)
    # Loop order is y-outer so the two y-only terms are computed once per row
    # rather than once per pixel. (The original had x outer and recomputed
    # `inner` for every pixel.)
    for y in range(h):
        # sin8(y + t): inner modulation — cells shift vertically over time
        inner = int(lut[(y + t + 192) & 255])
        # cos8(y): vertical brightness gradient. y=0..31 covered ~12% of the
        # cosine; rescale so the same span is covered whatever the height.
        vgrad = int(lut[((y * 32) // h) & 255])
        row   = y * w
        for x in range(w):
            # sin8(x*step + inner): horizontal cell structure
            wave  = int(lut[(x * xs + inner + 192) & 255]) + vgrad
            # color index with slow cycling
            ci = (wave + t) & 255
            idx = row + x
            buf[idx]      = int(lut[ci])
            buf[idx + n]  = int(lut[(ci + 85)  & 255])
            buf[idx + n2] = int(lut[(ci + 170) & 255])


@micropython.native  # noqa: F821
def _push_display():
    gfx = graphics
    buf = _buf
    w = _W; h = _H; n = _N; n2 = n * 2
    for y in range(h):
        row = y * w
        for x in range(w):
            idx = row + x
            gfx.set_pen(gfx.create_pen(
                int(buf[idx]),
                int(buf[idx + n]),
                int(buf[idx + n2])
            ))
            gfx.pixel(x, y)


def init():
    global _buf, _W, _H, _N, _NBUF, _XSTEP
    _W = W if W else 32
    _H = H if H else 32
    _N = _W * _H
    _NBUF = _N * 3
    _XSTEP = max(1, 384 // _W)     # 384/32 = 12, the original value
    _buf = bytearray(_NBUF)
    _build_lut()
    print("[waving_cell] init")


def draw():
    _state[0] = (time.ticks_ms() >> 4) & 255
    _render_wc(_buf, _cos8lut, _state)
    _push_display()


def deinit():
    global _buf
    _buf = None
