# /effects/distortion_waves.py
#
# Port of WLED "Distortion Waves" 2D effect.
# Original by ldirko: https://editor.soulmatelights.com/gallery/1089-distorsion-waves
# Adapted for WLED by @blazoncek. Ported to MicroPython / Cosmic Unicorn.
#
# Algorithm (per pixel, per frame):
#   Three colour channels (R, G, B) are each produced by two combined terms:
#
#   1. DISTORTION — nested cos8 applied to x, y, and time at different phases.
#      cos8(cos8(x*8 + t) + cos8(y*8 - t/2) + t/3) >> 1
#      This creates a slowly morphing interference pattern across the grid.
#
#   2. RADIAL RINGS — each channel has its own "centre point" that sweeps a
#      lissajous-like orbit (via beatsin at coprime frequencies). The squared
#      distance from that centre is subtracted from the time counter, then
#      scaled and passed through cos8 again. This creates concentric rings
#      that expand/contract and chase the moving centre.
#
#   The two terms are added (byte-wrapping) and passed through a final cos8.
#   Three independent channels with offset phase produce the RGB colour field.
#
# Key porting notes:
#   cos8(x): FastLED's 8-bit cosine — 0–255 range, full period = 256 steps.
#     Implemented as a 256-entry LUT built at init with math.cos.
#   beatsin(bpm, lo, hi): sine oscillator at <bpm> beats/minute.
#     Implemented with time % period to avoid accumulator drift.
#   All arithmetic matches C unsigned-byte wrap-around via & 255 masking.
#
# Render buffer layout:
#   _buf is bytearray(3072): [0:1024]=R, [1024:2048]=G, [2048:3072]=B
#   Packing the three channels into one buffer lets _render_dw use only
#   3 viper arguments (buf, lut, state) — viper's hard limit is 4.
#   Scalars (a, a2, a3, cx, cy, cx1, cy1, cx2, cy2) are packed into a
#   9-byte state bytearray; all values 0–255, fit cleanly in bytes.
#
# Effect contract: graphics=None, cu=None, init(), draw(), deinit()

import math
import time
from micropython import const

_TWO_PI = 6.2831853

graphics = None
cu       = None

GEOMETRY = "any"

W = 0    # injected by the loader
H = 0

_W    = 32
_H    = 32
_NPIX = 1024
_NBUF = 3072       # 3 x _NPIX — combined R/G/B buffer
# Spatial frequency per axis: 32px at (x << 3) spans exactly one 256-step wave.
# Derived per axis so the same number of waves fits any panel.
_XF = 8
_YF = 8
# Ring-coordinate scale per axis, mapping the panel onto the 0.._CS field the
# orbiting centres move in.
_XS = 4
_YS = 4
_SCALE   = const(4)          # spatial scale (WLED default intensity/32 = 4)
_BPM_CX  = const(6)          # beatsin BPMs for 6 centre-point oscillators
_BPM_CY  = const(8)          # (from original: 10-speed … 17-speed, speed=4)
_BPM_CX1 = const(9)
_BPM_CY1 = const(11)
_BPM_CX2 = const(13)
_BPM_CY2 = const(10)
_CS      = const(128)        # colsScaled = rowsScaled = 32 * _SCALE

# ------------------------------------------------------------------
# Buffers (allocated in init)
# ------------------------------------------------------------------
_buf  = None   # bytearray(_NBUF) — packed R/G/B pixel data
_cos8lut = bytearray(256)   # cos8 LUT — module-level, filled in init

# 9-byte state vector passed to viper: a a2 a3 cx cy cx1 cy1 cx2 cy2
_state = bytearray(9)

# Float accumulator for the primary time counter.
# Avoids the frame-rate resonance of (t // 32) — a2/a3 are derived
# from this float so their staircase stepping is properly decoupled.
_a_acc  = 0.0
_t_prev = 0


# ------------------------------------------------------------------
# Build cos8 LUT (once at init)
# cos8(x) = 128 + 127*cos(x * π / 128)  →  0–255
# ------------------------------------------------------------------
def _build_lut():
    for i in range(256):
        _cos8lut[i] = int((math.cos(i * math.pi / 128.0) + 1.0) * 127.5)


# ------------------------------------------------------------------
# beatsin — sine oscillator at <bpm> beats/minute, output [lo, hi]
#
# Uses math.sin directly (hardware FPU on RP2350/Pico 2 — fast).
# Returns a smoothly-advancing integer, max ±1 step per frame at 32fps.
# The original WLED uses beatsin16 (65536-step phase); this matches
# that quality by using float rather than the 256-step LUT approach.
# ------------------------------------------------------------------
def _beatsin(bpm, lo, hi, t_ms):
    period = 60000.0 / bpm
    phase  = (t_ms % int(period)) / period * _TWO_PI
    val    = (math.sin(phase) + 1.0) * 0.5   # 0.0–1.0
    return int(lo + val * (hi - lo) + 0.5)   # round to nearest int


# ------------------------------------------------------------------
# Distortion Waves render (viper)
#
# buf   — packed bytearray(3072): [0:1024]=R [1024:2048]=G [2048:3072]=B
# lut   — cos8 lookup table bytearray(256)
# state — bytearray(9): [0]=a [1]=a2 [2]=a3 [3]=cx [4]=cy
#                       [5]=cx1 [6]=cy1 [7]=cx2 [8]=cy2
#
# Negative intermediate values (e.g. y*8 - a2 when y=0) are handled
# correctly by & 255: Python/viper two's-complement lower 8 bits match
# C unsigned byte wrap-around exactly.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _render_dw(buf: ptr8, lut: ptr8, state: ptr8):
    a   = int(state[0]);  a2  = int(state[1]);  a3  = int(state[2])
    cx  = int(state[3]);  cy  = int(state[4])
    cx1 = int(state[5]);  cy1 = int(state[6])
    cx2 = int(state[7]);  cy2 = int(state[8])

    w  = int(_W); h = int(_H)
    n  = int(_NPIX); n2 = n << 1
    xf = int(_XF); yf = int(_YF)
    xs = int(_XS); ys = int(_YS)
    for x in range(w):
        xoffs = (x + 1) * xs
        for y in range(h):
            yoffs = (y + 1) * ys

            # distortion: nested cosines, per-channel time-phase offsets
            c0 = int(lut[((x * xf) + a)  & 255])
            c1 = int(lut[((y * yf) - a2) & 255])
            rdist = int(lut[(c0 + c1 + a3) & 255]) >> 1

            c0 = int(lut[((x * xf) - a2) & 255])
            c1 = int(lut[((y * yf) + a3) & 255])
            gdist = int(lut[(c0 + c1 + a + 32) & 255]) >> 1

            c0 = int(lut[((x * xf) + a3) & 255])
            c1 = int(lut[((y * yf) - a)  & 255])
            bdist = int(lut[(c0 + c1 + a2 + 64) & 255]) >> 1

            # radial rings: squared distance from each channel's orbiting centre
            # >> 7 maps 0–32768 dist² range to 0–255 byte space
            dxr = xoffs - cx;   dyr = yoffs - cy
            dxg = xoffs - cx1;  dyg = yoffs - cy1
            dxb = xoffs - cx2;  dyb = yoffs - cy2

            vr = (rdist + ((a  - ((dxr*dxr + dyr*dyr) >> 7)) << 1)) & 255
            vg = (gdist + ((a2 - ((dxg*dxg + dyg*dyg) >> 7)) << 1)) & 255
            vb = (bdist + ((a3 - ((dxb*dxb + dyb*dyb) >> 7)) << 1)) & 255

            idx = y * w + x
            buf[idx]      = int(lut[vr])
            buf[idx + n]  = int(lut[vg])
            buf[idx + n2] = int(lut[vb])


# ------------------------------------------------------------------
# Push packed buffer to display (native)
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _push_display():
    gfx = graphics
    buf = _buf
    w = _W; h = _H; n = _NPIX; n2 = n * 2
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


# ------------------------------------------------------------------
# init / draw / deinit
# ------------------------------------------------------------------
def init():
    global _buf, _a_acc, _t_prev
    global _W, _H, _NPIX, _NBUF, _XF, _YF, _XS, _YS

    _W    = W if W else 32
    _H    = H if H else 32
    _NPIX = _W * _H
    _NBUF = _NPIX * 3

    # One full 256-step wave across each axis, as 32 px at (x << 3) gave.
    _XF = max(1, 256 // _W)
    _YF = max(1, 256 // _H)
    # Map the panel onto the 0.._CS field the ring centres orbit in.
    _XS = max(1, _CS // _W)
    _YS = max(1, _CS // _H)

    _buf   = bytearray(_NBUF)
    _a_acc = 0.0
    _t_prev = time.ticks_ms()
    _build_lut()
    print("[distortion_waves] init — {}x{} xf={} yf={} xs={} ys={}".format(
        _W, _H, _XF, _YF, _XS, _YS))


_dbg_frame   = 0
_dbg_t_start = 0

def draw():
    global _dbg_frame, _dbg_t_start, _a_acc, _t_prev

    t  = time.ticks_ms()
    dt = time.ticks_diff(t, _t_prev)
    _t_prev = t

    # Float accumulator: advances by dt/32.0 per frame regardless of
    # frame-rate resonance. a2/a3 derived from continuous float so their
    # step timing is decoupled from a's steps (vs. integer >> which
    # causes all three to synchronise and create chromatic shimmer).
    _a_acc  = (_a_acc + dt / 32.0) % 256.0
    _state[0] = int(_a_acc) & 255
    _state[1] = int(_a_acc * 0.5) & 255
    _state[2] = int(_a_acc / 3.0) & 255
    _state[3] = _beatsin(_BPM_CX,  0, _CS, t)
    _state[4] = _beatsin(_BPM_CY,  0, _CS, t)
    _state[5] = _beatsin(_BPM_CX1, 0, _CS, t)
    _state[6] = _beatsin(_BPM_CY1, 0, _CS, t)
    _state[7] = _beatsin(_BPM_CX2, 0, _CS, t)
    _state[8] = _beatsin(_BPM_CY2, 0, _CS, t)

    t0 = time.ticks_ms()
    _render_dw(_buf, _cos8lut, _state)
    t1 = time.ticks_ms()
    _push_display()
    t2 = time.ticks_ms()

    _dbg_frame += 1
    if _dbg_frame == 1:
        _dbg_t_start = t
    if _dbg_frame % 20 == 0:
        elapsed = time.ticks_diff(t, _dbg_t_start)
        fps = _dbg_frame * 1000 // elapsed if elapsed > 0 else 0
        print("[dw] render={:d}ms push={:d}ms fps={:d}".format(
            time.ticks_diff(t1, t0),
            time.ticks_diff(t2, t1),
            fps
        ))


def deinit():
    global _buf
    _buf = None
