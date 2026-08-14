# /effects/hyperdrive.py
#
# 3D starfield flown through with continuous banking, curving flight.
# Stars carry ring buffers of 3D positions rendered as gradient-colored
# polylines by lib/wire3d — the trails bending through a turn are what
# make the flight readable.
#
# FLIGHT — three layers, all float, all per-frame (never per-star):
#   intent : summed slow sines per axis (wandering stick input) plus
#            occasional decaying impulses for harder banks.
#   rate   : a second-order spring-damper chases intent — turns overshoot
#            and settle. Damping is applied as damp**step and the
#            integration is scaled by step, so the response is the same
#            at any framerate.
#   bank   : roll intent is -yaw_rate * _BANK_K (coordinated turn), so the
#            field rolls into every turn.
# Hard turns bleed forward speed (_GFORCE_K) and it surges back on
# leveling out. Sine phases are accumulated each frame and wrapped at 2*pi
# — never derived from absolute ticks, which would snap on timer wrap.
#
# TRAILS — each star is a ring of _TRAIL_LEN 3D camera-space positions.
# Only the LIVE head is rotated and advanced each frame; every committed
# point is frozen forever, a snapshot of where that star actually sat in
# camera space at that instant. That is what makes trails curve: as the
# ship banks, successive head positions trace an arc, and the frozen
# history IS that arc.
#
# Rotating the whole ring instead — which this effect originally did — is
# the bug that makes every trail a straight radial line no matter how hard
# you turn. Applying one rotation to all points preserves their relative
# arrangement, so trail shape can only reflect the head's z-advance, which
# is radial by construction. The rotation that should produce curvature
# gets applied to the history too and cancels itself out.
#
# A wall-clock accumulator commits the head into the next ring slot once
# per _COMMIT_MS, so trails span the same flight time at any framerate. Trails are drawn head->tail as wire3d polylines: color is
# arc-length position in a gradient.py LUT (rebaked on palette change —
# one slow frame, accepted), brightness is the head's 1/z depth dim,
# blend is additive so crossing trails bloom.
#
# VOLUME — stars fill a cone _CONE wide per unit z, much wider than the
# screen frustum (~1.33/z at 32px with focal 12). The surplus is the
# reserve the camera banks into: stars are recycled only at the z limits,
# never laterally. Respawns go deep, where 1/z dimming fades them in.
# Respawn stays in Python (viper has no RNG): _advance appends dead star
# indices to _hdr and the caller reseeds exactly those rings.
#
# NEAR-MISS CLUSTERS — occasionally respawns clump toward one lateral
# direction and the intent biases the same way, so the ship curves toward
# the clump and it whips past off-axis.
#
# PERF — the dials are _STAR_COUNT then _TRAIL_LEN; both scale every
# per-frame cost linearly (rotation of count*len points, raster of
# count polylines) and the ring memory (count*len*12 bytes — 69KB at
# 360x16, allocated in init, freed in deinit). Drop both on an RP2040.
# The split timer prints every 60 frames: sim / trail / push ms + lit.
#
# Effect contract: GEOMETRY; graphics, W, H injected; init/draw/deinit.

import math
import random
import time
from array import array
from micropython import const

import gradient
import surface
import trail3d
import wire3d

graphics = None

GEOMETRY = "any"

W = 0    # injected by the loader
H = 0

_COORD_ONE  = const(1024)     # Q10, matches wire3d
_STAR_COUNT = const(360)
_TRAIL_LEN  = const(16)       # must be a power of two (_commit masks)
_FOCAL      = const(12)

# ------------------------------------------------------------------
# Framerate normalization: step = dt / reference frame, clamped
# ------------------------------------------------------------------
_REF_MS   = 33.0
_STEP_MAX = 3.0
_STEP_MIN = 0.1

# ------------------------------------------------------------------
# Flight feel (rates per reference frame)
# ------------------------------------------------------------------
_YAW_AMP   = 0.011
_PITCH_AMP = 0.0075
_ROLL_AMP  = 0.004

_STIFF    = 0.020
_DAMP     = 0.88
_LN_DAMP  = math.log(_DAMP)
_BANK_K   = 1.9

_IMP_DECAY  = 0.06
_IMP_MIN_MS = 2500
_IMP_MAX_MS = 6000

_BASE_SPD = 0.035
_SPD_EASE = 0.04

# Ring-commit interval. A trail spans _TRAIL_LEN * _COMMIT_MS of flight
# time, so this is the trail-length dial and it is FREE: the raster cost
# is set by _TRAIL_LEN (points drawn), not by how much time they cover.
# Raising it too far makes the samples span a wide enough arc that a hard
# bank shows facets rather than a curve.
#
# _TRAIL_LEN sets sample density; _COMMIT_MS sets how much flight time the
# trail covers, and raising it is FREE — the raster cost is set by the
# number of points, not by how far apart they are. Together they are the
# LOOK dial, and the look is the point of this effect.
#
# 16 x 180ms = 2.9s of travel on screen, against roughly 1.2s in the
# original hyperspace3d (16 samples pushed every frame at ~13fps). Longer
# streaks with the same sampling density, at better than twice that
# effect's framerate.
_COMMIT_MS = 180.0

# Stars are split into _COMMIT_GROUPS groups that commit at evenly spaced
# phases through the interval, instead of the whole field retiring a ring
# slot on the same frame. Whatever residual discontinuity survives the
# sliding tail then lands on 1/_COMMIT_GROUPS of the stars at a time and is
# spread across the interval, rather than the entire sky twitching in
# unison — which is the most visible arrangement possible.
# Must be a power of two: group is picked with a mask.
_COMMIT_GROUPS = const(8)
_DEAD_BASE = const(20)   # dead-star indices start here; 0..19 are params


_GFORCE_K   = 6.0
_GFORCE_MIN = 0.6

# Intent sine frequencies, rad/s
_WY1 = 0.37; _WY2 = 0.91
_WP1 = 0.31; _WP2 = 0.73
_WR1 = 0.23

# ------------------------------------------------------------------
# Volume
# ------------------------------------------------------------------
_Z_NEAR     = 0.08
_Z_FAR      = 15.0
_Z_SPAWN_LO = 10.0
_CONE       = 2.5
# The spawn volume used to be a SQUARE-section pyramid while the frustum is
# the panel's aspect. Harmless on a square display and badly wrong on a wide
# one: at 53x11 a square cone starves the sides and wastes most of what it
# seeds above and below, because the vertical view is a fifth of the
# horizontal. _CONE_RATIO is the surplus expressed as a multiple of the view
# half-angle, applied to each axis separately, and _CONE / (16/12) reproduces
# exactly the old value on a 32x32 with focal 12 — so the Cosmic is unchanged.
_CONE_RATIO = 1.875
_CONE_X     = 0.0     # per-axis spawn half-width per unit z; set in init()
_CONE_Y     = 0.0

# Depth dimming: brightness = _DIM_K / z, clamped [_DIM_MIN, 255]
_DIM_K   = 600.0
_DIM_MIN = 18

# ------------------------------------------------------------------
# Near-miss clusters
# ------------------------------------------------------------------
_CLUSTER_MIN_MS = 9000
_CLUSTER_MAX_MS = 16000
_CLUSTER_DUR_MS = 2600
_CLUSTER_STEER  = 0.010
_CLUSTER_FRAC   = 0.6

# ------------------------------------------------------------------
# Palettes: gradient.py stops, head (pos 0) -> tail (pos 255).
# A third of palette mutations use gradient.random_palette() instead.
# ------------------------------------------------------------------
_PALETTES = [
    [(0, 255, 255, 255), (48, 140, 200, 255), (160, 30, 60, 200), (255, 0, 0, 24)],
    [(0, 220, 255, 230), (56, 80, 255, 120), (255, 0, 32, 0)],
    [(0, 255, 240, 255), (64, 255, 90, 255), (255, 28, 0, 44)],
    [(0, 255, 255, 210), (64, 255, 190, 70), (255, 48, 8, 0)],
    [(0, 255, 230, 230), (56, 255, 60, 110), (255, 36, 0, 8)],
]

# Fraction of the ramp, in gradient units, forced down to black at the tail.
#
# Every _COMMIT_MS the ring discards its oldest point, so the trail loses a
# whole 1/_TRAIL_LEN of its length at once. If the tail still has colour at
# that moment the trail visibly blinks shorter — very obvious at
# _TRAIL_LEN 8, where each discard is an eighth of the streak. Ending the
# ramp at true black means the part that gets discarded was already
# invisible. Authored palettes bottom out around brightness 24-48, which is
# dim but not gone, so this is applied to every palette including the
# procedural ones.
# This is done on the BAKED LUT, in RGB, not by appending a black stop to
# the gradient. gradient.py interpolates in HSV and pure black has no
# defined hue (it reports h=0, s=0), so ramping a dark blue tail into a
# black stop sweeps hue 240->360 through magenta while saturation
# collapses — which RAISES red and green. Measured: it turned a monotonic
# 29->2 luma falloff into 29->19->22->31->20->0, a glowing bump right where
# the fade was supposed to be. A linear multiply in RGB cannot do that.
_TAIL_FADE = const(40)   # LUT entries faded, ~16% of the ramp


def _fade_lut_tail(lut):
    """Ramp the last _TAIL_FADE entries of a baked LUT linearly to black."""
    start = 256 - _TAIL_FADE
    for i in range(_TAIL_FADE):
        k = (_TAIL_FADE - 1 - i) * 256 // _TAIL_FADE
        o = (start + i) * 3
        lut[o] = (lut[o] * k) >> 8
        lut[o + 1] = (lut[o + 1] * k) >> 8
        lut[o + 2] = (lut[o + 2] * k) >> 8
    return lut

# ------------------------------------------------------------------
# State. All buffers allocated in init(), freed in deinit().
#
# _rings: int32 Q10 [x,y,z] * _TRAIL_LEN per star, star i's slot j at
#         (i*_TRAIL_LEN + j)*3. heads[i] is the current head slot; ring
#         order ascending from head = newest -> oldest (commit moves the
#         head downward), which is the path order wire3d walks.
# _hdr:   params for the _advance/_commit vipers:
#         0 n, 1 trail_len, 2 speed(Q10), 3 z_near, 4 z_far (Q10),
#         5 dim_k(Q10), 6 dim_min, 7 dead count (out),
#         8..13 cos/sin yaw, pitch, roll (Q14) for the head rotation,
#         16.. dead indices
# ------------------------------------------------------------------
_rings  = None
_tails  = None      # per-star slide origin for the sliding tail
_heads  = None
_bright = None
_hdr    = None
_lut    = None
_w3     = None
_fb     = None      # writable framebuffer view, or None on the pen fallback
_px     = None      # pixel-layer module matching the framebuffer format

_WIDTH  = 32
_HEIGHT = 32

# Flight dynamics
_yaw_rate = 0.0; _pitch_rate = 0.0; _roll_rate = 0.0
_yaw_vel  = 0.0; _pitch_vel  = 0.0; _roll_vel  = 0.0
_imp_yaw  = 0.0; _imp_pitch  = 0.0
_ph = [0.0, 0.0, 0.0, 0.0, 0.0]

_speed_cur  = _BASE_SPD
_speed_tgt  = _BASE_SPD
_commit_acc = 0.0     # ms into the current commit interval
_slide_prev = 0       # last frame's phase, to spot which groups rolled over

_last_ms      = 0
_next_impulse = 0
_warp_until   = 0
_next_mut     = 0
_palette      = 0

_next_cluster  = 0
_cluster_until = 0
_cluster_cx    = 0.0
_cluster_cy    = 0.0
_cluster_spawn = False

# Split timer
_t_last = 0
_t_sim = 0; _t_trail = 0
_fcount = 0
_DEBUG_EVERY = 60


# ------------------------------------------------------------------
# Star management
# ------------------------------------------------------------------
def _reset_star(i):
    z    = random.uniform(_Z_SPAWN_LO, _Z_FAR)
    sx   = z * _CONE_X
    sy   = z * _CONE_Y
    if _cluster_spawn and random.random() < _CLUSTER_FRAC:
        jx = sx * 0.22
        jy = sy * 0.22
        x = _cluster_cx * sx + random.uniform(-jx, jx)
        y = _cluster_cy * sy + random.uniform(-jy, jy)
    else:
        x = random.uniform(-sx, sx)
        y = random.uniform(-sy, sy)
    xi = int(x * _COORD_ONE)
    yi = int(y * _COORD_ONE)
    zi = int(z * _COORD_ONE)
    base = i * _TRAIL_LEN * 3
    # whole ring collapses to the spawn point: draws as a single dim dot
    # until history diverges
    for k in range(_TRAIL_LEN):
        o = base + k * 3
        _rings[o] = xi
        _rings[o + 1] = yi
        _rings[o + 2] = zi
    _heads[i] = 0
    _bright[i] = _DIM_MIN
    t3 = i * 3
    _tails[t3] = xi; _tails[t3 + 1] = yi; _tails[t3 + 2] = zi


def _start_cluster():
    global _cluster_until, _next_cluster, _cluster_cx, _cluster_cy
    now = time.ticks_ms()
    ang = random.uniform(0.0, 6.2832)
    r   = random.uniform(0.45, 0.9)
    _cluster_cx    = math.cos(ang) * r
    _cluster_cy    = math.sin(ang) * r
    _cluster_until = time.ticks_add(now, _CLUSTER_DUR_MS)
    _next_cluster  = time.ticks_add(now, random.randint(_CLUSTER_MIN_MS, _CLUSTER_MAX_MS))
    print("[hyperdrive] cluster ({:.2f},{:.2f})".format(_cluster_cx, _cluster_cy))


def _mutate():
    global _speed_tgt, _warp_until, _next_mut, _palette
    now       = time.ticks_ms()
    _next_mut = time.ticks_add(now, random.randint(6000, 10000))
    choice    = random.randint(0, 2)
    if choice == 0:
        _speed_tgt  = _BASE_SPD * 4.0
        _warp_until = time.ticks_add(now, random.randint(1500, 2200))
        print("[hyperdrive] warp")
    elif choice == 1:
        _speed_tgt  = _BASE_SPD * 0.15
        _warp_until = time.ticks_add(now, random.randint(2000, 3200))
        print("[hyperdrive] drift")
    else:
        roll = random.random()
        if roll < 0.45:
            # Recipes are built for this job: they run head-to-tail and end
            # at true black, so the tail can be retired without blinking.
            stops, desc = gradient.random_recipe()
            _fade_lut_tail(wire3d.bake(stops, _lut))
            print("[hyperdrive] recipe", desc)
        elif roll < 0.70:
            stops, desc = gradient.random_palette()
            # random_palette() closes the loop by repeating its first colour
            # at 255, for effects that cycle the gradient phase. A trail is a
            # ramp, not a cycle: left in, that final climb back toward bright
            # outruns the tail fade and the streak blinks again. Measured:
            # 2 of 40 random palettes were non-monotonic with it, 0 without.
            _fade_lut_tail(wire3d.bake(stops[:-1], _lut))
            print("[hyperdrive] palette", desc)
        else:
            _palette = (_palette + 1) % len(_PALETTES)
            _fade_lut_tail(wire3d.bake(_PALETTES[_palette], _lut))
            print("[hyperdrive] palette", _palette)


# ------------------------------------------------------------------
# Head advance (viper): z -= speed on the head slot only, dead check,
# 1/z depth dim. Dead indices go to _hdr for Python to respawn.
#
# NOTE: a head-position visibility cull was tried here and removed. It is
# unsound for this effect. 62.3% of paths draw nothing, but only 19.4%
# have heads more than 32px off-screen: at spawn depth the projection is
# ~1:1, so the whole cone lands just outside the 32px screen rather than
# far from it. Meanwhile trails of near-camera stars sweep up to 432px as
# the camera rotates, so any margin tight enough to cull usefully also
# drops visible trails at the screen edge (margin 32: 19.4% culled, 0.6%
# of pixels lost). A brightness-adaptive margin is provably safe but culls
# only 1.2%. The sound version of this idea is the bounding-box early-out
# inside build_path, which rejects after projecting and so cannot be wrong.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _advance(rings: ptr32, heads: ptr8, bright: ptr8, hdr: ptr32):
    n = int(hdr[0]); trail = int(hdr[1]); speed = int(hdr[2])
    z_near = int(hdr[3]); z_far = int(hdr[4])
    dim_k = int(hdr[5]); dim_min = int(hdr[6])
    cos_y = int(hdr[8]); sin_y = int(hdr[9])
    cos_p = int(hdr[10]); sin_p = int(hdr[11])
    cos_r = int(hdr[12]); sin_r = int(hdr[13])
    dead = int(0)
    for i in range(n):
        o = (i * trail + int(heads[i])) * 3
        x = int(rings[o]); y = int(rings[o + 1]); z = int(rings[o + 2])

        # Rotate ONLY the live position — see the TRAILS note in the header.
        rx = (x * cos_y - z * sin_y + 8192) >> 14
        rz = (x * sin_y + z * cos_y + 8192) >> 14
        x = rx; z = rz
        ry = (y * cos_p - z * sin_p + 8192) >> 14
        rz = (y * sin_p + z * cos_p + 8192) >> 14
        y = ry; z = rz
        rx = (x * cos_r - y * sin_r + 8192) >> 14
        ry = (x * sin_r + y * cos_r + 8192) >> 14
        x = rx; y = ry

        z -= speed
        if z < z_near or z > z_far:
            hdr[_DEAD_BASE + dead] = i
            dead += 1
            continue
        rings[o] = x; rings[o + 1] = y; rings[o + 2] = z
        br = dim_k // z
        if br > 255: br = 255
        elif br < dim_min: br = dim_min
        bright[i] = br
    hdr[7] = dead


# ------------------------------------------------------------------
# Sliding tail (viper).
#
# Retiring a whole ring slot at once makes the trail lose 1/_TRAIL_LEN of
# itself in a single frame — a visible pop, and fading the tail to black
# does NOT fix it: the gradient is normalised over the path's length, so a
# shorter path drags the black zone one whole segment toward the head and
# a lit chunk turns black instantly.
#
# So the last vertex is clipped continuously instead. The oldest slot is
# treated as scratch and rewritten every frame as a lerp from the real
# oldest point (kept in `tails`) toward the second oldest, by how far this
# commit interval has run. At slide == 1.0 it has arrived exactly where the
# discard will put it, so the commit itself changes nothing on screen.
# ------------------------------------------------------------------
# The ring machinery — sliding tail, staggered commit, the frozen-history
# rule — moved to lib/trail3d.py in Sprint 4. It is not 3D rendering and it
# is not specific to a starfield: boids and an attractor orbit want exactly
# the same thing. What stays here is how the head MOVES, which is the part
# that differs per effect.
#
# trail3d's header indices (0 count, 1 trail, 14 slide, 15 gmask, 16 gstep,
# 17 group) were chosen to match the ones this effect already used, so _hdr
# is shared rather than duplicated: _advance below still reads and writes it
# directly.


# ------------------------------------------------------------------
# init / draw / deinit
# ------------------------------------------------------------------
def init():
    global _rings, _tails, _heads, _bright, _hdr, _lut, _w3, _fb, _px
    global _WIDTH, _HEIGHT
    global _yaw_rate, _pitch_rate, _roll_rate
    global _yaw_vel, _pitch_vel, _roll_vel, _imp_yaw, _imp_pitch
    global _speed_cur, _speed_tgt, _commit_acc, _slide_prev
    global _last_ms, _next_impulse, _warp_until, _next_mut
    global _next_cluster, _cluster_until, _cluster_spawn, _palette
    global _t_last, _t_sim, _t_trail, _fcount

    _WIDTH  = W if W else 32
    _HEIGHT = H if H else 32
    n = _STAR_COUNT
    _rings, _heads, _tails = trail3d.new_rings(n, _TRAIL_LEN)
    _bright = bytearray(n)
    _hdr    = array('i', bytes(4 * (_DEAD_BASE + n)))
    _w3     = wire3d.new_data()

    global _CONE_X, _CONE_Y
    _CONE_X = (_WIDTH * 0.5) / _FOCAL * _CONE_RATIO
    _CONE_Y = (_HEIGHT * 0.5) / _FOCAL * _CONE_RATIO

    wire3d.set_view(_w3, _WIDTH, _HEIGHT, _FOCAL, _Z_NEAR)
    _w3[17] = 0    # additive blend: crossing trails bloom
    _w3[18] = 0
    _w3[21] = 0    # open paths

    # 0, 1, 15, 16 are trail3d's; 3..6 are this effect's own depth params,
    # sharing the array because _advance reads both in one viper pass.
    _hdr[3] = int(_Z_NEAR * _COORD_ONE)
    _hdr[4] = int(_Z_FAR * _COORD_ONE)
    _hdr[5] = int(_DIM_K * _COORD_ONE)
    _hdr[6] = _DIM_MIN
    trail3d.init_header(_hdr, n, _TRAIL_LEN, _COMMIT_GROUPS)

    _fb, _px = surface.acquire(graphics, _WIDTH, _HEIGHT)
    if _fb is None:
        print("[hyperdrive] no writable framebuffer — effect will not render")

    _palette = random.randint(0, len(_PALETTES) - 1)
    if random.random() < 0.5:
        _lut = _fade_lut_tail(wire3d.bake(gradient.random_recipe()[0]))
    else:
        _lut = _fade_lut_tail(wire3d.bake(_PALETTES[_palette]))

    _yaw_rate = _pitch_rate = _roll_rate = 0.0
    _yaw_vel  = _pitch_vel  = _roll_vel  = 0.0
    _imp_yaw  = _imp_pitch  = 0.0
    for i in range(5):
        _ph[i] = random.uniform(0.0, 6.2832)

    _speed_cur  = _BASE_SPD
    _speed_tgt  = _BASE_SPD
    _commit_acc = 0.0
    _slide_prev = 0

    now           = time.ticks_ms()
    _last_ms      = now
    _next_impulse = time.ticks_add(now, random.randint(_IMP_MIN_MS, _IMP_MAX_MS))
    _warp_until   = 0
    _next_mut     = time.ticks_add(now, random.randint(6000, 10000))
    _next_cluster = time.ticks_add(now, random.randint(_CLUSTER_MIN_MS, _CLUSTER_MAX_MS))
    _cluster_until = 0
    _cluster_spawn = False

    # fill the whole depth range so the field starts full, not just far
    for i in range(n):
        z  = random.uniform(_Z_NEAR + 0.5, _Z_FAR)
        xi = int(random.uniform(-1.0, 1.0) * z * _CONE_X * _COORD_ONE)
        yi = int(random.uniform(-1.0, 1.0) * z * _CONE_Y * _COORD_ONE)
        zi = int(z * _COORD_ONE)
        base = i * _TRAIL_LEN * 3
        for k in range(_TRAIL_LEN):
            o = base + k * 3
            _rings[o] = xi
            _rings[o + 1] = yi
            _rings[o + 2] = zi
        _heads[i] = 0
        _bright[i] = _DIM_MIN
        t3 = i * 3
        _tails[t3] = xi; _tails[t3 + 1] = yi; _tails[t3 + 2] = zi

    _t_last = now
    _t_sim = _t_trail = 0
    _fcount = 0

    print("[hyperdrive] init {}x{} stars={} trail={} cone={} zfar={}".format(
        _WIDTH, _HEIGHT, n, _TRAIL_LEN, _CONE, _Z_FAR))


def draw():
    global _yaw_rate, _pitch_rate, _roll_rate
    global _yaw_vel, _pitch_vel, _roll_vel, _imp_yaw, _imp_pitch
    global _speed_cur, _speed_tgt, _commit_acc, _slide_prev
    global _last_ms, _next_impulse, _warp_until
    global _next_cluster, _cluster_until, _cluster_spawn
    global _t_last, _t_sim, _t_trail, _fcount

    if _rings is None:
        return

    now = time.ticks_ms()

    # ---- framerate normalization ----
    dt = time.ticks_diff(now, _last_ms)
    _last_ms = now
    step = dt / _REF_MS
    if step > _STEP_MAX:   step = _STEP_MAX
    elif step < _STEP_MIN: step = _STEP_MIN
    dts = dt * 0.001

    # ---- timers ----
    if _warp_until and time.ticks_diff(_warp_until, now) <= 0:
        _speed_tgt  = _BASE_SPD
        _warp_until = 0
    if time.ticks_diff(_next_mut, now) <= 0:
        _mutate()
    if time.ticks_diff(_next_impulse, now) <= 0:
        # yaw impulses twice as common as pitch: banks read better than dives
        if random.random() < 0.67:
            _imp_yaw += random.uniform(-0.022, 0.022)
        else:
            _imp_pitch += random.uniform(-0.016, 0.016)
        _next_impulse = time.ticks_add(now, random.randint(_IMP_MIN_MS, _IMP_MAX_MS))
    if time.ticks_diff(_next_cluster, now) <= 0:
        _start_cluster()
    cluster_on = _cluster_until and time.ticks_diff(_cluster_until, now) > 0
    _cluster_spawn = cluster_on

    # ---- INTENT: accumulated sine phases + impulses + cluster bias ----
    ph = _ph
    ph[0] += _WY1 * dts
    ph[1] += _WY2 * dts
    ph[2] += _WP1 * dts
    ph[3] += _WP2 * dts
    ph[4] += _WR1 * dts
    for i in range(5):
        if ph[i] > 6.283185:
            ph[i] -= 6.283185

    yaw_intent   = _YAW_AMP   * (math.sin(ph[0]) + 0.5 * math.sin(ph[1])) + _imp_yaw
    pitch_intent = _PITCH_AMP * (math.sin(ph[2]) + 0.5 * math.sin(ph[3])) + _imp_pitch
    if cluster_on:
        yaw_intent   += _CLUSTER_STEER * _cluster_cx
        pitch_intent += _CLUSTER_STEER * _cluster_cy

    decay = _IMP_DECAY * step
    if decay > 0.9: decay = 0.9
    _imp_yaw   *= (1.0 - decay)
    _imp_pitch *= (1.0 - decay)

    # ---- RATE: spring-damper, framerate-normalized ----
    dampf = math.exp(_LN_DAMP * step)
    stiff = _STIFF * step
    _yaw_vel    = _yaw_vel * dampf + (yaw_intent - _yaw_rate) * stiff
    _yaw_rate  += _yaw_vel * step
    _pitch_vel  = _pitch_vel * dampf + (pitch_intent - _pitch_rate) * stiff
    _pitch_rate += _pitch_vel * step

    # ---- BANK: coordinated turn ----
    roll_intent = -_yaw_rate * _BANK_K + _ROLL_AMP * math.sin(ph[4])
    _roll_vel   = _roll_vel * dampf + (roll_intent - _roll_rate) * stiff
    _roll_rate += _roll_vel * step

    # ---- speed: eased target, g-force bleed in hard turns ----
    _speed_cur += (_speed_tgt - _speed_cur) * (_SPD_EASE * step)
    gload = (_yaw_rate if _yaw_rate >= 0 else -_yaw_rate) + \
            (_pitch_rate if _pitch_rate >= 0 else -_pitch_rate)
    speed_factor = 1.0 - _GFORCE_K * gload
    if speed_factor < _GFORCE_MIN: speed_factor = _GFORCE_MIN
    speed = _speed_cur * step * speed_factor

    t0 = time.ticks_ms()

    # ---- advance the live heads (rotate + z), recycle the dead ----
    # History is never touched again after commit, which is what curves the
    # trails; it also means only _STAR_COUNT points rotate per frame rather
    # than _STAR_COUNT * _TRAIL_LEN.
    w3 = _w3
    wire3d.set_rotation(w3, _yaw_rate * step, _pitch_rate * step, _roll_rate * step)
    hdr = _hdr
    hdr[8] = w3[0]; hdr[9] = w3[1]
    hdr[10] = w3[2]; hdr[11] = w3[3]
    hdr[12] = w3[4]; hdr[13] = w3[5]
    hdr[14] = int(_commit_acc * 256.0 / _COMMIT_MS) & 255

    spd_fx = int(speed * _COORD_ONE)
    _hdr[2] = spd_fx if spd_fx > 0 else 1
    trail3d.slide_tail(_rings, _heads, _tails, _hdr)
    _advance(_rings, _heads, _bright, _hdr)
    for k in range(_hdr[7]):
        _reset_star(_hdr[_DEAD_BASE + k])

    # ---- commit, one group at a time, as the phase clock passes each ----
    _commit_acc += dt
    if _commit_acc >= _COMMIT_MS:
        _commit_acc -= _COMMIT_MS * int(_commit_acc / _COMMIT_MS)
    slide = int(_commit_acc * 256.0 / _COMMIT_MS) & 255
    adv = (slide - _slide_prev) & 255
    if adv:
        gstep = 256 // _COMMIT_GROUPS
        for gi in range(_COMMIT_GROUPS):
            if ((gi * gstep - _slide_prev - 1) & 255) < adv:
                _hdr[17] = gi
                trail3d.commit(_rings, _heads, _tails, _hdr)
    _slide_prev = slide

    t1 = time.ticks_ms()

    # ---- raster all trails straight into the framebuffer ----
    surface.clear(graphics)
    w3[9] = _TRAIL_LEN
    build = wire3d.build_path
    draw = _px.draw_path
    fb = _fb; rings = _rings; lut = _lut
    heads = _heads; bright = _bright
    for i in range(_STAR_COUNT):
        w3[16] = bright[i]
        w3[20] = heads[i]
        w3[25] = i * _TRAIL_LEN
        build(rings, w3)
        # 62% of paths emit nothing (the bbox early-out rejects them). A
        # viper call costs 18us measured, so testing the count in Python
        # first is ~0.5us to skip 18us on the majority of stars.
        if w3[26]:
            draw(fb, lut, w3)

    t2 = time.ticks_ms()

    _t_sim   += time.ticks_diff(t1, t0)
    _t_trail += time.ticks_diff(t2, t1)
    _fcount  += 1

    if _fcount % _DEBUG_EVERY == 0:
        wall = time.ticks_diff(now, _t_last)
        fps  = _DEBUG_EVERY * 1000 // max(wall, 1)
        print("[hyperdrive] fps={} | sim={}ms trail={}ms".format(
            fps,
            _t_sim   // _DEBUG_EVERY,
            _t_trail // _DEBUG_EVERY))
        _t_last = now
        _t_sim = _t_trail = 0


def deinit():
    global _rings, _tails, _heads, _bright, _hdr, _lut, _w3, _fb, _px
    _rings  = None
    _tails  = None
    _heads  = None
    _bright = None
    _hdr    = None
    _lut   = None
    _w3    = None
    _fb    = None
    _px    = None
