# /lib/surface_rgb888.py
#
# Pixel layer for a packed RGB888 framebuffer: one little-endian
# 0x00RRGGBB word per pixel, row-major. That is PicoGraphics' RGB888
# layout, verified on a Cosmic by writing a known pen and reading the
# buffer back.
#
# Pairs with wire3d.build_path(), which does all the geometry and leaves a
# segment list in the data array. Nothing here divides or projects; it
# walks segments and pokes pixels. This module is the ONLY thing that has
# to be rewritten for a different pixel format (see surface_rgb565), which
# is why the geometry lives elsewhere — viper cannot parameterise a
# pointer type, so ptr32 and ptr16 rasters are necessarily separate
# functions, and the split keeps the duplication to this file.
#
# No clear() here: gfx.set_pen(0); gfx.clear() is a C-level memset inside
# PicoGraphics and beats any viper loop.
#
# THREE RASTERS, NOT ONE. draw_path, draw_path_aa and draw_path_wide are
# separate functions that duplicate their blend arithmetic rather than
# sharing it. That is deliberate on both counts:
#   * a shared helper would be a viper CALL per pixel, and a viper call
#     entry measured 18us — ruinous inside a per-pixel loop
#   * one function handling every combination is how the batching attempt
#     grew to 278 lines and four levels of nesting, which hard-faulted the
#     board on frame 0 every time (see the incident record in wire3d.py).
#     Each function here stays short and at most two deep.
# Callers pick one at bind time with select(), so the choice costs nothing
# per path.
#
# BLEND MODES (data[17])
#   0 additive  — saturating add. Crossing trails sum toward white, which
#                 is what makes hyperspace read as painting with light.
#                 This is the effect, not overhead.
#   1 max       — brightest wins. Like additive, order-independent.
#   2 opaque    — last writer wins. Cheapest per pixel and the only one
#                 that needs depth sorting to look right.
#   3 alpha     — constant-alpha blend from data[29] (0..256).
# Additive and max are commutative, so they never need sorting. Opaque and
# alpha are not. The two cheapest modes are exactly the two that require
# the expensive per-path work.

from micropython import const

import wire3d

BYTES_PER_PIXEL = const(4)

# Must equal wire3d.SEGS. It cannot simply be imported: const() only
# inlines inside its defining module, so an imported name reaches viper as
# an object and fails to compile. Declared locally to stay a compile-time
# literal, and asserted here so the two can never silently drift.
_SEGS = const(160)
assert _SEGS == wire3d.SEGS, "surface_rgb888._SEGS out of sync with wire3d.SEGS"

BLEND_ADD = const(0)
BLEND_MAX = const(1)
BLEND_OPAQUE = const(2)
BLEND_ALPHA = const(3)


# ------------------------------------------------------------------
# Exponential per-channel decay, in place. This is what leaves motion
# trails behind moving geometry. Black pixels are skipped with a single
# test rather than three, so a mostly-dark frame costs little.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def fade(fb: ptr32, data: ptr32):
    dec_r = int(data[22]); dec_g = int(data[23]); dec_b = int(data[24])
    npix = int(data[14]) * int(data[15])
    for i in range(npix):
        v = int(fb[i])
        if v:
            r = ((((v >> 16) & 255) * dec_r) >> 8) << 16
            g = ((((v >> 8) & 255) * dec_g) >> 8) << 8
            b = ((v & 255) * dec_b) >> 8
            fb[i] = r | g | b


# ------------------------------------------------------------------
# Rasterise the segment list built by wire3d.build_path().
#
# Every segment's endpoints are already inside the screen and its DDA
# increments and gradient span are precomputed, so this loop contains no
# division and no bounds test.
#
# data[19] is the LUT index mask: 255 for a 256-entry table, 1023 for a
# 1024-entry one. A deep table costs 3KB and buys enough hue resolution
# for a gradient to sweep the wheel several times across one ramp.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def draw_path(fb: ptr32, lut: ptr8, data: ptr32):
    nseg = int(data[26])
    width = int(data[14])
    bright = int(data[16]); blend = int(data[17])
    phase = int(data[18]); mask = int(data[19])
    alpha = int(data[29])

    for k in range(nseg):
        sc = _SEGS + k * 8
        fx = (int(data[sc]) << 16) + 32768
        fy = (int(data[sc + 1]) << 16) + 32768
        xi = int(data[sc + 2]); yi = int(data[sc + 3])
        pfx = int(data[sc + 5]); pstep = int(data[sc + 6])
        count = int(data[sc + 4]) + int(data[sc + 7])

        for _s in range(count):
            li = (((pfx >> 16) + phase) & mask) * 3
            rv = (int(lut[li]) * bright) >> 8
            gv = (int(lut[li + 1]) * bright) >> 8
            bv = (int(lut[li + 2]) * bright) >> 8
            idx = (fy >> 16) * width + (fx >> 16)
            old = int(fb[idx])
            if old:
                if blend == 1:
                    o = (old >> 16) & 255
                    if o > rv: rv = o
                    o = (old >> 8) & 255
                    if o > gv: gv = o
                    o = old & 255
                    if o > bv: bv = o
                elif blend == 0:
                    rv += (old >> 16) & 255
                    if rv > 255: rv = 255
                    gv += (old >> 8) & 255
                    if gv > 255: gv = 255
                    bv += old & 255
                    if bv > 255: bv = 255
                elif blend == 3:
                    rv = (rv * alpha + ((old >> 16) & 255) * (256 - alpha)) >> 8
                    gv = (gv * alpha + ((old >> 8) & 255) * (256 - alpha)) >> 8
                    bv = (bv * alpha + (old & 255) * (256 - alpha)) >> 8
            fb[idx] = (rv << 16) | (gv << 8) | bv
            fx += xi
            fy += yi
            pfx += pstep


# ------------------------------------------------------------------
# Anti-aliased 1px raster, additive or max only.
#
# At 32x32 this is the best value in the whole feature set: the per-pixel
# terms are nearly free here (hyperdrive lights ~150 pixels a frame, so
# all blending and LUT lookup together cost well under a millisecond),
# while a diagonal line at this resolution is visibly a staircase. Roughly
# doubling a term that small to remove the staircase is close to free.
#
# The method is the cheap one: walk the same DDA, and split each pixel's
# intensity between the two pixels straddling the true line along the
# minor axis, weighted by the DDA's own fractional error. Not Xiaolin Wu
# — no endpoint handling — but the ends of these lines are already faded
# by the gradient.
#
# Opaque and alpha are not offered. Anti-aliasing an opaque line means
# compositing coverage, which is a different algorithm; additive AA is
# what a glowing trail actually wants.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def draw_path_aa(fb: ptr32, lut: ptr8, data: ptr32):
    nseg = int(data[26])
    width = int(data[14]); height = int(data[15])
    bright = int(data[16]); blend = int(data[17])
    phase = int(data[18]); mask = int(data[19])
    npix = width * height

    for k in range(nseg):
        sc = _SEGS + k * 8
        fx = (int(data[sc]) << 16) + 32768
        fy = (int(data[sc + 1]) << 16) + 32768
        xi = int(data[sc + 2]); yi = int(data[sc + 3])
        pfx = int(data[sc + 5]); pstep = int(data[sc + 6])
        count = int(data[sc + 4]) + int(data[sc + 7])

        # The major axis advances a full pixel per step, so the OTHER axis
        # carries the sub-pixel error worth spreading.
        axi = xi if xi >= 0 else -xi
        ayi = yi if yi >= 0 else -yi
        xmajor = 1 if axi >= ayi else 0
        step = width if xmajor else 1     # neighbour offset in the framebuffer

        for _s in range(count):
            li = (((pfx >> 16) + phase) & mask) * 3
            r0 = (int(lut[li]) * bright) >> 8
            g0 = (int(lut[li + 1]) * bright) >> 8
            b0 = (int(lut[li + 2]) * bright) >> 8
            px = fx >> 16
            py = fy >> 16
            idx = py * width + px
            # fractional part of the minor axis: how far between the two
            # candidate pixels the true line actually falls
            frac = ((fy >> 8) & 255) if xmajor else ((fx >> 8) & 255)
            w1 = frac
            w0 = 256 - frac

            for _pass in range(2):
                if _pass:
                    i2 = idx + step
                    w = w1
                else:
                    i2 = idx
                    w = w0
                if w and i2 >= 0 and i2 < npix:
                    rv = (r0 * w) >> 8
                    gv = (g0 * w) >> 8
                    bv = (b0 * w) >> 8
                    old = int(fb[i2])
                    if old:
                        if blend:
                            o = (old >> 16) & 255
                            if o > rv: rv = o
                            o = (old >> 8) & 255
                            if o > gv: gv = o
                            o = old & 255
                            if o > bv: bv = o
                        else:
                            rv += (old >> 16) & 255
                            if rv > 255: rv = 255
                            gv += (old >> 8) & 255
                            if gv > 255: gv = 255
                            bv += old & 255
                            if bv > 255: bv = 255
                    fb[i2] = (rv << 16) | (gv << 8) | bv
            fx += xi
            fy += yi
            pfx += pstep


# ------------------------------------------------------------------
# Thick line raster, additive or max only. data[27] is the width in
# pixels (2 or 3; 1 should use draw_path, which is cheaper).
#
# Barely matters at 32x32, where 1px is already a bold stroke. It matters
# a great deal at 480x480, where 1px is a hairline — which is the whole
# reason it exists before there is a 480x480 effect to use it.
#
# Extra pixels are laid perpendicular to the major axis, so a thick line
# keeps its width regardless of direction without any per-pixel distance
# work. Bounds ARE tested here: the rails guarantee the centre pixel is on
# screen and say nothing about its neighbours.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def draw_path_wide(fb: ptr32, lut: ptr8, data: ptr32):
    nseg = int(data[26])
    width = int(data[14]); height = int(data[15])
    bright = int(data[16]); blend = int(data[17])
    phase = int(data[18]); mask = int(data[19])
    thick = int(data[27])
    if thick < 2:
        thick = 2
    lo = -(thick >> 1)
    hi = lo + thick

    for k in range(nseg):
        sc = _SEGS + k * 8
        fx = (int(data[sc]) << 16) + 32768
        fy = (int(data[sc + 1]) << 16) + 32768
        xi = int(data[sc + 2]); yi = int(data[sc + 3])
        pfx = int(data[sc + 5]); pstep = int(data[sc + 6])
        count = int(data[sc + 4]) + int(data[sc + 7])

        axi = xi if xi >= 0 else -xi
        ayi = yi if yi >= 0 else -yi
        xmajor = 1 if axi >= ayi else 0

        for _s in range(count):
            li = (((pfx >> 16) + phase) & mask) * 3
            r0 = (int(lut[li]) * bright) >> 8
            g0 = (int(lut[li + 1]) * bright) >> 8
            b0 = (int(lut[li + 2]) * bright) >> 8
            px = fx >> 16
            py = fy >> 16

            for d in range(lo, hi):
                if xmajor:
                    nx = px
                    ny = py + d
                else:
                    nx = px + d
                    ny = py
                if nx >= 0 and nx < width and ny >= 0 and ny < height:
                    rv = r0; gv = g0; bv = b0
                    idx = ny * width + nx
                    old = int(fb[idx])
                    if old:
                        if blend:
                            o = (old >> 16) & 255
                            if o > rv: rv = o
                            o = (old >> 8) & 255
                            if o > gv: gv = o
                            o = old & 255
                            if o > bv: bv = o
                        else:
                            rv += (old >> 16) & 255
                            if rv > 255: rv = 255
                            gv += (old >> 8) & 255
                            if gv > 255: gv = 255
                            bv += old & 255
                            if bv > 255: bv = 255
                    fb[idx] = (rv << 16) | (gv << 8) | bv
            fx += xi
            fy += yi
            pfx += pstep


# ------------------------------------------------------------------
# Bind-time selection. Call once and keep the result; picking per path
# would add a Python-level branch to the term that already dominates.
# ------------------------------------------------------------------
def select(data):
    """Return the raster matching the flags currently set in `data`."""
    if int(data[27]) > 1:
        return draw_path_wide
    if int(data[28]):
        return draw_path_aa
    return draw_path
