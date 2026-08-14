# /effects/soap.py
#
# Port of WLED "2D Soap" effect.
# Ported to MicroPython / Cosmic Unicorn.
#
# Algorithm:
#   Each frame:
#   1. Update 1024-pixel noise field (viper): blend old with fresh sinusoidal noise.
#      Three byte offsets (ox, oy, ot) drift each frame for 2D+t motion.
#   2. soapRows (viper): for each row i, sample noise at first pixel → signed
#      displacement. For each output column j, blend two adjacent source pixels
#      (ease8InOutCubic sub-pixel blend). Out-of-bounds indices inject fresh
#      palette colour from the noise field — this is what keeps the display alive.
#   3. soapCols (viper): same pass over columns.
#   4. Push to display (native).
#
# Buffer layout (combined for viper 4-arg limit):
#   _pix_buf  bytearray(3072): [0:1024]=R  [1024:2048]=G  [2048:3072]=B
#   _aux      bytearray(608):  [0:256]=ease8  [256:512]=cos8  [512:544]=tmp_r
#                               [544:576]=tmp_g  [576:608]=tmp_b
#   _noise3d  bytearray(1024): noise field (separate — reused by _update_noise)
#
# _soap_rows_v / _soap_cols_v take (pix, aux, n3d) = 3 viper args.
# _update_noise takes (n3d, lut, state) = 3 viper args (lut = standalone _cos8lut).
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
_NPIX = 1024
_N2   = 2048

# Noise spatial frequency per axis — 7 and 9 steps per pixel at width 32.
# Derived per axis so the same number of noise cells spans any panel.
_NFX  = 7
_NFY  = 9
_AMPLITUDE  = const(3)      # displacement scale; delta=(noise-128)*amp>>8 = 0 or 1
_SMOOTHNESS = const(200)    # old-noise weight 0-255; higher = slower change

_N_SCALE_X  = 7.0 / 32.0
_N_SCALE_Y  = 7.0 / 32.0

# Offsets within _aux
_AUX_LC = const(256)   # cos8 LUT
# Scratch rows must hold max(W, H) entries; offsets are computed in init().
_AUX_TR = 512
_AUX_TG = 544
_AUX_TB = 576

_pix_buf  = None   # bytearray(3072)
_noise3d  = None   # bytearray(1024)
_aux      = None   # bytearray(608): [ease8:256][cos8:256][tmp_r:32][tmp_g:32][tmp_b:32]

_cos8lut  = bytearray(256)   # standalone copy for _update_noise (same data as _aux[256:512])
_nc       = bytearray(3)     # noise drift offsets [ox, oy, ot]

_dbg_frame   = 0
_dbg_t_start = 0


# ------------------------------------------------------------------
# Build LUTs into _aux and _cos8lut
# ------------------------------------------------------------------
def _build_luts():
    aux = _aux
    for i in range(256):
        v = int((math.cos(i * math.pi / 128.0) + 1.0) * 127.5)
        _cos8lut[i] = v
        aux[256 + i] = v                             # cos8 copy in _aux[256:512]
    for i in range(256):
        t = i / 255.0
        aux[i] = int(t * t * (3.0 - 2.0 * t) * 255.0 + 0.5)  # ease8 in _aux[0:256]


# ------------------------------------------------------------------
# Update noise field (viper) — two-frequency sinusoidal interference.
# cos8(x*7+oy+ot) + cos8(y*9+ox) averaged to ~128 centre.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _update_noise(n3d: ptr8, lut: ptr8, state: ptr8):
    ox     = int(state[0])
    oy     = int(state[1])
    ot     = int(state[2])
    smooth = 200
    inv_s  = 56
    w   = int(_W); h = int(_H)
    nfx = int(_NFX); nfy = int(_NFY)
    for y in range(h):
        for x in range(w):
            a   = int(lut[(x * nfx + oy + ot) & 255])
            b   = int(lut[(y * nfy + ox)      & 255])
            n   = (a + b) >> 1
            idx = y * w + x
            n3d[idx] = (int(n3d[idx]) * smooth + n * inv_s) >> 8
    state[0] = (ox + 3) & 255
    state[1] = (oy + 2) & 255
    state[2] = (ot + 1) & 255


# ------------------------------------------------------------------
# Initialise pixels from noise field (rainbow palette, once at init).
# ------------------------------------------------------------------
def _init_pixels():
    lut = _cos8lut
    n3d = _noise3d
    pb  = _pix_buf
    n = _NPIX; n2 = _N2
    for i in range(n):
        ci = (~int(n3d[i]) * 3) & 255
        pb[i]      = int(lut[ ci         ])
        pb[i + n]  = int(lut[(ci + 85)  & 255])
        pb[i + n2] = int(lut[(ci + 170) & 255])


# ------------------------------------------------------------------
# Horizontal displacement pass (viper).
#
# Mirrors WLED soapPixels(isRow=true):
#   zD = j + delta*dir,  zF = zD + dir
#   In-bounds  → blend from pixel buffer
#   Out-of-bounds → inject fresh palette color from noise (keeps effect alive)
#
# pix: combined RGB buffer [R:1024][G:1024][B:1024]
# aux: [ease8:256][cos8:256][tmp_r:32][tmp_g:32][tmp_b:32]
# n3d: noise field [1024]
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _soap_rows_v(pix: ptr8, aux: ptr8, n3d: ptr8):
    w = int(_W); h = int(_H)
    # NOT `n` — the loop below reuses `n` for the noise sample, and shadowing
    # the channel stride with a 0-255 noise byte silently reads the wrong
    # channel. Viper does not bounds-check, so it just renders garbage.
    np = int(_NPIX); n2 = int(_N2)
    tr = int(_AUX_TR); tg = int(_AUX_TG); tb = int(_AUX_TB)
    for i in range(h):
        row = i * w
        n   = int(n3d[row])
        amt = (n - 128) * 3
        if amt >= 0:
            delta = amt >> 8;  frac = amt & 255;  fwd = 1
        else:
            amt = -amt;  delta = amt >> 8;  frac = amt & 255;  fwd = -1
        eF  = int(aux[frac])
        eA  = int(aux[255 - frac])
        eF1 = eF + 1
        eA1 = eA + 1
        for j in range(w):
            zD = j + delta * fwd
            zF = zD + fwd
            # `& 31` was a width-32 modulo; use the real width.
            if zD < 0:
                yD = (-zD) % w
            else:
                yD = zD % w
            if zF < 0:
                yF = (-zF) % w
            else:
                yF = zF % w
            idD = row + yD
            idF = row + yF
            if 0 <= zD < w:
                rA = int(pix[idD]);  gA = int(pix[idD + np]);  bA = int(pix[idD + n2])
            else:
                ci = (~int(n3d[idD]) * 3) & 255
                rA = int(aux[256 + ci])
                gA = int(aux[256 + ((ci + 85)  & 255)])
                bA = int(aux[256 + ((ci + 170) & 255)])
            if 0 <= zF < w:
                rB = int(pix[idF]);  gB = int(pix[idF + np]);  bB = int(pix[idF + n2])
            else:
                ci = (~int(n3d[idF]) * 3) & 255
                rB = int(aux[256 + ci])
                gB = int(aux[256 + ((ci + 85)  & 255)])
                bB = int(aux[256 + ((ci + 170) & 255)])
            aux[tr + j] = ((rA * eA1) >> 8) + ((rB * eF1) >> 8)
            aux[tg + j] = ((gA * eA1) >> 8) + ((gB * eF1) >> 8)
            aux[tb + j] = ((bA * eA1) >> 8) + ((bB * eF1) >> 8)
        for j in range(w):
            pix[row + j]      = int(aux[tr + j])
            pix[row + j + np]  = int(aux[tg + j])
            pix[row + j + n2] = int(aux[tb + j])


# ------------------------------------------------------------------
# Vertical displacement pass (viper). Mirrors soapPixels(isRow=false).
# n3d[i] = noise at (col=i, row=0).
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _soap_cols_v(pix: ptr8, aux: ptr8, n3d: ptr8):
    w = int(_W); h = int(_H)
    np = int(_NPIX); n2 = int(_N2)
    tr = int(_AUX_TR); tg = int(_AUX_TG); tb = int(_AUX_TB)
    for i in range(w):
        n   = int(n3d[i])
        amt = (n - 128) * 3
        if amt >= 0:
            delta = amt >> 8;  frac = amt & 255;  fwd = 1
        else:
            amt = -amt;  delta = amt >> 8;  frac = amt & 255;  fwd = -1
        eF  = int(aux[frac])
        eA  = int(aux[255 - frac])
        eF1 = eF + 1
        eA1 = eA + 1
        for j in range(h):
            zD = j + delta * fwd
            zF = zD + fwd
            # This pass walks COLUMNS, so the wrap is by height, not width.
            if zD < 0:
                yD = (-zD) % h
            else:
                yD = zD % h
            if zF < 0:
                yF = (-zF) % h
            else:
                yF = zF % h
            idD = yD * w + i
            idF = yF * w + i
            if 0 <= zD < h:
                rA = int(pix[idD]);  gA = int(pix[idD + np]);  bA = int(pix[idD + n2])
            else:
                ci = (~int(n3d[idD]) * 3) & 255
                rA = int(aux[256 + ci])
                gA = int(aux[256 + ((ci + 85)  & 255)])
                bA = int(aux[256 + ((ci + 170) & 255)])
            if 0 <= zF < h:
                rB = int(pix[idF]);  gB = int(pix[idF + np]);  bB = int(pix[idF + n2])
            else:
                ci = (~int(n3d[idF]) * 3) & 255
                rB = int(aux[256 + ci])
                gB = int(aux[256 + ((ci + 85)  & 255)])
                bB = int(aux[256 + ((ci + 170) & 255)])
            aux[tr + j] = ((rA * eA1) >> 8) + ((rB * eF1) >> 8)
            aux[tg + j] = ((gA * eA1) >> 8) + ((gB * eF1) >> 8)
            aux[tb + j] = ((bA * eA1) >> 8) + ((bB * eF1) >> 8)
        for j in range(h):
            pix[j * w + i]      = int(aux[tr + j])
            pix[j * w + i + np] = int(aux[tg + j])
            pix[j * w + i + n2] = int(aux[tb + j])


# ------------------------------------------------------------------
# Push pixel buffer to display (native)
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _push_display():
    gfx = graphics
    pb  = _pix_buf
    w = _W; h = _H; n = _NPIX; n2 = _N2
    for y in range(h):
        row = y * w
        for x in range(w):
            idx = row + x
            gfx.set_pen(gfx.create_pen(int(pb[idx]), int(pb[idx + n]), int(pb[idx + n2])))
            gfx.pixel(x, y)


# ------------------------------------------------------------------
# init / draw / deinit
# ------------------------------------------------------------------
def init():
    global _pix_buf, _noise3d, _aux, _dbg_frame, _dbg_t_start
    global _W, _H, _NPIX, _N2, _NFX, _NFY, _AUX_TR, _AUX_TG, _AUX_TB

    _W    = W if W else 32
    _H    = H if H else 32
    _NPIX = _W * _H
    _N2   = _NPIX * 2

    # Same noise-cell count on any panel (7 and 9 per 32px originally).
    _NFX = max(1, (7 * 32) // _W)
    _NFY = max(1, (9 * 32) // _H)

    # Scratch rows sit after the two 256-byte LUTs and must hold the longer
    # axis, since the row pass walks width and the column pass walks height.
    span    = _W if _W > _H else _H
    _AUX_TR = 512
    _AUX_TG = 512 + span
    _AUX_TB = 512 + span * 2

    _pix_buf = bytearray(_NPIX * 3)
    _noise3d = bytearray(_NPIX)
    _aux     = bytearray(512 + span * 3)
    _nc[0] = 0;  _nc[1] = 0;  _nc[2] = 0
    _build_luts()
    for _ in range(16):
        _update_noise(_noise3d, _cos8lut, _nc)
    _init_pixels()
    _dbg_frame = 0
    print("[soap] init — {}x{} nfx={} nfy={}".format(_W, _H, _NFX, _NFY))


def draw():
    global _dbg_frame, _dbg_t_start
    t0 = time.ticks_ms()
    _update_noise(_noise3d, _cos8lut, _nc)
    t1 = time.ticks_ms()
    _soap_rows_v(_pix_buf, _aux, _noise3d)
    t2 = time.ticks_ms()
    _soap_cols_v(_pix_buf, _aux, _noise3d)
    t3 = time.ticks_ms()
    _push_display()
    t4 = time.ticks_ms()
    _dbg_frame += 1
    if _dbg_frame == 1:
        _dbg_t_start = t0
    if _dbg_frame % 30 == 0:
        el = time.ticks_diff(t0, _dbg_t_start)
        fps = _dbg_frame * 1000 // el if el > 0 else 0
        print("[soap] noise={:d} rows={:d} cols={:d} push={:d} fps={:d}".format(
            time.ticks_diff(t1, t0), time.ticks_diff(t2, t1),
            time.ticks_diff(t3, t2), time.ticks_diff(t4, t3), fps))


def deinit():
    global _pix_buf, _noise3d, _aux
    _pix_buf = None
    _noise3d = None
    _aux     = None
