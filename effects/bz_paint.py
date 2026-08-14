# /effects/bz_paint.py
# BZ reaction with persistent painted canvas — waves fade to black over time
#
# Barkley excitable-medium sim; pacemaker sources emit expanding rings that are
# painted onto a canvas which decays toward black, leaving trails.
#
# Resolution-independent. Ring spacing and source count scale with the panel
# (see init) so roughly the same number of rings is on screen on any board.
#
# Effect contract:
#   GEOMETRY = "any"
#   graphics = None
#   W, H     = injected by the loader
#   init(); draw(); deinit()

# ================================================================== #
#  ARTISTIC CONFIGURATION
# ================================================================== #

MOOD = 0.6

# Canvas decay per frame: 0-255 where 255=no fade, 0=instant black
# 230 ≈ half-life of ~10 frames, 245 ≈ ~25 frames, 252 ≈ ~60 frames
DECAY = 245

# Gradient: list of (phase_0_255, r, g, b) stops — interpolated between them
# phase 0 = quiescent/background, 255 = deep refractory
GRADIENT = [
    (  0,   0,   0,   0),   # black background
    ( 40,   0,  20,  80),   # dark blue — wave approaching
    ( 90,   0, 100, 255),   # bright blue — leading edge
    (128,   0, 255, 180),   # cyan-green — peak
    (170, 200, 255,   0),   # yellow — just past peak
    (210, 255,  60,   0),   # orange — refractory
    (240, 180,   0,  60),   # deep red — tail
    (255,  20,   0,  20),   # near-black red — recovery
]

# Source behavior
ARC_CHANCE        = 0.25
SPAWN_GAP_FRAMES  = 32
CROSSFADE_FRAMES  = 96   # unused — gradient replaces palettes

# Manual overrides (None = use MOOD)
NUM_SOURCES_OVERRIDE     = None
RING_SPACING_OVERRIDE    = None
WAVE_SHARPNESS_OVERRIDE  = None
STEPS_PER_FRAME_OVERRIDE = None
LIFESPAN_OVERRIDE        = None

# ================================================================== #
#  END OF CONFIG
# ================================================================== #

import gc
import time
import random
from array import array
from micropython import const

GEOMETRY = "any"

graphics = None
W = 0    # injected by the loader
H = 0

# Display dimensions as module globals rather than const(). The viper functions
# hoist them into locals once per call (`W = int(_W)`), so runtime values cost
# nothing in the inner loop — measured at 0.0% against the const version.
# init() sets all three from the injected W/H.
_W   = 32
_H   = 32
_WH  = 1024
_S   = const(1000)

# Barkley physics
_A_INT  = const(750)
_B_INT  = const(50)
_DU_INT = const(500)
_DT_INT = const(50)
_FIRE   = const(3)

_PACE_MARGIN = const(3)

# Derived from MOOD
_EPS_INT       = 40
_SPF           = 6
_num_sources   = 5
_ring_spacing  = 75
_lifespan_base = 400

# BZ state
_u  = None; _v  = None
_nu = None; _nv = None
_step = [0]

# Painted canvas: three separate byte-range arrays (0-255 per channel)
_cr = None; _cg = None; _cb = None

# Pre-computed gradient lookup (256 entries × 3 channels)
_gr = None; _gg = None; _gb = None

# Pacemakers: [x, y, period, phase, age, lifespan, is_arc]
_nuclei      = None
_spawn_queue = None

_t_last_frame = 0
_t_sim_total  = 0
_t_draw_total = 0
_frame_count  = 0
_DEBUG_EVERY  = 30


# --------------------------------------------------------------------- #
# Gradient table
# --------------------------------------------------------------------- #
def _build_gradient():
    global _gr, _gg, _gb
    stops = GRADIENT
    gr = array('i', [0] * 256)
    gg = array('i', [0] * 256)
    gb = array('i', [0] * 256)
    for si in range(len(stops) - 1):
        p0, r0, g0, b0 = stops[si]
        p1, r1, g1, b1 = stops[si + 1]
        span = p1 - p0
        if span <= 0:
            continue
        for i in range(span):
            t = i * 256 // span
            ti = 256 - t
            idx = p0 + i
            if 0 <= idx < 256:
                gr[idx] = (r0 * ti + r1 * t) >> 8
                gg[idx] = (g0 * ti + g1 * t) >> 8
                gb[idx] = (b0 * ti + b1 * t) >> 8
    # fill last stop
    p, r, g, b = stops[-1]
    if 0 <= p < 256:
        gr[p] = r; gg[p] = g; gb[p] = b
    _gr = gr; _gg = gg; _gb = gb


# --------------------------------------------------------------------- #
# Pacemakers
# --------------------------------------------------------------------- #
def _margins():
    """Inset bounds for source placement, clamped so a short panel still works.

    _PACE_MARGIN assumes room to spare. On an 11px-tall Galactic a fixed margin
    leaves almost nothing, and on anything shorter the range would invert and
    randint() would raise.
    """
    mx = min(_PACE_MARGIN, (_W - 1) // 2)
    my = min(_PACE_MARGIN, (_H - 1) // 2)
    return mx, my, _W - 1 - mx, _H - 1 - my


def _find_quiescent_spot():
    x_lo, y_lo, x_hi, y_hi = _margins()
    bx = random.randint(x_lo, x_hi)
    by = random.randint(y_lo, y_hi)
    bu = _u[by * _W + bx]
    for _ in range(12):
        x = random.randint(x_lo, x_hi)
        y = random.randint(y_lo, y_hi)
        val = _u[y * _W + x]
        if val < bu:
            bu = val; bx = x; by = y
    return bx, by


def _spawn_source():
    x, y   = _find_quiescent_spot()
    jitter = max(4, _ring_spacing // 8)
    period = _ring_spacing + random.randint(-jitter, jitter)
    phase  = random.randint(0, period - 1)
    life   = _lifespan_base + random.randint(-_lifespan_base // 4, _lifespan_base // 4)
    is_arc = 1 if random.random() < ARC_CHANCE else 0
    _nuclei.append([x, y, period, phase, 0, life, is_arc])
    print("[bzp] +source ({},{}) period={} arc={} n={}".format(
        x, y, period, is_arc, len(_nuclei)))


def _tick_lifecycle():
    dead = []
    for i, p in enumerate(_nuclei):
        p[4] += 1
        if p[4] >= p[5]:
            dead.append(i)
    for i in reversed(dead):
        p = _nuclei[i]
        print("[bzp] -source ({},{}) n={}".format(p[0], p[1], len(_nuclei) - 1))
        _nuclei.pop(i)
        _spawn_queue.append([SPAWN_GAP_FRAMES])
    for q in _spawn_queue:
        q[0] -= 1
    ready = [q for q in _spawn_queue if q[0] <= 0]
    for _ in ready:
        _spawn_source()
    for q in ready:
        _spawn_queue.remove(q)


# --------------------------------------------------------------------- #
# Simulation (viper + native, unchanged from bz_new2)
# --------------------------------------------------------------------- #
@micropython.native  # noqa: F821
def _fire_pacemakers(n):
    u = _u; v = _v
    W = _W; H = _H; S = _S; fire = _FIRE
    for p in _nuclei:
        px = p[0]; py = p[1]; period = p[2]; phase = p[3]; is_arc = p[6]
        if (n + phase) % period < fire:
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if is_arc and dy > 0:
                        continue
                    nx = px + dx; ny = py + dy
                    if 0 < nx < W - 1 and 0 < ny < H - 1:
                        idx = ny * W + nx
                        u[idx] = S
                        v[idx] = 0


@micropython.viper  # noqa: F821
def _rd_inner():
    u  = ptr32(_u);  v  = ptr32(_v)   # type: ignore
    nu = ptr32(_nu); nv = ptr32(_nv)  # type: ignore
    W = int(_W); H = int(_H); S = int(_S)
    A = int(_A_INT); B = int(_B_INT); EPS = int(_EPS_INT)
    DU = int(_DU_INT); DT = int(_DT_INT)
    for y in range(1, H - 1):
        yw = y * W
        for x in range(1, W - 1):
            i   = yw + x
            U   = u[i]; V = v[i]
            lap = u[i-1] + u[i+1] + u[i-W] + u[i+W] - (U << 2)
            thr = (V + B) * S // A
            uu  = U * (S - U) // S
            rxn = uu * (U - thr) // EPS
            dif = DU * lap // S
            U2  = U + DT * (rxn + dif) // S
            V2  = V + DT * (U - V) // S
            if U2 < 0:   U2 = 0
            elif U2 > S: U2 = S
            if V2 < 0:   V2 = 0
            elif V2 > S: V2 = S
            nu[i] = U2; nv[i] = V2


@micropython.viper  # noqa: F821
def _boundary_and_copy():
    u  = ptr32(_u);  v  = ptr32(_v)   # type: ignore
    nu = ptr32(_nu); nv = ptr32(_nv)  # type: ignore
    W = int(_W); H = int(_H)
    for x in range(W):
        nu[x]         = nu[W+x];         nv[x]         = nv[W+x]
        nu[(H-1)*W+x] = nu[(H-2)*W+x];   nv[(H-1)*W+x] = nv[(H-2)*W+x]
    for y in range(H):
        yw = y * W
        nu[yw]     = nu[yw+1];     nv[yw]     = nv[yw+1]
        nu[yw+W-1] = nu[yw+W-2];   nv[yw+W-1] = nv[yw+W-2]
    for i in range(W * H):
        u[i] = nu[i]; v[i] = nv[i]


def _sim_step():
    n = _step[0]
    _fire_pacemakers(n)
    _rd_inner()
    _boundary_and_copy()
    _step[0] = n + 1


# --------------------------------------------------------------------- #
# Canvas paint + decay (viper)
# --------------------------------------------------------------------- #
@micropython.viper  # noqa: F821
def _paint_and_decay():
    u  = ptr32(_u);  v  = ptr32(_v)   # type: ignore
    cr = ptr32(_cr); cg = ptr32(_cg); cb = ptr32(_cb)  # type: ignore
    gr = ptr32(_gr); gg = ptr32(_gg); gb = ptr32(_gb)  # type: ignore
    W = int(_W); H = int(_H); S = int(_S)
    decay = int(DECAY)
    for i in range(W * H):
        U = u[i]; V = v[i]
        # phase: 0=quiescent, peaks at wave front (~S), trails into refractory
        phase = (U + (S - U) * V // S) * 255 // S
        if phase < 0:   phase = 0
        if phase > 255: phase = 255
        nr = int(gr[phase])
        ng = int(gg[phase])
        nb = int(gb[phase])
        # decay existing canvas
        r = cr[i] * decay >> 8
        g = cg[i] * decay >> 8
        b = cb[i] * decay >> 8
        # keep whichever is brighter per channel
        if nr > r: r = nr
        if ng > g: g = ng
        if nb > b: b = nb
        cr[i] = r; cg[i] = g; cb[i] = b


# --------------------------------------------------------------------- #
# init / deinit
# --------------------------------------------------------------------- #
def init():
    global _u, _v, _nu, _nv, _nuclei, _spawn_queue
    global _cr, _cg, _cb
    global _EPS_INT, _SPF, _num_sources, _ring_spacing, _lifespan_base
    global _t_last_frame, _t_sim_total, _t_draw_total, _frame_count
    global _W, _H, _WH

    gc.collect()

    # --- display dimensions (loader-injected; fall back to 32x32) ---
    _W  = W if W else 32
    _H  = H if H else 32
    _WH = _W * _H

    # Source count and ring spacing scale with the panel. _ring_spacing is a
    # firing PERIOD in sim steps and the wavefront speed is fixed by the
    # physics, so the same period puts rings the same distance apart in pixels
    # whatever the display size — scaling by the short axis keeps roughly the
    # same number of rings on screen on every board.
    scale = min(_W, _H) / 32.0
    area  = _WH / 1024.0

    m = max(0.0, min(1.0, MOOD))
    _num_sources   = NUM_SOURCES_OVERRIDE    or max(2, int((3 + int(m * 5)) * area))
    _ring_spacing  = RING_SPACING_OVERRIDE   or max(12, int((100 - m * 50) * scale))
    _EPS_INT       = WAVE_SHARPNESS_OVERRIDE or int(50 - m * 25)
    _SPF           = STEPS_PER_FRAME_OVERRIDE or (4 + int(m * 4))
    _lifespan_base = LIFESPAN_OVERRIDE       or int(700 - m * 400)

    _build_gradient()

    _u  = array('i', [0] * _WH)
    _v  = array('i', [0] * _WH)
    _nu = array('i', [0] * _WH)
    _nv = array('i', [0] * _WH)
    _cr = array('i', [0] * _WH)
    _cg = array('i', [0] * _WH)
    _cb = array('i', [0] * _WH)
    _step[0] = 0

    _nuclei      = []
    _spawn_queue = []
    for _ in range(_num_sources):
        _spawn_source()

    _t_last_frame = time.ticks_ms()
    _t_sim_total  = 0
    _t_draw_total = 0
    _frame_count  = 0

    print("[bzp] mood={} sources={} spacing={} eps={} spf={} lifespan={}".format(
        MOOD, _num_sources, _ring_spacing, _EPS_INT, _SPF, _lifespan_base))
    print("[bzp] decay={} mem_free={}".format(DECAY, gc.mem_free()))


def deinit():
    global _u, _v, _nu, _nv, _nuclei, _spawn_queue, _cr, _cg, _cb
    global _gr, _gg, _gb
    _u = None; _v = None; _nu = None; _nv = None
    _cr = None; _cg = None; _cb = None
    _gr = None; _gg = None; _gb = None
    _nuclei = None; _spawn_queue = None


# --------------------------------------------------------------------- #
# Draw
# --------------------------------------------------------------------- #
@micropython.native  # noqa: F821
def draw():
    global _t_last_frame, _t_sim_total, _t_draw_total, _frame_count

    t0 = time.ticks_ms()
    for _ in range(_SPF):
        _sim_step()
    t1 = time.ticks_ms()

    _tick_lifecycle()
    _paint_and_decay()

    gfx = graphics
    cr = _cr; cg = _cg; cb = _cb
    W = _W; H = _H

    for y in range(H):
        yw = y * W
        for x in range(W):
            i = yw + x
            gfx.set_pen(gfx.create_pen(cr[i], cg[i], cb[i]))
            gfx.pixel(x, y)

    t2 = time.ticks_ms()

    _t_sim_total  += time.ticks_diff(t1, t0)
    _t_draw_total += time.ticks_diff(t2, t1)
    _frame_count  += 1

    if _frame_count % _DEBUG_EVERY == 0:
        now      = time.ticks_ms()
        wall_ms  = time.ticks_diff(now, _t_last_frame)
        fps      = _DEBUG_EVERY * 1000 // max(wall_ms, 1)
        avg_sim  = _t_sim_total  // _DEBUG_EVERY
        avg_draw = _t_draw_total // _DEBUG_EVERY
        avg_frm  = wall_ms       // _DEBUG_EVERY
        print("[bzp] frame={} fps={} n={} | sim={}ms draw={}ms total={}ms".format(
            _frame_count, fps, len(_nuclei), avg_sim, avg_draw, avg_frm))
        _t_last_frame = now
        _t_sim_total  = 0
        _t_draw_total = 0
