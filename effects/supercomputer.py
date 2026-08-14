# /effects/supercomputer.py
#
# Blinkenlights — every pixel is an amber lamp on its own cycle, flaring to
# full brightness then decaying, like a 1960s mainframe front panel.
#
# Effect contract:
#   GEOMETRY = "any"
#   graphics = None
#   W, H     = injected by the loader
#   init(); draw(); deinit()

import random
import gc
from micropython import const

GEOMETRY = "any"

graphics = None
W = 0    # injected by the loader
H = 0

_CR = const(230)
_CG = const(150)
_CB = const(0)

# Per-pixel state, sized in init() once the display dimensions are known.
_lifetime = None
_age      = None

# Scale factor: store float*1000 as int, max value ~1100
_SCALE = const(1000)
_INC   = const(25)    # 0.025 * 1000


def init():
    global _lifetime, _age
    gc.collect()
    n = W * H
    _lifetime = [0] * n
    _age      = [0] * n
    for i in range(n):
        _lifetime[i] = _SCALE + int(random.uniform(0.0, 0.1) * _SCALE)
        _age[i]      = int(random.uniform(0.0, 1.0) * _lifetime[i])


@micropython.native  # noqa: F821
def draw():
    lt = _lifetime; ag = _age
    sc = _SCALE; inc = _INC
    cr = _CR; cg = _CG; cb = _CB
    gfx = graphics
    w = W; h = H

    # Update ages
    for i in range(w * h):
        a = ag[i] + inc
        if a >= lt[i]:
            a = 0
            lt[i] = sc + int(random.uniform(0.0, 0.1) * sc)
        ag[i] = a

    # Render
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            a = ag[i]; l = lt[i]
            t30 = (l * 3) // 10
            t50 = l // 2
            if a < t30:
                gfx.set_pen(gfx.create_pen(cr, cg, cb))
            elif a < t50:
                decay_num = t50 - a
                decay_den = t50 - t30
                # decay 0..1 as integer fraction
                r = (cr * decay_num * 5) // (decay_den * sc // 100 + 1)
                g = (cg * decay_num * 5) // (decay_den * sc // 100 + 1)
                b = (cb * decay_num * 5) // (decay_den * sc // 100 + 1)
                if r > 255: r = 255
                if g > 255: g = 255
                if b > 255: b = 255
                gfx.set_pen(gfx.create_pen(r, g, b))
            else:
                gfx.set_pen(gfx.create_pen(0, 0, 0))
            gfx.pixel(x, y)


def deinit():
    global _lifetime, _age
    _lifetime = None
    _age = None
