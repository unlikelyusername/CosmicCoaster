# /effects/glitch.py
#
# Animated neon color bars with stacked, autonomous glitch effects.
# Every effect runs independently with its own timer and trigger probability.
# A "mood" system reshuffles effect probabilities every 6–10 seconds,
# creating distinct periods: calm, shredded, corrupt, chromatic, storm, melt.
#
# Glitch effects (all stackable, all active simultaneously possible):
#   H_SLICE     Horizontal row tears — slices shift left/right, VHS tape damage
#   PB_BURST    Pixel burst static — spray of random neon pixels
#   BK_CORRUPT  Block corruption — random solid-color rectangular artifacts
#   SC_DROP     Scanline dropout — rows go black (display failure)
#   IV_FLASH    Inversion flash — full-screen colour invert, 1–2 frames
#   CH_DRIFT    Chromatic drift — R shifts right, B shifts left (aberration)
#   FREEZE      Frame freeze — display locks for N frames then snaps back
#   BAND_SHIFT  Structured band shift — 3–5 consecutive rows shifted left/right
#               Three sub-modes: BLOCK (uniform), STAIR (progressive +2px/row),
#               SCATTER (per-row random). Wrap or black-fill on edges.
#
# Moods (cycle, each lasting 6–10 s):
#   CALM    low probability everything, brief durations
#   STORM   all effects high probability, longer durations
#   SHRED   h_slice + scanline + band_shift dominant
#   STATIC  pixel_burst + block dominant
#   CHROMA  channel_drift + invert dominant
#   MELT    all effects, medium-high — the "nightclub" mode
#
# Effect contract: graphics=None, cu=None, init(), draw(), deinit()

import math
import random
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
_HUE_STEP = 32   # 256 // number of colour bars; set in init()

# ------------------------------------------------------------------
# Effect indices
# ------------------------------------------------------------------
_HS = const(0)   # h_slice
_PB = const(1)   # pixel_burst
_BC = const(2)   # block_corrupt
_SD = const(3)   # scanline_drop
_IV = const(4)   # invert_flash
_CD = const(5)   # channel_drift
_FZ = const(6)   # freeze
_BS = const(7)   # band_shift
_NE = const(8)   # number of effects

# BAND_SHIFT sub-modes
_BS_BLOCK   = const(0)   # all rows same dx
_BS_STAIR   = const(1)   # each row += 2px (progressive staircase)
_BS_SCATTER = const(2)   # each row independent random dx

# ------------------------------------------------------------------
# Mood tables — (probs_per_frame_pct, max_duration_frames)
# Order: HS, PB, BC, SD, IV, CD, FZ
# ------------------------------------------------------------------
_MOOD_PROBS = [
    #         HS   PB   BC   SD   IV   CD   FZ   BS
    bytearray([ 3,  2,  2,  1,  1,  3,  1,  2]),   # 0 CALM
    bytearray([22, 18, 18, 14,  8, 20,  6, 12]),   # 1 STORM
    bytearray([35,  5,  5, 28,  5,  5,  2, 20]),   # 2 SHRED
    bytearray([ 5, 32, 28,  8,  5,  5,  2,  6]),   # 3 STATIC
    bytearray([ 5,  5,  3,  5, 14, 35,  5,  5]),   # 4 CHROMA
    bytearray([18, 14, 14, 11,  8, 18,  6, 14]),   # 5 MELT
]
_MOOD_DURS = [
    #         HS   PB   BC   SD   IV   CD   FZ   BS
    bytearray([ 3,  2,  3,  2,  1, 12,  8,  3]),   # 0 CALM
    bytearray([ 6,  3,  5,  2,  2, 22, 16,  8]),   # 1 STORM
    bytearray([ 7,  2,  3,  3,  1, 10,  8,  6]),   # 2 SHRED
    bytearray([ 3,  4,  6,  2,  2,  8,  5,  4]),   # 3 STATIC
    bytearray([ 3,  2,  2,  2,  2, 28, 10,  3]),   # 4 CHROMA
    bytearray([ 5,  3,  4,  3,  2, 16, 12,  6]),   # 5 MELT
]
_MOOD_NAMES = ['CALM', 'STORM', 'SHRED', 'STATIC', 'CHROMA', 'MELT']

# ------------------------------------------------------------------
# Neon palette for burst/block effects (8 colours, 3 bytes each)
# ------------------------------------------------------------------
_NEON = bytearray([
    255,   0,  60,   # hot pink
      0, 255, 100,   # neon green
      0, 200, 255,   # electric blue
    255, 200,   0,   # amber
    180,   0, 255,   # purple
    255,  60,   0,   # orange
      0, 255, 255,   # cyan
    255, 255,   0,   # yellow
])

# ------------------------------------------------------------------
# Module state — pixel buffers
# ------------------------------------------------------------------
_rbuf = None   # bytearray(_NPIX) — working render buffer
_gbuf = None
_bbuf = None

# Temp row buffers for h_slice (avoids dynamic allocation in draw loop)
# Row scratch buffers — one display width. Reallocated in init().
_tmp_r = bytearray(32)
_tmp_g = bytearray(32)
_tmp_b = bytearray(32)

# ------------------------------------------------------------------
# Module state — effect timers / active flags
# ------------------------------------------------------------------
_on    = bytearray(_NE)    # 1 = active, 0 = inactive
_timer = bytearray(_NE)    # frames remaining (capped at 255)

# H_SLICE parameters (max 6 slices)
_hs_n      = 0
_hs_y      = [0] * 6   # row y per slice
_hs_dx     = [0] * 6   # signed dx per slice (negative=left, positive=right)

# PB_BURST parameters
_pb_count  = 0          # pixels per frame

# BK_CORRUPT parameters (max 8 blocks, 5 bytes each: x y w h color_idx)
_bc_n      = 0
_bc_data   = bytearray(8 * 5)

# SC_DROP parameters (max 8 rows)
_sd_n      = 0
_sd_rows   = bytearray(8)

# CH_DRIFT parameters (signed, stored as plain Python ints)
_cd_dr     = 0    # R shift (+ve = right, -ve = left)
_cd_db     = 0    # B shift

# FREEZE: no extra params — uses existing _rbuf/_gbuf/_bbuf
_fz_countdown = 0

# BAND_SHIFT parameters
_bs_y     = 0           # first row of the band
_bs_n     = 0           # number of rows (3–5)
_bs_wrap  = 0           # 1=wrap around, 0=black fill
# per-row dx stored as dx+16 (range -15..+15 → 1..31) in a bytearray
_bs_dxs   = bytearray(5)

# Base hue animation
_hue_t     = 0
_hue_step  = 1    # LUT steps per frame; slower = more gradual shift

# Mood state
_mood      = 0
_next_mood = 0    # ticks_ms

# ------------------------------------------------------------------
# Mood mutation
# ------------------------------------------------------------------
def _pick_mood():
    global _mood, _next_mood
    old = _mood
    while _mood == old:
        _mood = random.randint(0, len(_MOOD_PROBS) - 1)
    _next_mood = time.ticks_add(time.ticks_ms(), random.randint(6000, 10000))
    print("[glitch] mood ->", _MOOD_NAMES[_mood])


# ------------------------------------------------------------------
# Effect trigger functions
# ------------------------------------------------------------------
def _trigger_hs():
    global _hs_n
    _hs_n = random.randint(2, 5)
    for i in range(_hs_n):
        _hs_y[i]  = random.randint(0, _H - 1)
        mag = random.randint(5, 14)
        _hs_dx[i] = mag if random.randint(0, 1) else -mag
    dur = random.randint(2, int(_MOOD_DURS[_mood][_HS]))
    _on[_HS] = 1; _timer[_HS] = dur


def _trigger_pb():
    global _pb_count
    _pb_count = random.randint(25, 90)
    dur = random.randint(1, int(_MOOD_DURS[_mood][_PB]))
    _on[_PB] = 1; _timer[_PB] = dur


def _trigger_bc():
    global _bc_n
    _bc_n = random.randint(3, 8)
    for i in range(_bc_n):
        b = i * 5
        _bc_data[b]     = random.randint(0, 28)   # x
        _bc_data[b + 1] = random.randint(0, 28)   # y
        _bc_data[b + 2] = random.randint(2, 5)    # w
        _bc_data[b + 3] = random.randint(2, 5)    # h
        _bc_data[b + 4] = random.randint(0, 7)    # color_idx
    dur = random.randint(2, int(_MOOD_DURS[_mood][_BC]))
    _on[_BC] = 1; _timer[_BC] = dur


def _trigger_sd():
    global _sd_n
    _sd_n = random.randint(2, 6)
    for i in range(_sd_n):
        _sd_rows[i] = random.randint(0, _H - 1)
    dur = random.randint(1, int(_MOOD_DURS[_mood][_SD]))
    _on[_SD] = 1; _timer[_SD] = dur


def _trigger_iv():
    dur = random.randint(1, max(1, int(_MOOD_DURS[_mood][_IV])))
    _on[_IV] = 1; _timer[_IV] = dur


def _trigger_cd():
    global _cd_dr, _cd_db
    mag = random.randint(1, 3)
    _cd_dr =  mag
    _cd_db = -mag   # opposite direction = chromatic aberration
    dur = random.randint(int(_MOOD_DURS[_mood][_CD]) // 2,
                         int(_MOOD_DURS[_mood][_CD]))
    _on[_CD] = 1; _timer[_CD] = max(1, dur)


def _trigger_fz():
    global _fz_countdown
    dur = random.randint(5, int(_MOOD_DURS[_mood][_FZ]))
    _fz_countdown = dur
    _on[_FZ] = 1; _timer[_FZ] = dur


def _trigger_bs():
    global _bs_y, _bs_n, _bs_wrap
    _bs_n    = random.randint(3, 5)
    _bs_y    = random.randint(0, max(0, _H - _bs_n))
    _bs_wrap = 0 if random.randint(0, 3) == 0 else 1   # 75% wrap, 25% black
    base_mag = random.randint(3, 12)
    sign     = 1 if random.randint(0, 1) else -1
    mode     = random.randint(0, 2)
    for i in range(_bs_n):
        if mode == _BS_BLOCK:
            dx = sign * base_mag
        elif mode == _BS_STAIR:
            dx = sign * (base_mag + i * 2)   # +3, +5, +7 … per row
        else:                                 # _BS_SCATTER
            mag = random.randint(2, base_mag + 3)
            dx  = mag if random.randint(0, 1) else -mag
        _bs_dxs[i] = max(1, min(31, dx + 16))   # clamp into bytearray range
    dur = random.randint(2, int(_MOOD_DURS[_mood][_BS]))
    _on[_BS] = 1; _timer[_BS] = dur


# ------------------------------------------------------------------
# BAND_SHIFT apply (native)
# Reads per-row dx from _bs_dxs (stored as dx+16), shifts each row,
# optionally wrapping content around the display edge.
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _apply_bs(rb, gb, bb):
    y0   = _bs_y
    n    = _bs_n
    dxs  = _bs_dxs
    wrap = _bs_wrap
    tr   = _tmp_r; tg = _tmp_g; tb = _tmp_b
    w = _W; h = _H
    for i in range(n):
        y    = y0 + i
        if y >= h: break
        dx   = int(dxs[i]) - 16   # decode stored value back to signed dx
        base = y * w
        for x in range(w):
            sx = x - dx
            if wrap:
                # Was `sx & 31`, a power-of-two shortcut that only wraps at
                # width 32. dx is bounded to +/-15, so one conditional does the
                # same job at any width without a modulo.
                if sx < 0:    sx += w
                elif sx >= w: sx -= w
            if 0 <= sx < w:
                tr[x] = rb[base + sx]
                tg[x] = gb[base + sx]
                tb[x] = bb[base + sx]
            else:
                tr[x] = 0; tg[x] = 0; tb[x] = 0
        for x in range(w):
            rb[base + x] = tr[x]
            gb[base + x] = tg[x]
            bb[base + x] = tb[x]


_TRIGGERS = [_trigger_hs, _trigger_pb, _trigger_bc,
             _trigger_sd, _trigger_iv, _trigger_cd, _trigger_fz, _trigger_bs]


# ------------------------------------------------------------------
# Base layer — animated neon color bars (viper)
#
# 8 bars × 4 px wide; hue cycles over time.
# Alternating rows slightly dimmer for CRT scanline texture.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _render_base(rb: ptr8, gb: ptr8, bb: ptr8, ht: int):
    W  = int(_W)
    H  = int(_H)
    hs = int(_HUE_STEP)
    for x in range(W):
        bar = x >> 2            # x // 4 = bar index
        h   = (bar * hs + ht) & 255
        hi  = (h * 6) >> 8
        f   = (h * 6) & 255
        q   = 255 - f
        r   = int(0); g = int(0); b = int(0)
        if   hi == 0: r = int(255); g = f;        b = int(0)
        elif hi == 1: r = q;        g = int(255); b = int(0)
        elif hi == 2: r = int(0);   g = int(255); b = f
        elif hi == 3: r = int(0);   g = q;        b = int(255)
        elif hi == 4: r = f;        g = int(0);   b = int(255)
        else:         r = int(255); g = int(0);   b = q
        for y in range(H):
            # Alternating-row scanline: full brightness even rows, 82% odd
            bright = int(255) if (y & 1) == 0 else int(210)
            idx = y * W + x
            rb[idx] = (r * bright) >> 8
            gb[idx] = (g * bright) >> 8
            bb[idx] = (b * bright) >> 8


# ------------------------------------------------------------------
# H_SLICE — horizontal row shift (native, uses pre-allocated tmp rows)
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _apply_hs(rb, gb, bb):
    n   = _hs_n
    tr  = _tmp_r; tg = _tmp_g; tb = _tmp_b
    w   = _W
    for si in range(n):
        y    = _hs_y[si]
        dx   = _hs_dx[si]    # positive = shift right
        base = y * w
        for x in range(w):
            sx = x - dx
            tr[x] = rb[base + sx] if 0 <= sx < w else 0
            tg[x] = gb[base + sx] if 0 <= sx < w else 0
            tb[x] = bb[base + sx] if 0 <= sx < w else 0
        for x in range(w):
            rb[base + x] = tr[x]
            gb[base + x] = tg[x]
            bb[base + x] = tb[x]


# ------------------------------------------------------------------
# PB_BURST — random neon static (native)
# Re-randomised every active frame → flickering shower
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _apply_pb(rb, gb, bb):
    n    = _pb_count
    neon = _NEON
    for _ in range(n):
        x  = random.randint(0, _W - 1)
        y  = random.randint(0, _H - 1)
        ci = random.randint(0, 7) * 3
        idx = y * _W + x
        rb[idx] = neon[ci]
        gb[idx] = neon[ci + 1]
        bb[idx] = neon[ci + 2]


# ------------------------------------------------------------------
# BK_CORRUPT — solid-colour block artifacts (native)
# Block positions baked at trigger time; held fixed for duration
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _apply_bc(rb, gb, bb):
    n    = _bc_n
    data = _bc_data
    neon = _NEON
    w = _W; h = _H
    for i in range(n):
        b   = i * 5
        bx  = int(data[b]);     by  = int(data[b + 1])
        bw  = int(data[b + 2]); bh  = int(data[b + 3])
        ci  = int(data[b + 4]) * 3
        cr  = int(neon[ci]); cg = int(neon[ci + 1]); cb = int(neon[ci + 2])
        for dy in range(bh):
            ry = by + dy
            if ry >= h: break
            row = ry * w
            for dx in range(bw):
                rx = bx + dx
                if rx >= w: break
                idx = row + rx
                rb[idx] = cr; gb[idx] = cg; bb[idx] = cb


# ------------------------------------------------------------------
# SC_DROP — scanline blackout (native)
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _apply_sd(rb, gb, bb):
    rows = _sd_rows
    w = _W
    for i in range(_sd_n):
        base = int(rows[i]) * w
        for x in range(w):
            rb[base + x] = 0
            gb[base + x] = 0
            bb[base + x] = 0


# ------------------------------------------------------------------
# IV_FLASH — full invert (viper)
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _apply_iv(rb: ptr8, gb: ptr8, bb: ptr8):
    for i in range(int(_NPIX)):
        rb[i] = 255 - int(rb[i])
        gb[i] = 255 - int(gb[i])
        bb[i] = 255 - int(bb[i])


# ------------------------------------------------------------------
# CH_DRIFT — chromatic channel shift (viper)
# R shifts right by dr, B shifts left by |db| (db is negative).
# In-place: process direction chosen to avoid read-before-write.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _apply_cd(rb: ptr8, bb: ptr8, dr: int, db: int):
    W = int(_W)
    H = int(_H)
    for y in range(H):
        base = y * W
        # R channel
        if dr > 0:      # shift right: iterate right→left
            for x in range(W - 1, -1, -1):
                sx = x - dr
                rb[base + x] = rb[base + sx] if sx >= 0 else 0
        elif dr < 0:    # shift left: iterate left→right
            ndr = -dr
            for x in range(W):
                sx = x + ndr
                rb[base + x] = rb[base + sx] if sx < W else 0
        # B channel
        if db > 0:
            for x in range(W - 1, -1, -1):
                sx = x - db
                bb[base + x] = bb[base + sx] if sx >= 0 else 0
        elif db < 0:
            ndb = -db
            for x in range(W):
                sx = x + ndb
                bb[base + x] = bb[base + sx] if sx < W else 0


# ------------------------------------------------------------------
# Push working buffer to display (full 1024 pixels — base covers all)
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _push_display():
    gfx = graphics
    rb  = _rbuf; gb = _gbuf; bb = _bbuf
    w = _W; h = _H
    for y in range(h):
        row = y * w
        for x in range(w):
            idx = row + x
            gfx.set_pen(gfx.create_pen(int(rb[idx]), int(gb[idx]), int(bb[idx])))
            gfx.pixel(x, y)


# ------------------------------------------------------------------
# init / draw / deinit
# ------------------------------------------------------------------
def init():
    global _rbuf, _gbuf, _bbuf, _hue_t, _mood, _next_mood
    global _W, _H, _NPIX, _HUE_STEP, _tmp_r, _tmp_g, _tmp_b

    _W    = W if W else 32
    _H    = H if H else 32
    _NPIX = _W * _H

    # Colour bars are 4px wide, and the hue must complete one full 256-step
    # cycle across however many bars fit. At width 32 that is 8 bars and a step
    # of 32 — which is why the original could hardcode it.
    n_bars    = max(1, (_W + 3) >> 2)
    _HUE_STEP = max(1, 256 // n_bars)

    # Row scratch must match the display width.
    _tmp_r = bytearray(_W)
    _tmp_g = bytearray(_W)
    _tmp_b = bytearray(_W)

    _rbuf  = bytearray(_NPIX)
    _gbuf  = bytearray(_NPIX)
    _bbuf  = bytearray(_NPIX)
    _hue_t = 0
    _mood  = 5    # start in MELT — most interesting from cold
    _next_mood = time.ticks_add(time.ticks_ms(), random.randint(6000, 10000))

    for i in range(_NE):
        _on[i] = 0; _timer[i] = 0

    print("[glitch] init — {}x{} bars={} hue_step={} mood={}".format(
        _W, _H, n_bars, _HUE_STEP, _MOOD_NAMES[_mood]))


@micropython.native  # noqa: F821
def _effects_tick():
    """Apply active effects and roll for new triggers. Split from draw() to
    keep each native function within the compiler's instruction limit."""
    probs = _MOOD_PROBS[_mood]

    if _on[_HS]:
        _apply_hs(_rbuf, _gbuf, _bbuf)
        _timer[_HS] -= 1
        if _timer[_HS] == 0: _on[_HS] = 0
    elif random.randint(0, 99) < int(probs[_HS]):
        _trigger_hs()

    if _on[_PB]:
        _apply_pb(_rbuf, _gbuf, _bbuf)
        _timer[_PB] -= 1
        if _timer[_PB] == 0: _on[_PB] = 0
    elif random.randint(0, 99) < int(probs[_PB]):
        _trigger_pb()

    if _on[_BC]:
        _apply_bc(_rbuf, _gbuf, _bbuf)
        _timer[_BC] -= 1
        if _timer[_BC] == 0: _on[_BC] = 0
    elif random.randint(0, 99) < int(probs[_BC]):
        _trigger_bc()

    if _on[_SD]:
        _apply_sd(_rbuf, _gbuf, _bbuf)
        _timer[_SD] -= 1
        if _timer[_SD] == 0: _on[_SD] = 0
    elif random.randint(0, 99) < int(probs[_SD]):
        _trigger_sd()

    if _on[_IV]:
        _apply_iv(_rbuf, _gbuf, _bbuf)
        _timer[_IV] -= 1
        if _timer[_IV] == 0: _on[_IV] = 0
    elif random.randint(0, 99) < int(probs[_IV]):
        _trigger_iv()

    if _on[_CD]:
        _apply_cd(_rbuf, _bbuf, _cd_dr, _cd_db)
        _timer[_CD] -= 1
        if _timer[_CD] == 0: _on[_CD] = 0
    elif random.randint(0, 99) < int(probs[_CD]):
        _trigger_cd()

    if _on[_BS]:
        _apply_bs(_rbuf, _gbuf, _bbuf)
        _timer[_BS] -= 1
        if _timer[_BS] == 0: _on[_BS] = 0
    elif random.randint(0, 99) < int(probs[_BS]):
        _trigger_bs()

    # Freeze triggers AFTER render so it captures the glitched frame
    if not _on[_FZ] and random.randint(0, 99) < int(probs[_FZ]):
        _trigger_fz()


def draw():
    global _hue_t, _fz_countdown

    now = time.ticks_ms()

    if time.ticks_diff(_next_mood, now) <= 0:
        _pick_mood()

    # Freeze: skip render, re-display frozen buffer
    if _on[_FZ]:
        _fz_countdown -= 1
        if _fz_countdown <= 0:
            _on[_FZ] = 0; _timer[_FZ] = 0
        _push_display()
        return

    _hue_t = (_hue_t + _hue_step) & 255
    _render_base(_rbuf, _gbuf, _bbuf, _hue_t)
    _effects_tick()
    _push_display()


def deinit():
    global _rbuf, _gbuf, _bbuf
    _rbuf = None
    _gbuf = None
    _bbuf = None