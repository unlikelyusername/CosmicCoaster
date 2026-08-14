# /effects/glitchv2.py
#
# Hardware-failure glitch effects — temporally coherent, animated, legible.
#
# CONTRAST WITH glitch.py:
#   glitch.py effects snap on/off with randomised parameters each trigger.
#   The viewer sees noise, but not a recognisable failure mode.
#
#   Every effect here carries state across frames. Parameters evolve with
#   velocity. Effects have onset → hold → decay phases. The viewer can read
#   the *story* of specific hardware breaking down and recovering:
#   a tape head drifting off lock, an image losing vertical sync and rolling,
#   a magnetic field sweeping through the frame, a signal at the edge of
#   sync lock jittering. These are legible because the same failure mode
#   always produces the same spatial and temporal signature.
#
# Effects:
#   TRACKING_DRIFT  Bottom N rows drift horizontally as a tape head loses lock.
#                   Displacement accumulates over frames to a peak (onset),
#                   holds briefly (the mechanism slipping to a stop), then
#                   decays back to 0 (re-lock). Top 2 rows of the damage zone
#                   taper (¼ dx, ½ dx) — a soft edge, not a hard cut.
#
#   SYNC_ROLL       Vertical sync loss — the image slowly rolls. A sub-row
#                   accumulator drives smooth motion below 1 row/frame.
#                   Velocity ramps up (onset), holds, then ramps down (decay).
#                   On re-lock, offset snaps to 0 — that snap IS the re-lock.
#                   A noise band at the seam marks the scan head crossing.
#                   Roll is applied at display-push time (not to the buffer),
#                   so tracking/desat/jitter roll with the image — correct,
#                   they are source-side damage, not display-side damage.
#
#   DESAT_SWEEP     Magnetic interference — a desaturation band sweeps
#                   vertically across the frame at sub-row velocity.
#                   Color drains as the band passes, then recovers.
#                   The continuous motion makes the field source feel physical.
#                   No onset/decay states: the sweep itself is the animation.
#
#   JITTER          Sync edge noise — per-row horizontal micro-displacement
#                   (±1–3 px). Individual row offsets are re-randomised every
#                   frame (correct: jitter IS noisy per-frame), but the
#                   *envelope* (intensity) rises, holds, and falls smoothly.
#                   This is the visual signature of a signal right at the
#                   edge of sync lock.
#
# Architecture:
#   Phase system: IDLE → ONSET → HOLD → DECAY → IDLE for each effect.
#   Velocity fields + sub-pixel accumulators drive continuous motion.
#   All state in module-level scalars — no dynamic allocation in draw loop.
#   Effects stack on a shared pixel buffer in source order; sync roll is
#   applied at push time so it correctly wraps all source-side damage.
#
# Effect contract: graphics=None, cu=None, init(), draw(), deinit()

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

# Horizontal wrap fast path. When the width is a power of two the wrap is a
# single AND, which is what the original hardcoded as `& 31`. Cosmic (32) and
# Stellar (16) both qualify; Galactic (53) does not and takes the split-loop
# path instead. Set in init(): mask value, or 0 meaning "not a power of two".
_WMASK = 31

# ------------------------------------------------------------------
# Phase states (shared by all effects)
# ------------------------------------------------------------------
_IDLE  = const(0)
_ONSET = const(1)
_HOLD  = const(2)
_DECAY = const(3)

# ------------------------------------------------------------------
# Effect indices
# ------------------------------------------------------------------
_TD = const(0)   # tracking_drift
_SR = const(1)   # sync_roll
_DS = const(2)   # desat_sweep
_JT = const(3)   # jitter
_NE = const(4)

# ------------------------------------------------------------------
# Mood tables — trigger probability per frame (%)
# Order: TD, SR, DS, JT
# ------------------------------------------------------------------
_MOOD_PROBS = [
    #         TD   SR   DS   JT
    bytearray([ 3,  1,  2,  5]),   # 0 DORMANT    barely alive
    bytearray([22,  5, 10, 18]),   # 1 DEGRADING  tracking going bad
    bytearray([ 8, 28,  5, 14]),   # 2 ROLLING    vertical sync issues
    bytearray([10,  5, 24, 22]),   # 3 FIELD      magnetic + jitter
    bytearray([22, 20, 18, 28]),   # 4 FAILING    everything going wrong
]
_MOOD_NAMES = ['DORMANT', 'DEGRADING', 'ROLLING', 'FIELD', 'FAILING']

# ------------------------------------------------------------------
# Neon palette — base layer color bars (8 colours × 3 bytes)
# ------------------------------------------------------------------
_NEON = bytearray([
    255,   0,  60,
      0, 255, 100,
      0, 200, 255,
    255, 200,   0,
    180,   0, 255,
    255,  60,   0,
      0, 255, 255,
    255, 255,   0,
])

# ------------------------------------------------------------------
# Pixel buffers (allocated in init, freed in deinit)
# ------------------------------------------------------------------
_rbuf = None
_gbuf = None
_bbuf = None

# Shared temp row buffer (avoids allocation in apply loops)
# Row scratch buffers — one display width. Reallocated in init().
_tmp_r = bytearray(32)
_tmp_g = bytearray(32)
_tmp_b = bytearray(32)

# ------------------------------------------------------------------
# TRACKING_DRIFT state
#
# Tape head loses horizontal lock on the bottom N rows.
# dx accumulates toward target (onset), holds, then decays back to 0.
# _td_vel is the rate of change in px/frame (1 or 2).
# ------------------------------------------------------------------
_td_phase  = _IDLE
_td_dx     = 0     # current displacement in pixels (signed)
_td_target = 0     # peak displacement to drift toward
_td_vel    = 0     # |px per frame| during onset and decay
_td_rows   = 0     # how many rows from the bottom are affected
_td_hold   = 0     # frames to hold at peak before decay begins

# ------------------------------------------------------------------
# SYNC_ROLL state
#
# Sub-row accumulator (×8 fixed point) drives smooth rolling below
# 1 row/frame. Velocity ramps up during onset, holds, then ramps
# down during decay. On decay reaching vel=0, offset snaps to 0
# (re-lock). _sr_dir is +1 (down) or -1 (up).
# ------------------------------------------------------------------
_sr_phase  = _IDLE
_sr_offset = 0     # current roll offset in rows (0–31)
_sr_sub    = 0     # sub-row accumulator (×8 fixed point, always 0–7)
_sr_vel    = 0     # row advance per frame × 8 (e.g. 8 = exactly 1 row/frame)
_sr_vel_t  = 0     # target velocity reached at end of onset
_sr_hold   = 0     # frames to hold at peak velocity
_sr_dir    = 1     # +1=roll down, -1=roll up
_SR_NOISE  = const(2)   # half-width of noise band at seam in rows

# ------------------------------------------------------------------
# DESAT_SWEEP state
#
# Band sweeps from off-screen, across the display, off-screen again.
# _ds_y is the band centre; starts at -_ds_half (top entry) or
# 32+_ds_half (bottom entry). Sub-row accumulator (×4 fixed point)
# drives smooth motion. No onset/decay: the sweep IS the animation.
# ------------------------------------------------------------------
_ds_phase    = _IDLE
_ds_y        = 0   # centre row of the band (may be outside 0–31)
_ds_half     = 0   # half-width of band in rows
_ds_strength = 0   # desaturation depth 0–255 (255 = full grey)
_ds_vel      = 0   # advance per frame × 4 (e.g. 4 = 1 row/frame)
_ds_sub      = 0   # sub-row accumulator (×4 fixed point, always 0–3)
_ds_dir      = 1   # +1=sweep down, -1=sweep up

# ------------------------------------------------------------------
# JITTER state
#
# Intensity (max ± px displacement) rises from 0 to target (onset),
# holds, then falls back to 0 (decay). Per-row offsets re-randomise
# every active frame — that's intentional, jitter IS frame-noisy.
# Only the envelope is smooth.
# ------------------------------------------------------------------
_jt_phase     = _IDLE
_jt_intensity = 0  # current max ± displacement in pixels (0–3)
_jt_target    = 0  # peak intensity for this episode
_jt_hold      = 0  # frames to hold at peak

# ------------------------------------------------------------------
# Mood / hue state
# ------------------------------------------------------------------
_mood      = 0
_next_mood = 0
_hue_t     = 0
_hue_step  = 1


# ------------------------------------------------------------------
# Mood picker
# ------------------------------------------------------------------
def _pick_mood():
    global _mood, _next_mood
    old = _mood
    while _mood == old:
        _mood = random.randint(0, len(_MOOD_PROBS) - 1)
    _next_mood = time.ticks_add(time.ticks_ms(), random.randint(6000, 10000))
    print("[glitchv2] mood ->", _MOOD_NAMES[_mood])


# ------------------------------------------------------------------
# Base layer — animated neon color bars (viper)
# 8 bars × 4 px wide; hue cycles over time.
# Odd rows slightly dimmed for CRT scanline texture.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _render_base(rb: ptr8, gb: ptr8, bb: ptr8, ht: int):
    W = int(_W)
    H = int(_H)
    hs = int(_HUE_STEP)
    for x in range(W):
        bar = x >> 2
        h   = (bar * hs + ht) & 255
        hi  = (h * 6) >> 8
        f   = (h * 6) & 255
        q   = 255 - f
        r = int(0); g = int(0); b = int(0)
        if   hi == 0: r = int(255); g = f;        b = int(0)
        elif hi == 1: r = q;        g = int(255); b = int(0)
        elif hi == 2: r = int(0);   g = int(255); b = f
        elif hi == 3: r = int(0);   g = q;        b = int(255)
        elif hi == 4: r = f;        g = int(0);   b = int(255)
        else:         r = int(255); g = int(0);   b = q
        for y in range(H):
            bright = int(255) if (y & 1) == 0 else int(210)
            idx = y * W + x
            rb[idx] = (r * bright) >> 8
            gb[idx] = (g * bright) >> 8
            bb[idx] = (b * bright) >> 8


# ------------------------------------------------------------------
# TRACKING_DRIFT apply (native)
#
# Shifts the bottom _td_rows rows by _td_dx pixels (wrap-around).
# Top 2 rows of the zone taper: ¼ dx then ½ dx — a soft edge that
# reads as the head slipping rather than a hard data cut.
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _apply_td(rb, gb, bb):
    dx    = _td_dx
    rows  = _td_rows
    tr    = _tmp_r; tg = _tmp_g; tb = _tmp_b
    w = _W; h = _H; mask = _WMASK
    y_top = h - rows
    for y in range(y_top, h):
        depth = y - y_top
        if depth == 0:
            adx = dx >> 2     # ¼ displacement — soft entry into damage zone
        elif depth == 1:
            adx = dx >> 1     # ½ displacement
        else:
            adx = dx          # full displacement in the body of the zone
        if adx == 0:
            continue
        base = y * w
        if mask:
            # Power-of-two width: single AND, as the original did.
            for x in range(w):
                sx = (x - adx) & mask
                tr[x] = rb[base + sx]
                tg[x] = gb[base + sx]
                tb[x] = bb[base + sx]
        else:
            # General width: split at the wrap point rather than branching per
            # pixel. The source index is monotonic, so x < d is the wrapped run.
            d = adx % w
            for x in range(d):
                sx = x - d + w
                tr[x] = rb[base + sx]
                tg[x] = gb[base + sx]
                tb[x] = bb[base + sx]
            for x in range(d, w):
                sx = x - d
                tr[x] = rb[base + sx]
                tg[x] = gb[base + sx]
                tb[x] = bb[base + sx]
        for x in range(w):
            rb[base + x] = tr[x]
            gb[base + x] = tg[x]
            bb[base + x] = tb[x]


# ------------------------------------------------------------------
# DESAT_SWEEP apply (viper — touches every pixel in the band)
#
# Blends each pixel toward its greyscale value by `strength` (0–255).
# Formula: result = r + (strength × (grey − r)) / 256
# This is a weighted average: always stays in 0–255, no clamping needed.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _apply_ds(rb: ptr8, gb: ptr8, bb: ptr8, y0: int, y1: int, strength: int):
    w = int(_W)
    for y in range(y0, y1):
        base = y * w
        for x in range(w):
            idx = base + x
            r = int(rb[idx]); g = int(gb[idx]); b = int(bb[idx])
            grey = (r + g + b) // 3
            rb[idx] = r + (strength * (grey - r)) // 256
            gb[idx] = g + (strength * (grey - g)) // 256
            bb[idx] = b + (strength * (grey - b)) // 256


# ------------------------------------------------------------------
# JITTER apply (native)
#
# Each row gets an independent random ± displacement this frame.
# Re-randomising every frame is intentional — jitter IS frame-noisy.
# The envelope (_jt_intensity) is what's smooth across frames.
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _apply_jt(rb, gb, bb):
    intensity = _jt_intensity
    tr = _tmp_r; tg = _tmp_g; tb = _tmp_b
    w = _W; h = _H; mask = _WMASK
    for y in range(h):
        dx = random.randint(-intensity, intensity)
        if dx == 0:
            continue
        base = y * w
        if mask:
            for x in range(w):
                sx = (x - dx) & mask
                tr[x] = rb[base + sx]
                tg[x] = gb[base + sx]
                tb[x] = bb[base + sx]
        else:
            d = dx % w
            for x in range(d):
                sx = x - d + w
                tr[x] = rb[base + sx]
                tg[x] = gb[base + sx]
                tb[x] = bb[base + sx]
            for x in range(d, w):
                sx = x - d
                tr[x] = rb[base + sx]
                tg[x] = gb[base + sx]
                tb[x] = bb[base + sx]
        for x in range(w):
            rb[base + x] = tr[x]
            gb[base + x] = tg[x]
            bb[base + x] = tb[x]


# ------------------------------------------------------------------
# Push display with optional sync-roll offset (native)
#
# Roll is applied here rather than to the buffer. Display row y reads
# buffer row (y + roll_off) % 32. Applying at push time means
# tracking/desat/jitter effects roll with the image — correct, because
# they are source-side damage (on the tape), not display-side damage.
#
# The seam (where the display wraps to a different part of the buffer)
# shows a band of greyscale noise — the scan head crossing point.
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _push_display(roll_off, seam_y, noise_half):
    gfx = graphics
    rb = _rbuf; gb = _gbuf; bb = _bbuf
    w = _W; h = _H
    for y in range(h):
        # Vertical wrap — was `& 31`, which silently used the WIDTH mask for a
        # row index. Identical at 32x32; wrong on any non-square panel.
        src_y = y + roll_off
        if src_y >= h: src_y -= h
        row   = src_y * w
        near  = (abs(y - seam_y) < noise_half) if noise_half > 0 else False
        for x in range(w):
            if near:
                v = random.randint(0, 50)
                gfx.set_pen(gfx.create_pen(v, v, v))
            else:
                idx = row + x
                gfx.set_pen(gfx.create_pen(int(rb[idx]), int(gb[idx]), int(bb[idx])))
            gfx.pixel(x, y)


# ------------------------------------------------------------------
# TRACKING_DRIFT state machine
# ------------------------------------------------------------------
def _start_td():
    global _td_phase, _td_dx, _td_target, _td_vel, _td_rows, _td_hold
    _td_dx     = 0
    _td_target = random.randint(4, 14) * (1 if random.randint(0, 1) else -1)
    _td_vel    = random.randint(1, 2)
    _td_rows   = random.randint(8, 16)
    _td_hold   = random.randint(4, 14)
    _td_phase  = _ONSET


def _tick_td():
    global _td_phase, _td_dx, _td_hold

    if _td_phase == _IDLE:
        if random.randint(0, 99) < int(_MOOD_PROBS[_mood][_TD]):
            _start_td()
        return

    if _td_phase == _ONSET:
        # accumulate dx toward target one step per frame
        if _td_dx < _td_target:
            _td_dx = min(_td_dx + _td_vel, _td_target)
        else:
            _td_dx = max(_td_dx - _td_vel, _td_target)
        if _td_dx == _td_target:
            _td_phase = _HOLD

    elif _td_phase == _HOLD:
        _td_hold -= 1
        if _td_hold <= 0:
            _td_phase = _DECAY

    elif _td_phase == _DECAY:
        # drift back toward 0
        if _td_dx > 0:
            _td_dx = max(0, _td_dx - _td_vel)
        else:
            _td_dx = min(0, _td_dx + _td_vel)
        if _td_dx == 0:
            _td_phase = _IDLE
            return

    _apply_td(_rbuf, _gbuf, _bbuf)


# ------------------------------------------------------------------
# SYNC_ROLL state machine
# ------------------------------------------------------------------
def _start_sr():
    global _sr_phase, _sr_offset, _sr_sub, _sr_vel, _sr_vel_t, _sr_hold, _sr_dir
    _sr_offset = 0
    _sr_sub    = 0
    _sr_vel    = 0
    _sr_vel_t  = random.randint(4, 12)    # target ×8: 0.5–1.5 rows/frame
    _sr_hold   = random.randint(15, 45)   # frames at peak velocity
    _sr_dir    = 1 if random.randint(0, 1) else -1
    _sr_phase  = _ONSET


def _tick_sr():
    global _sr_phase, _sr_offset, _sr_sub, _sr_vel, _sr_hold

    if _sr_phase == _IDLE:
        if random.randint(0, 99) < int(_MOOD_PROBS[_mood][_SR]):
            _start_sr()
        return

    # update velocity per phase
    if _sr_phase == _ONSET:
        _sr_vel += 1
        if _sr_vel >= _sr_vel_t:
            _sr_vel   = _sr_vel_t
            _sr_phase = _HOLD

    elif _sr_phase == _HOLD:
        _sr_hold -= 1
        if _sr_hold <= 0:
            _sr_phase = _DECAY

    elif _sr_phase == _DECAY:
        _sr_vel -= 1
        if _sr_vel <= 0:
            # re-lock: snap offset back to 0 (that's what re-lock looks like)
            _sr_offset = 0
            _sr_sub    = 0
            _sr_phase  = _IDLE
            return

    # advance sub-row accumulator (vel always positive; dir applies to offset)
    _sr_sub    += _sr_vel
    rows_adv    = _sr_sub >> 3        # // 8
    _sr_sub     = _sr_sub & 7         # % 8, always 0–7
    _sr_offset  = (_sr_offset + rows_adv * _sr_dir) % _H


# ------------------------------------------------------------------
# DESAT_SWEEP state machine
# ------------------------------------------------------------------
def _start_ds():
    global _ds_phase, _ds_y, _ds_half, _ds_strength, _ds_vel, _ds_sub, _ds_dir
    _ds_half     = random.randint(3, 7)
    _ds_strength = random.randint(160, 255)
    _ds_vel      = random.randint(2, 6)    # ×4: 0.5–1.5 rows/frame
    _ds_dir      = 1 if random.randint(0, 1) else -1
    _ds_sub      = 0
    # start just off-screen so the entry itself is visible
    _ds_y        = -_ds_half if _ds_dir == 1 else _H + _ds_half
    _ds_phase    = _HOLD     # sweep is the entire animation; HOLD = active


def _tick_ds():
    global _ds_phase, _ds_y, _ds_sub

    if _ds_phase == _IDLE:
        if random.randint(0, 99) < int(_MOOD_PROBS[_mood][_DS]):
            _start_ds()
        return

    # advance band position
    _ds_sub += _ds_vel
    adv      = _ds_sub >> 2       # // 4
    _ds_sub  = _ds_sub & 3        # % 4, always 0–3
    _ds_y   += adv * _ds_dir

    # apply to visible portion of band
    y0 = max(0, _ds_y - _ds_half)
    y1 = min(_H, _ds_y + _ds_half)
    if y0 < y1:
        _apply_ds(_rbuf, _gbuf, _bbuf, y0, y1, _ds_strength)

    # exit when band has fully swept off the other edge
    if _ds_dir == 1 and (_ds_y - _ds_half) >= _H:
        _ds_phase = _IDLE
    elif _ds_dir == -1 and (_ds_y + _ds_half) < 0:
        _ds_phase = _IDLE


# ------------------------------------------------------------------
# JITTER state machine
# ------------------------------------------------------------------
def _start_jt():
    global _jt_phase, _jt_intensity, _jt_target, _jt_hold
    _jt_intensity = 0
    _jt_target    = random.randint(1, 3)
    _jt_hold      = random.randint(8, 28)
    _jt_phase     = _ONSET


def _tick_jt():
    global _jt_phase, _jt_intensity, _jt_hold

    if _jt_phase == _IDLE:
        if random.randint(0, 99) < int(_MOOD_PROBS[_mood][_JT]):
            _start_jt()
        return

    if _jt_phase == _ONSET:
        _jt_intensity += 1
        if _jt_intensity >= _jt_target:
            _jt_intensity = _jt_target
            _jt_phase     = _HOLD

    elif _jt_phase == _HOLD:
        _jt_hold -= 1
        if _jt_hold <= 0:
            _jt_phase = _DECAY

    elif _jt_phase == _DECAY:
        _jt_intensity -= 1
        if _jt_intensity <= 0:
            _jt_intensity = 0
            _jt_phase     = _IDLE
            return

    _apply_jt(_rbuf, _gbuf, _bbuf)


# ------------------------------------------------------------------
# init / draw / deinit
# ------------------------------------------------------------------
def init():
    global _rbuf, _gbuf, _bbuf, _hue_t, _mood, _next_mood
    global _td_phase, _td_dx
    global _sr_phase, _sr_offset, _sr_sub, _sr_vel
    global _ds_phase
    global _jt_phase, _jt_intensity
    global _W, _H, _NPIX, _HUE_STEP, _tmp_r, _tmp_g, _tmp_b, _WMASK

    _W    = W if W else 32
    _H    = H if H else 32
    _NPIX = _W * _H

    # Colour bars are 4px wide; hue completes one 256-step cycle across
    # however many bars fit. Width 32 gives 8 bars and a step of 32, which is
    # why the original could hardcode it.
    n_bars    = max(1, (_W + 3) >> 2)
    _HUE_STEP = max(1, 256 // n_bars)

    # Power-of-two width -> wrap with a mask; otherwise 0 selects the split loop.
    _WMASK = (_W - 1) if (_W & (_W - 1)) == 0 else 0

    _tmp_r = bytearray(_W)
    _tmp_g = bytearray(_W)
    _tmp_b = bytearray(_W)

    _rbuf  = bytearray(_NPIX)
    _gbuf  = bytearray(_NPIX)
    _bbuf  = bytearray(_NPIX)
    _hue_t = 0

    _mood      = 1   # start DEGRADING — most readable from cold start
    _next_mood = time.ticks_add(time.ticks_ms(), random.randint(6000, 10000))

    _td_phase = _IDLE;  _td_dx        = 0
    _sr_phase = _IDLE;  _sr_offset    = 0;  _sr_sub = 0;  _sr_vel = 0
    _ds_phase = _IDLE
    _jt_phase = _IDLE;  _jt_intensity = 0

    print("[glitchv2] init — mood:", _MOOD_NAMES[_mood])


def draw():
    global _hue_t

    now = time.ticks_ms()
    if time.ticks_diff(_next_mood, now) <= 0:
        _pick_mood()

    _hue_t = (_hue_t + _hue_step) & 255
    _render_base(_rbuf, _gbuf, _bbuf, _hue_t)

    # Source-side effects: applied to the buffer before display push.
    # Order: tracking (large spatial shift) → desat (colour) → jitter (micro).
    _tick_td()
    _tick_ds()
    _tick_jt()

    # Sync roll: update state, then apply at push time.
    # The buffer contents (including tracking/desat/jitter) roll with the
    # image — they are on the tape, not on the display head.
    _tick_sr()
    if _sr_phase != _IDLE and _sr_offset != 0:
        seam_y     = (_H - _sr_offset) % _H
        noise_half = _SR_NOISE
    else:
        seam_y     = 0
        noise_half = 0
    _push_display(_sr_offset, seam_y, noise_half)


def deinit():
    global _rbuf, _gbuf, _bbuf
    _rbuf = None
    _gbuf = None
    _bbuf = None
