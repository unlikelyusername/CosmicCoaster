# /effects/bz_ripple.py
#
# BZ ripple — an off-screen Belousov-Zhabotinsky (Barkley excitable-medium)
# sim drives the SHAPE; color comes from TIME; trails come from a fading canvas.
#
# Three independent jobs:
#   shape   — the BZ sim emits expanding circular waves that interfere.
#   color   — one pen color per frame, swept through a gradient by g_pos.
#   history — a persistent RGB canvas that dims toward black every frame.
#
# The BZ sim is never drawn to screen. Each frame we threshold it to a binary
# "is this a wavefront?" mask and stamp the current pen color onto the canvas
# wherever the mask is set. Because each expanding ring is painted at a
# different gradient position than the ring before it, a single wavetrain
# shows a gradient sweep across its rings.
#
# EVERY RUN IS DIFFERENT. init() seeds the RNG and rolls the physics regime,
# source layout, motion, and a procedurally generated color palette — all
# within curated bands so it's novel but never broken. On top of that the sim
# MUTATES as it runs (regime drift, wave-breaking events, drifting sources) so
# it keeps developing instead of settling into steady rings.
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
#  CONFIG
# ================================================================== #

RESTLESS = 0.55    # master dial: 0 = calm/wide/slow, 1 = dense/chaotic/fast.
                   # Biases every rolled range below.

SEED     = None    # None = fresh random run each boot. Set an int to reproduce
                   # a run you liked (the seed is printed every run).

SCHEME   = None    # Color scheme. None = random. Or pin one:
                   # "analogous" "complementary" "triadic" "split" "tetradic"

GRAD_OVERRIDE = None   # Set to a fixed [(pos,r,g,b),...] to pin the palette
                       # and ignore SCHEME.

# ================================================================== #

import gc
import time
import random
from array import array
from micropython import const

import gradient

GEOMETRY = "any"

graphics = None
W = 0    # injected by the loader
H = 0

# Display dimensions as module globals rather than const(). The viper functions
# below already load them via `int(_W)`, so they work unchanged with runtime
# values — the only cost is a global load instead of a folded constant.
# init() sets all three from the injected W/H.
_W   = 32
_H   = 32
_WH  = 1024
_S   = const(1000)

# Barkley physics — fixed parts (fixed-point, scale S=1000)
_A_INT  = const(750)
_DU_INT = const(500)
_DT_INT = const(50)
_FIRE   = const(3)        # source fires for this many steps each period
_PACE_MARGIN = const(3)

# Barkley physics — MUTABLE parts (drift while running)
_B   = 50
_EPS = 40
# drift bands (set in init around the rolled base)
_eps_lo = 34; _eps_hi = 46
_b_lo   = 38; _b_hi   = 62

# Rolled-per-run parameters (set in init)
_ring_spacing    = 60
_num_sources     = 4
_lifespan_base   = 500
_steps_per_frame = 6
_dim             = 240
_step_val        = 3
_step_sign       = 1
_threshold       = 500
_fire_radius     = 0       # 0 -> 3x3 source, 1 -> 5x5 source
_event_chance    = 0.04    # per-frame chance of a disturbance event
_src_drift       = 0.5     # source wander tendency
_seed            = 0

# Sim state
_u  = None; _v  = None
_nu = None; _nv = None

# Output canvas (persistent, fades)
_cr = None; _cg = None; _cb = None

# Active gradient (generated in init) + pen position
GRAD   = None
_g_pos = 0
_step  = [0]

# Pacemakers: [x, y, period, phase, age, lifespan]
_nuclei = None

# Debug timing
_t_last_frame = 0
_t_sim_total  = 0
_t_draw_total = 0
_frame_count  = 0
_DEBUG_EVERY  = 30


# --------------------------------------------------------------------- #
# Pacemakers
# --------------------------------------------------------------------- #
def _margins():
    """Inset bounds for source placement, clamped so a short panel still works.

    _PACE_MARGIN assumes room to spare. On an 11px-tall Galactic a fixed margin
    of 3 leaves almost nothing, and on anything shorter the range would invert
    and randint() would raise.
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
    sp     = _ring_spacing
    jitter = max(4, sp // 8)
    period = sp + random.randint(-jitter, jitter)
    phase  = random.randint(0, period - 1)
    life   = _lifespan_base + random.randint(-_lifespan_base // 4, _lifespan_base // 4)
    _nuclei.append([x, y, period, phase, 0, life])


def _tick_lifecycle():
    dead = []
    for i, p in enumerate(_nuclei):
        p[4] += 1
        if p[4] >= p[5]:
            dead.append(i)
    for i in reversed(dead):
        _nuclei.pop(i)
        _spawn_source()


@micropython.native  # noqa: F821
def _fire_pacemakers(n):
    u = _u; v = _v
    W = _W; H = _H; S = _S; fire = _FIRE
    rad = _fire_radius + 1            # 1 -> 3x3, 2 -> 5x5
    for p in _nuclei:
        px = p[0]; py = p[1]; period = p[2]; phase = p[3]
        if (n + phase) % period < fire:
            for dy in range(-rad, rad + 1):
                for dx in range(-rad, rad + 1):
                    nx = px + dx; ny = py + dy
                    if 0 < nx < W - 1 and 0 < ny < H - 1:
                        idx = ny * W + nx
                        u[idx] = S
                        v[idx] = 0


# --------------------------------------------------------------------- #
# Mutation — keeps the sim developing instead of settling.
#   * regime drift: random-walk EPS and B within their bands
#   * source drift: nudge source positions
#   * disturbance events: break a wavefront (-> spiral) or spark a new wave
# --------------------------------------------------------------------- #
def _disturbance():
    # Radius is clamped to what the panel can hold — a 5x5 block needs 2 cells
    # of clearance on every side, which an 11px-tall display cannot give.
    if random.randint(0, 1):
        # refractory block — punches a hole that breaks passing wavefronts
        # into free ends, which curl into rotating spirals.
        r = min(2, (_W - 1) // 2, (_H - 1) // 2)
        vv = _S * 3 // 4
        cx = random.randint(r, _W - 1 - r); cy = random.randint(r, _H - 1 - r)
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                idx = (cy + dy) * _W + (cx + dx)
                _u[idx] = 0; _v[idx] = vv
    else:
        # spark — a transient stimulation that launches a fresh wave
        r = min(1, (_W - 1) // 2, (_H - 1) // 2)
        cx = random.randint(r, _W - 1 - r); cy = random.randint(r, _H - 1 - r)
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                idx = (cy + dy) * _W + (cx + dx)
                _u[idx] = _S; _v[idx] = 0


def _mutate():
    global _EPS, _B
    # regime random-walk (kept inside curated bands so it stays alive)
    _EPS += random.randint(-1, 1)
    if _EPS < _eps_lo:   _EPS = _eps_lo
    elif _EPS > _eps_hi: _EPS = _eps_hi
    _B += random.randint(-1, 1)
    if _B < _b_lo:   _B = _b_lo
    elif _B > _b_hi: _B = _b_hi

    # source drift
    x_lo, y_lo, x_hi, y_hi = _margins()
    drift_p = _src_drift * 0.15
    for p in _nuclei:
        if random.random() < drift_p:
            p[0] += random.randint(-1, 1)
            p[1] += random.randint(-1, 1)
            if p[0] < x_lo:   p[0] = x_lo
            elif p[0] > x_hi: p[0] = x_hi
            if p[1] < y_lo:   p[1] = y_lo
            elif p[1] > y_hi: p[1] = y_hi

    # disturbance events
    if random.random() < _event_chance:
        _disturbance()


# --------------------------------------------------------------------- #
# Reaction-diffusion step (Barkley) — viper
# --------------------------------------------------------------------- #
@micropython.viper  # noqa: F821
def _rd_inner():
    u  = ptr32(_u);  v  = ptr32(_v)   # type: ignore
    nu = ptr32(_nu); nv = ptr32(_nv)  # type: ignore
    W = int(_W); H = int(_H); S = int(_S)
    A = int(_A_INT); B = int(_B); EPS = int(_EPS)
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
# Paint + dim — viper. Dims the canvas, then stamps the pen color onto
# every pixel whose u is over the threshold.
# --------------------------------------------------------------------- #
@micropython.viper  # noqa: F821
def _paint_and_dim(pr: int, pg: int, pb: int):
    u  = ptr32(_u)                                       # type: ignore
    cr = ptr32(_cr); cg = ptr32(_cg); cb = ptr32(_cb)   # type: ignore
    n = int(_WH); dim = int(_dim); thr = int(_threshold)
    for i in range(n):
        r = cr[i] * dim >> 8
        g = cg[i] * dim >> 8
        b = cb[i] * dim >> 8
        if u[i] > thr:
            r = pr; g = pg; b = pb
        cr[i] = r; cg[i] = g; cb[i] = b


# --------------------------------------------------------------------- #
# init / deinit
# --------------------------------------------------------------------- #
def init():
    global _u, _v, _nu, _nv, _cr, _cg, _cb, _nuclei, _g_pos, GRAD
    global _B, _EPS, _eps_lo, _eps_hi, _b_lo, _b_hi
    global _ring_spacing, _num_sources, _lifespan_base, _steps_per_frame
    global _dim, _step_val, _step_sign, _threshold, _fire_radius
    global _event_chance, _src_drift, _seed
    global _t_last_frame, _t_sim_total, _t_draw_total, _frame_count
    global _W, _H, _WH

    gc.collect()

    # --- display dimensions (loader-injected; fall back to 32x32) ---
    _W  = W if W else 32
    _H  = H if H else 32
    _WH = _W * _H

    # --- seed (reproducible if SEED set, else fresh every boot) ---
    _seed = SEED if SEED is not None else (time.ticks_us() & 0x7fffffff)
    random.seed(_seed)
    R = max(0.0, min(1.0, RESTLESS))

    # --- physics regime + drift bands ---
    eps_base = int(54 - R * 24) + random.randint(-4, 4)
    if eps_base < 24: eps_base = 24
    elif eps_base > 60: eps_base = 60
    _EPS = eps_base
    _eps_lo = eps_base - 6; _eps_hi = eps_base + 6
    b_base = 50 + random.randint(-15, 15)
    _B = b_base
    _b_lo = b_base - 12; _b_hi = b_base + 12

    # --- sources + size ---
    # Both scale with the panel. _ring_spacing is a firing PERIOD in sim steps,
    # and the wavefront speed is fixed by the physics, so the same period puts
    # rings the same distance apart in pixels regardless of display size — on a
    # smaller panel that means fewer visible rings. Scaling by the short axis
    # keeps roughly the same number of rings on screen on every board.
    # Source count scales with area for the same reason.
    _scale = min(_W, _H) / 32.0
    _area  = (_W * _H) / 1024.0

    _num_sources   = 2 + int(R * 4 * _area) + random.randint(0, 1)
    if _num_sources < 2: _num_sources = 2

    _ring_spacing  = int((80 - R * 40) * _scale) + random.randint(-8, 8)
    _min_spacing   = max(12, int(28 * _scale))
    if _ring_spacing < _min_spacing: _ring_spacing = _min_spacing
    _lifespan_base = random.randint(300, 700)
    _fire_radius   = random.randint(0, 1)                         # point vs fat
    _threshold     = random.randint(420, 600)                     # ring thickness

    # --- motion ---
    _steps_per_frame = 4 + int(R * 4) + random.randint(0, 1)      # 4..9
    _dim             = random.randint(234, 248)                   # trail length
    _step_val        = random.randint(2, 6)                       # color speed
    _step_sign       = 1 if random.randint(0, 1) else -1          # cycle direction

    # --- mutation intensity ---
    _event_chance = 0.01 + R * 0.06
    _src_drift    = R

    # --- palette ---
    if GRAD_OVERRIDE is not None:
        GRAD = GRAD_OVERRIDE; pal_desc = "override"
    else:
        GRAD, pal_desc = gradient.random_palette(SCHEME)

    # --- arrays ---
    _u  = array('i', [0] * _WH)
    _v  = array('i', [0] * _WH)
    _nu = array('i', [0] * _WH)
    _nv = array('i', [0] * _WH)
    _cr = array('i', [0] * _WH)
    _cg = array('i', [0] * _WH)
    _cb = array('i', [0] * _WH)
    _step[0] = 0
    _g_pos   = 0

    _nuclei = []
    for _ in range(_num_sources):
        _spawn_source()

    _t_last_frame = time.ticks_ms()
    _t_sim_total  = 0
    _t_draw_total = 0
    _frame_count  = 0

    print("[bzr] SEED={} restless={} palette={}".format(_seed, RESTLESS, pal_desc))
    print("[bzr] eps={} b={} sources={} spacing={} thr={} fire_r={}".format(
        _EPS, _B, _num_sources, _ring_spacing, _threshold, _fire_radius))
    print("[bzr] spf={} dim={} step={}{} event={} mem={}".format(
        _steps_per_frame, _dim, "+" if _step_sign > 0 else "-", _step_val,
        _event_chance, gc.mem_free()))


def deinit():
    global _u, _v, _nu, _nv, _cr, _cg, _cb, _nuclei
    _u = None; _v = None; _nu = None; _nv = None
    _cr = None; _cg = None; _cb = None
    _nuclei = None


# --------------------------------------------------------------------- #
# Draw
# --------------------------------------------------------------------- #
@micropython.native  # noqa: F821
def draw():
    global _g_pos
    global _t_last_frame, _t_sim_total, _t_draw_total, _frame_count

    t0 = time.ticks_ms()
    for _ in range(_steps_per_frame):
        _sim_step()
    t1 = time.ticks_ms()

    _tick_lifecycle()
    _mutate()

    # one pen color for the whole frame, swept through the gradient
    _g_pos = (_g_pos + _step_val * _step_sign) % 256
    pr, pg, pb = gradient.sample(GRAD, _g_pos)

    # dim history + stamp the wavefronts in the current pen color
    _paint_and_dim(pr, pg, pb)

    # blit canvas -> screen
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
        print("[bzr] frame={} fps={} n={} eps={} b={} g_pos={} | sim={}ms draw={}ms total={}ms".format(
            _frame_count, fps, len(_nuclei), _EPS, _B, _g_pos, avg_sim, avg_draw, avg_frm))
        _t_last_frame = now
        _t_sim_total  = 0
        _t_draw_total = 0
