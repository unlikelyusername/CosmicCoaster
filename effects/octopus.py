# /effects/octopus.py
#
# Port of WLED "2D Octopus" effect.
# Original by Stepko and Sutaburosu: https://editor.soulmatelights.com/gallery/671-octopus
# Adapted for WLED by @blazoncek. Ported to MicroPython / Cosmic Unicorn.
#
# Algorithm:
#   Init: for each pixel compute polar coords relative to display centre:
#     angle  = int(40.7436 * atan2(dy, dx)) & 255  → 0-255 full circle
#     radius = sqrt(dx²+dy²) * mapp                 → 0-127, mapp=180//32=5
#   Stored as _maps[0:1024]=angle, _maps[1024:2048]=radius.
#
#   Per pixel per frame (t is a 16-bit counter advancing each frame):
#     inner = sin8((angle*4 - radius)/4 + t/2)
#     raw   = sin8(inner + radius - t + angle * legs_factor)
#     intensity = (raw² >> 8)        ← squaring sharpens the arm edges
#     hue   = t/2 - radius           ← palette rotates outward over time
#     pixel = rainbow(hue) * intensity/256
#
#   Why 16-bit t: with 8-bit t, hue = t>>1 only covers half the palette per
#   cycle, causing a jarring colour jump every ~2s when t wraps. With 16-bit t,
#   th = t>>1 takes 512 steps to wrap (seamless — all trig is mod 256 anyway).
#
# Porting notes:
#   sin8(x) = cos8((x+192)&255) — single cos8 LUT covers both.
#   mapp = 180 // 32 = 5.
#   state[3]: [0]=t_lo, [1]=t_hi, [2]=legs_factor (1=default 1 arm shape).
#   Combined buffer (3072 bytes) + maps (2048 bytes) = 4 viper args total.
#
# Resolution-independent. The polar map is rebuilt for the actual display in
# init(), and _MAPP is derived so radius still spans 0..127 whatever the size.
# On a non-square panel the arms are simply cropped by the shorter axis.
#
# Dimensions are read inside the viper function as module globals rather than
# passed as arguments: viper takes at most 4 args, and the buffers already use
# all four. Reading a global there costs nothing measurable — it is hoisted to
# a local once per call, not per pixel.
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
cu       = None
W = 0    # injected by the loader
H = 0

_W    = 32
_H    = 32
_N    = 1024     # W*H — channel stride in _buf and _maps
_NBUF  = 3072
_NMAPS = 2048    # [0:_N]=angle, [_N:2*_N]=radius
_MAPP  = 5       # scaled in init so radius spans 0..127
_LEGS  = const(5)     # arm multiplier: WLED default custom3=128 → 128//4+1=33

_buf     = None
_maps    = None
_cos8lut = bytearray(256)
_state   = bytearray(3)   # [0]=t_lo, [1]=t_hi, [2]=legs_factor


def _build_lut():
    for i in range(256):
        _cos8lut[i] = int((math.cos(i * math.pi / 128.0) + 1.0) * 127.5)


def _build_maps():
    # Polar coordinate map relative to the display centre.
    # angle: 40.7436 * atan2(dy, dx) ≈ 128/π * atan2 → maps -π..π to -128..128,
    #        wraps to 0-255.
    # radius: Euclidean distance * _MAPP, kept within 0-127.
    #
    # Built once at init in plain Python, so re-centring and rescaling for a
    # different panel costs nothing at frame time.
    w = _W; h = _H; n = _N
    cx = w / 2.0;  cy = h / 2.0
    m  = _maps
    for y in range(h):
        dy = y - cy
        for x in range(w):
            dx = x - cx
            idx = y * w + x
            m[idx]     = int(40.7436 * math.atan2(dy, dx)) & 255
            m[idx + n] = int(math.sqrt(dx * dx + dy * dy) * _MAPP) & 255


# ------------------------------------------------------------------
# Octopus render (viper).
#
# state[0:2] = 16-bit t (lo, hi); state[2] = legs_factor.
# t is 16-bit so th = t>>1 covers 0-255 before wrapping (512-step hue
# period), eliminating the visible colour jump of an 8-bit counter.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _render_oct(buf: ptr8, lut: ptr8, maps: ptr8, state: ptr8):
    t  = int(state[0]) | (int(state[1]) << 8)
    th = t >> 1
    lf = int(state[2])
    w  = int(_W); h = int(_H); n = int(_N)
    n2 = n << 1
    for y in range(h):
        for x in range(w):
            idx    = y * w + x
            ang    = int(maps[idx])
            rad    = int(maps[idx + n])
            inner  = int(lut[((ang * 4 - rad) // 4 + th + 192) & 255])
            raw    = int(lut[(inner + rad - t + ang * lf + 192) & 255])
            intensity = (raw * raw) >> 8
            ci     = (th - rad) & 255
            buf[idx]      = (int(lut[ci])               * intensity) >> 8
            buf[idx + n]  = (int(lut[(ci + 85)  & 255]) * intensity) >> 8
            buf[idx + n2] = (int(lut[(ci + 170) & 255]) * intensity) >> 8


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
    global _buf, _maps, _W, _H, _N, _NBUF, _NMAPS, _MAPP

    _W = W if W else 32
    _H = H if H else 32
    _N = _W * _H
    _NBUF  = _N * 3
    _NMAPS = _N * 2

    # Scale so the furthest pixel from centre lands near 127, matching the
    # original's 180//32 = 5 on a 32x32 (max radius 22.6 -> 113).
    max_r = math.sqrt((_W / 2.0) ** 2 + (_H / 2.0) ** 2)
    _MAPP = max(1, int(127.0 / max_r)) if max_r > 0 else 1

    _buf  = bytearray(_NBUF)
    _maps = bytearray(_NMAPS)
    _build_lut()
    _build_maps()
    _state[0] = 0;  _state[1] = 0;  _state[2] = _LEGS
    print("[octopus] init — {}x{} mapp={}".format(_W, _H, _MAPP))


def draw():
    # Advance 16-bit counter by 5/frame — matches WLED default (speed/32+1 = 128/32+1 = 5)
    t = ((int(_state[0]) | (int(_state[1]) << 8)) + 5) & 0xFFFF
    _state[0] = t & 255
    _state[1] = (t >> 8) & 255
    _render_oct(_buf, _cos8lut, _maps, _state)
    _push_display()


def deinit():
    global _buf, _maps
    _buf  = None
    _maps = None
