# /effects/wireframe.py
#
# Two tumbling wireframe solids (cube, octahedron) on crossing orbits,
# rendered by lib/wire3d. Edge color is arc-length position in a
# gradient.py LUT, and the gradient phase advances every frame, so color
# flows along the edges. The accumulator fades instead of clearing, so
# motion leaves ghost trails. Orbits swing close enough that corners
# cross the near plane — the lib's 3D clip draws the visible part of
# clipped edges instead of dropping them.
#
# Each solid is one packed Q10 vertex array holding its edge paths in
# path order (loops + pillars), transformed in a single wire3d.transform
# call per frame, then drawn as a handful of polyline calls. Palettes
# rebake from gradient.random_palette() on a timer (one slow frame per
# swap, accepted).
#
# Effect contract: GEOMETRY; graphics, W, H injected; init/draw/deinit.

import math
import random
import time
from array import array

import gradient
import surface
import wire3d

graphics = None

GEOMETRY = "any"

W = 0    # injected by the loader
H = 0

_FOCAL  = 12
_Z_NEAR = 0.10
_FADE   = (205, 205, 218)   # slight blue bias: ghosts cool as they fade

_SWAP_MIN_MS = 9000
_SWAP_MAX_MS = 15000

# Cube: 16 packed Q10 points = bottom loop, top loop, four pillars.
# paths are (base, count, closed).
_CUBE_SIZE = 1.2
_CUBE_PTS = (
    (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),      # bottom loop
    (-1, -1,  1), (1, -1,  1), (1, 1,  1), (-1, 1,  1),      # top loop
    (-1, -1, -1), (-1, -1, 1), (1, -1, -1), (1, -1, 1),      # pillars
    (1,  1, -1), (1,  1,  1), (-1, 1, -1), (-1, 1, 1),
)
_CUBE_PATHS = ((0, 4, 1), (4, 4, 1), (8, 2, 0), (10, 2, 0), (12, 2, 0), (14, 2, 0))

# Octahedron: three closed equator squares, 12 packed points.
_OCTA_SIZE = 1.5
_OCTA_PTS = (
    (1, 0, 0), (0, 1, 0), (-1, 0, 0), (0, -1, 0),
    (1, 0, 0), (0, 0, 1), (-1, 0, 0), (0, 0, -1),
    (0, 1, 0), (0, 0, 1), (0, -1, 0), (0, 0, -1),
)
_OCTA_PATHS = ((0, 4, 1), (4, 4, 1), (8, 4, 1))

_WIDTH  = 32
_HEIGHT = 32

_w3   = None
_fb   = None      # writable framebuffer view
_px   = None      # pixel-layer module matching the framebuffer format
_objs = None      # list of dicts: model, scratch, paths, lut, angles, rates...

_last_ms   = 0
_next_swap = 0

_t_last = 0
_fcount = 0
_DEBUG_EVERY = 60


def _make_model(pts, size):
    m = array('i', bytes(4 * 3 * len(pts)))
    for i, (x, y, z) in enumerate(pts):
        m[i * 3]     = int(x * size * 1024)
        m[i * 3 + 1] = int(y * size * 1024)
        m[i * 3 + 2] = int(z * size * 1024)
    return m


def _make_obj(pts, size, paths, stops, orbit_phase):
    return {
        'model':   _make_model(pts, size),
        'scratch': array('i', bytes(4 * 3 * len(pts))),
        'npts':    len(pts),
        'paths':   paths,
        'lut':     wire3d.bake(stops),
        'ang':     [random.uniform(0, 6.28) for _ in range(3)],
        'rate':    [random.uniform(0.5, 1.1) * random.choice((-1, 1)) for _ in range(3)],
        'orbit':   orbit_phase,
        'phase':   0.0,
        'flow':    random.uniform(90.0, 160.0),   # gradient steps/second
    }


def init():
    global _w3, _fb, _px, _objs, _WIDTH, _HEIGHT, _last_ms, _next_swap
    global _t_last, _fcount

    _WIDTH  = W if W else 32
    _HEIGHT = H if H else 32
    _w3  = wire3d.new_data()
    wire3d.set_view(_w3, _WIDTH, _HEIGHT, _FOCAL, _Z_NEAR)
    _w3[17] = 0                      # additive: crossing edges bloom
    _w3[22] = _FADE[0]
    _w3[23] = _FADE[1]
    _w3[24] = _FADE[2]

    _fb, _px = surface.acquire(graphics, _WIDTH, _HEIGHT)
    if _fb is None:
        print("[wireframe] no writable framebuffer — effect will not render")

    _objs = [
        _make_obj(_CUBE_PTS, _CUBE_SIZE, _CUBE_PATHS, gradient.FIRE, 0.0),
        _make_obj(_OCTA_PTS, _OCTA_SIZE, _OCTA_PATHS, gradient.OCEAN, 3.14159),
    ]

    now = time.ticks_ms()
    _last_ms   = now
    _next_swap = time.ticks_add(now, random.randint(_SWAP_MIN_MS, _SWAP_MAX_MS))
    _t_last = now
    _fcount = 0

    print("[wireframe] init {}x{} objects={}".format(_WIDTH, _HEIGHT, len(_objs)))


def draw():
    global _last_ms, _next_swap, _t_last, _fcount

    if _fb is None:
        return

    now = time.ticks_ms()
    dt  = time.ticks_diff(now, _last_ms)
    _last_ms = now
    if dt > 100: dt = 100
    dts = dt * 0.001

    if time.ticks_diff(_next_swap, now) <= 0:
        _next_swap = time.ticks_add(now, random.randint(_SWAP_MIN_MS, _SWAP_MAX_MS))
        obj = _objs[random.randint(0, len(_objs) - 1)]
        stops, desc = gradient.random_palette()
        wire3d.bake(stops, obj['lut'])
        print("[wireframe] palette", desc)

    w3 = _w3
    _px.fade(_fb, w3)

    for obj in _objs:
        ang = obj['ang']
        for i in range(3):
            ang[i] += obj['rate'][i] * dts
            if ang[i] > 6.283185: ang[i] -= 6.283185
            elif ang[i] < 0.0:    ang[i] += 6.283185
        obj['orbit'] += 0.21 * dts
        if obj['orbit'] > 6.283185: obj['orbit'] -= 6.283185
        obj['phase'] += obj['flow'] * dts
        if obj['phase'] > 256.0: obj['phase'] -= 256.0

        p  = obj['orbit']
        oz = 5.5 + 3.3 * math.sin(p)
        ox = 1.8 * math.sin(p * 2.0 + 1.0)
        oy = 1.2 * math.sin(p * 3.0)

        wire3d.set_rotation(w3, ang[0], ang[1], ang[2])
        wire3d.set_translation(w3, ox, oy, oz)
        w3[9] = obj['npts']
        wire3d.transform(obj['model'], obj['scratch'], w3)

        br = int(620.0 / oz)
        if br > 256: br = 256
        elif br < 56: br = 56
        w3[16] = br
        w3[18] = int(obj['phase']) & 255

        lut = obj['lut']
        scratch = obj['scratch']
        for base, n, closed in obj['paths']:
            w3[9]  = n
            w3[20] = 0
            w3[21] = closed
            w3[25] = base
            wire3d.build_path(scratch, w3)
            _px.draw_path(_fb, lut, w3)

    _fcount += 1
    if _fcount % _DEBUG_EVERY == 0:
        wall = time.ticks_diff(now, _t_last)
        fps  = _DEBUG_EVERY * 1000 // max(wall, 1)
        print("[wireframe] fps={}".format(fps))
        _t_last = now


def deinit():
    global _w3, _fb, _px, _objs
    _w3   = None
    _fb   = None
    _px   = None
    _objs = None
