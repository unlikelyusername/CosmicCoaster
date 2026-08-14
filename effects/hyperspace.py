# /effects/hyperspace.py
#
# 3D starfield — each star has a ring buffer of its last TRAIL_LEN
# projected screen positions. Trails drawn as gradient line segments:
# dim/faded tail → bright/white-hot head. Additive blending — overlapping
# trails bloom to white. Stars fly toward the viewer (Z decreasing),
# reset to far plane when they pass the near clip or leave the screen.
#
# Auto-mutations every 5–9 s:
#   WARP BURST   5× speed for 1.5–2 s   — stars streak outward
#   DRIFT SLOW   0.15× speed for 1.5–3s — eerie ghost crawl
#   SCATTER      all stars instantly reset
#   COLOR SHIFT  next palette
#
# Palettes: CYAN (classic), MATRIX (green), MAGENTA, AMBER
#
# Resolution-independent. Both axes share the same focal length, so a
# non-square panel shows a letterbox crop of the same view cone rather than a
# stretched image.
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
_NS   = const(48)    # stars
_TL   = const(10)    # trail length (ring buffer slots per star)

# Projection focal length.
# At z=1 (near), x=±1.0 → px = 16 ± _FOCAL
_FOCAL = const(12)

# Base Z decrement per frame (approach speed at warp_mult=1)
_BASE_SPEED = 0.013

# ------------------------------------------------------------------
# Color palettes — (tail_r, tail_g, tail_b, head_r, head_g, head_b)
# Tail = oldest slot (dim), head = newest slot (bright/white-hot)
# ------------------------------------------------------------------
_PALETTES = [
    (  0,  20,  60,  140, 220, 255),  # Cyan — classic hyperspace
    (  0,  30,   0,   80, 255,  80),  # Matrix green
    ( 50,   0,  50,  255,  60, 255),  # Magenta
    ( 50,  25,   0,  255, 180,  40),  # Amber
]

# ------------------------------------------------------------------
# Module state
# ------------------------------------------------------------------
_x  = [0.0] * _NS   # star x ∈ ±1.0
_y  = [0.0] * _NS   # star y ∈ ±1.0
_z  = [0.0] * _NS   # star z ∈ (0, 1.0]  — near=0, far=1

# Flat trail ring buffers — screen coords 0..31; 255 = invalid slot.
# Layout: star i, ring slot j → index i*_TL + j
_trail_x  = bytearray(_NS * _TL)
_trail_y  = bytearray(_NS * _TL)
_trail_hd = bytearray(_NS)   # write-head per star (0.._TL-1)

# Current projected pixel position for each star, fed to viper
_px_cur   = bytearray(_NS * 2)   # [x0,y0, x1,y1, …]; 255 = off-screen

# RGB accumulation buffer — all trails composited here before display
_rbuf = bytearray(_NPIX)
_gbuf = bytearray(_NPIX)
_bbuf = bytearray(_NPIX)

# Warp state
_warp_mult  = 1.0
_warp_until = 0

# Mutation state
_palette  = 0
_next_mut = 0

# ------------------------------------------------------------------
# Star management
# ------------------------------------------------------------------
def _reset_star(i):
    """Randomise position on far half of z-range; clear trail."""
    _x[i] = random.uniform(-1.0, 1.0)
    _y[i] = random.uniform(-1.0, 1.0)
    _z[i] = random.uniform(0.55, 1.0)
    base = i * _TL
    for j in range(_TL):
        _trail_x[base + j] = 255
        _trail_y[base + j] = 255
    _trail_hd[i] = 0


def _scatter():
    for i in range(_NS):
        _reset_star(i)


def _mutate():
    global _warp_mult, _warp_until, _palette, _next_mut
    now = time.ticks_ms()
    _next_mut = time.ticks_add(now, random.randint(5000, 9000))
    roll = random.randint(0, 3)
    if roll == 0:
        _warp_mult  = 5.0
        _warp_until = time.ticks_add(now, random.randint(1200, 2200))
        print("[hyperspace] WARP BURST x5")
    elif roll == 1:
        _warp_mult  = 0.15
        _warp_until = time.ticks_add(now, random.randint(1500, 3000))
        print("[hyperspace] DRIFT SLOW x0.15")
    elif roll == 2:
        _scatter()
        print("[hyperspace] SCATTER")
    else:
        _palette = (_palette + 1) % len(_PALETTES)
        print("[hyperspace] COLOR ->", _palette)


# ------------------------------------------------------------------
# Clear accumulation buffers to black (viper)
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _clear_bufs(rb: ptr8, gb: ptr8, bb: ptr8):
    for i in range(int(_NPIX)):
        rb[i] = 0
        gb[i] = 0
        bb[i] = 0


# ------------------------------------------------------------------
# Core trail render (viper)
#
# For each star:
#   1. Push current projected position into the ring buffer.
#   2. Walk buffer oldest→newest (age 0=tail, TL-1=head).
#   3. Between consecutive valid positions draw a gradient line segment.
#      Colour gradient: tail_rgb (dim) → head_rgb (bright).
#   4. Additive blend into accumulation buffer, clamped at 255.
#
# Distance approximation in line drawing: integer step interpolation.
# All bounds-checked; off-screen segments are clipped per pixel.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _render_trails(rb: ptr8, gb: ptr8, bb: ptr8,
                   pxc: ptr8, txb: ptr8, tyb: ptr8, thb: ptr8,
                   tr: int, tg: int, tb: int,
                   hr: int, hg: int, hb: int):
    TL  = int(10)
    NS  = int(48)
    W   = int(_W)
    H   = int(_H)
    TL1 = int(9)    # TL - 1, divisor for gradient normalisation

    for i in range(NS):
        # Push current position to ring buffer at write head
        hd = int(thb[i])
        txb[i * TL + hd] = int(pxc[i * 2])
        tyb[i * TL + hd] = int(pxc[i * 2 + 1])
        hd = (hd + 1) % TL
        thb[i] = hd          # advance head AFTER push

        # Walk: oldest = hd (just vacated), newest = hd-1 mod TL
        px0 = int(-1)
        py0 = int(-1)

        for age in range(TL):
            slot = (hd + age) % TL
            spx  = int(txb[i * TL + slot])
            spy  = int(tyb[i * TL + slot])

            if spx == 255 or spy == 255:   # invalid / off-screen slot
                px0 = int(-1); py0 = int(-1)
                continue

            # Gradient: age 0=tail(dim) → TL-1=head(bright)
            gp = age * 255 // TL1
            ng = 255 - gp
            rv = (tr * ng + hr * gp) >> 8
            gv = (tg * ng + hg * gp) >> 8
            bv = (tb * ng + hb * gp) >> 8

            if px0 >= 0:
                # Interpolated line segment px0,py0 → spx,spy
                dx   = spx - px0
                dy   = spy - py0
                adx  = dx if dx >= 0 else -dx
                ady  = dy if dy >= 0 else -dy
                steps = adx if adx > ady else ady
                if steps == 0: steps = int(1)
                for s in range(steps + 1):
                    lx = px0 + dx * s // steps
                    ly = py0 + dy * s // steps
                    if lx >= 0 and lx < W and ly >= 0 and ly < H:
                        idx = ly * W + lx
                        v = int(rb[idx]) + rv; rb[idx] = v if v < 255 else 255
                        v = int(gb[idx]) + gv; gb[idx] = v if v < 255 else 255
                        v = int(bb[idx]) + bv; bb[idx] = v if v < 255 else 255
            elif spx >= 0 and spx < W and spy >= 0 and spy < H:
                idx = spy * W + spx
                v = int(rb[idx]) + rv; rb[idx] = v if v < 255 else 255
                v = int(gb[idx]) + gv; gb[idx] = v if v < 255 else 255
                v = int(bb[idx]) + bv; bb[idx] = v if v < 255 else 255

            px0 = spx; py0 = spy


# ------------------------------------------------------------------
# Push accumulation buffer to display.
# Clears display first (fast C call), then draws only non-black pixels.
# With 48 stars and short trails most pixels are black — big speedup.
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _push_display():
    gfx = graphics
    rb  = _rbuf; gb = _gbuf; bb = _bbuf
    gfx.set_pen(gfx.create_pen(0, 0, 0))
    gfx.clear()
    w = _W; h = _H
    for y in range(h):
        row = y * w
        for x in range(w):
            idx = row + x
            rv = int(rb[idx]); gv = int(gb[idx]); bv = int(bb[idx])
            if rv | gv | bv:
                gfx.set_pen(gfx.create_pen(rv, gv, bv))
                gfx.pixel(x, y)


# ------------------------------------------------------------------
# init / draw / deinit
# ------------------------------------------------------------------
def init():
    global _warp_mult, _warp_until, _palette, _next_mut
    global _W, _H, _NPIX, _rbuf, _gbuf, _bbuf

    _W    = W if W else 32
    _H    = H if H else 32
    _NPIX = _W * _H

    # Trail coords are stored as uint8 with 255 as the off-screen sentinel, so
    # the panel must stay under 255 on each axis. All three Unicorns do.
    _rbuf = bytearray(_NPIX)
    _gbuf = bytearray(_NPIX)
    _bbuf = bytearray(_NPIX)

    _warp_mult  = 1.0
    _warp_until = 0
    _palette    = 0
    _next_mut   = time.ticks_add(time.ticks_ms(), random.randint(5000, 9000))

    for i in range(_NS):
        # Stagger initial z-depths so grid fills immediately rather than
        # all stars popping in from the far plane at once.
        _x[i] = random.uniform(-1.0, 1.0)
        _y[i] = random.uniform(-1.0, 1.0)
        _z[i] = random.uniform(0.02, 1.0)
        base = i * _TL
        for j in range(_TL):
            _trail_x[base + j] = 255
            _trail_y[base + j] = 255
        _trail_hd[i] = 0

    print("[hyperspace] init — {}x{}, {} stars, trail={}".format(
        _W, _H, _NS, _TL))


@micropython.native  # noqa: F821
def draw():
    global _warp_mult, _warp_until

    now = time.ticks_ms()

    if _warp_until and time.ticks_diff(_warp_until, now) <= 0:
        _warp_mult  = 1.0
        _warp_until = 0

    if time.ticks_diff(_next_mut, now) <= 0:
        _mutate()

    speed = _BASE_SPEED * _warp_mult
    w = _W; h = _H
    cx = w / 2.0; cy = h / 2.0

    # -- Project star positions and detect resets --
    for i in range(_NS):
        _z[i] -= speed
        z = _z[i]
        if z < 0.001: z = 0.001
        iz  = 1.0 / z
        px  = int(_x[i] * iz * _FOCAL + cx)
        py  = int(_y[i] * iz * _FOCAL + cy)
        # Both coords get sentinel 255 if EITHER is off-screen.
        # Storing (15, 255) with only spx checked in viper creates
        # a line segment toward y=255, clipped to the screen edge = vertical stripe.
        if 0 <= px < w and 0 <= py < h:
            _px_cur[i * 2]     = px
            _px_cur[i * 2 + 1] = py
        else:
            _px_cur[i * 2]     = 255
            _px_cur[i * 2 + 1] = 255
        # Reset when past near plane or sufficiently off-screen
        if _z[i] < 0.02 or px < -6 or px > w + 6 or py < -6 or py > h + 6:
            _reset_star(i)

    # -- Composite all trails, push to display --
    _clear_bufs(_rbuf, _gbuf, _bbuf)
    tr, tg, tb, hr, hg, hb = _PALETTES[_palette]
    _render_trails(_rbuf, _gbuf, _bbuf,
                   _px_cur, _trail_x, _trail_y, _trail_hd,
                   tr, tg, tb, hr, hg, hb)
    _push_display()


def deinit():
    global _rbuf, _gbuf, _bbuf
    _rbuf = None
    _gbuf = None
    _bbuf = None