# /effects/hyperspace3d_new.py
#
# ===================================================================== #
#  HYPERSPACE 3D  —  natural-flight starfield
# ===================================================================== #
#
# --------------------------------------------------------------------- #
#  TODO — TOO SLOW (10 fps measured on Cosmic, 2026-08-13)
#
#  Supersedes hyperspace3d.py (archived): same 3D camera, but flight intent
#  comes from summed sines at irrational frequency ratios rather than a
#  maneuver state machine. Keep the flight model; it is the good part.
#
#  Fix the frame rate IN THIS ORDER — do not start by cutting content:
#
#    1. CODE OPTIMIZATION FIRST. Profile before touching anything. Check
#       per-frame allocation before assuming the cost is arithmetic — glitch
#       and bz_waves both looked CPU-bound and were actually GC-bound, where
#       one allocating line in a hot loop cost 12x the frame time. Measure
#       min/median/p95/max, not a mean; a stuttering effect can show a
#       perfectly healthy average.
#    2. THEN reduce star count (_NS, currently 144).
#    3. THEN reduce trail length (_TL, currently 16 deep).
#
#  Steps 2 and 3 visibly cost content, so they are the last resort, not the
#  first move.
#
#  SEPARATE ISSUE — the 3D does not read on screen. There is no horizon, no
#  ground plane, no oriented reference of any kind, and a rotationally
#  symmetric point cloud makes ROLL essentially imperceptible. All the flight
#  modelling is invisible for want of something to bank relative to. A horizon
#  line, faint ground grid, or directionally asymmetric trails would do more
#  for the effect than any amount of speed.
#
#  Trails are 2D: a ring buffer of already-projected screen coords, drawn as
#  Bresenham segments between consecutive history points. Positions and the
#  perspective divide are genuinely 3D; the trails approximate the projected
#  curve piecewise-linearly. Fine except under fast rotation or close passes.
# --------------------------------------------------------------------- #
#
# A 3D starfield you fly THROUGH like a jet or spaceship — continuous,
# organic banking and weaving, not a procedural "execute maneuver #4"
# state machine. Stars stream past with motion-blur trails.
#
# --------------------------------------------------------------------- #
#  HOW THE FLIGHT FEELS NATURAL
# --------------------------------------------------------------------- #
#   1. INTENT — smooth summed sines at irrational frequency ratios (the
#      ship is always gently working the stick, never repeats) + occasional
#      decaying impulses for harder banks. No named maneuvers.
#   2. RATE   — a SECOND-ORDER spring-damper chases intent, slightly
#      under-damped so turns overshoot and settle: that's the ship's MASS.
#   3. BANK   — roll is derived LIVE from yaw rate (coordinated turn); the
#      whole field rolling into a turn is the strongest "I'm flying" cue.
#   + forward speed eases (never steps); warp/drift surges ramp smoothly.
#
# --------------------------------------------------------------------- #
#  SPACE, SPAWN & DESPAWN  (the volume model — "wide forward field")
# --------------------------------------------------------------------- #
# The problem with a narrow forward cone: when you bank, the screen sweeps
# into space the cone never filled, and you see black. The fix is to fill a
# WIDE forward region — much wider than the screen — so the periphery you
# turn INTO is always already populated.
#
# Stars are rotated by the camera every frame (they swing in/out of view as
# you bank). They are recycled ONLY when they leave the depth range:
#   * z < _Z_NEAR — whipped past the camera, or
#   * z > _Z_FAR  — drifted out the back.
# There is NO lateral despawn: stars that drift to the sides are KEPT as the
# reserve you bank into. Their x,y stay bounded because spawn x,y scale with
# z (a cone), so projected coords never run far off screen anyway.
#
# Recycled stars RESPAWN deep (near _Z_FAR) with x,y across the WIDE cone
# (_CONE ≈ 2.5 vs the screen's ~1.33), so ~2x the screen is stocked at every
# depth. Stars stream inward keeping their x,y, so the whole cone — every
# depth, well past the screen edges — stays uniformly full. Gentle weaving
# flight never reveals void; only a near-180° flip would (and we never do).
#
# DEPTH DIMMING: each star's brightness scales ~1/z, so distant stars are
# faint and blaze as they rush past. Doubles as spawn-pop concealment —
# stars fade in from the distance instead of popping in.
#
# G-FORCE: hard turns bleed forward speed; leveling out lets it surge back.
#
# NEAR-MISS CLUSTERS: occasionally a clump of stars spawns off to one side
# and the flight intent biases toward it — you bank around/through a faked
# obstacle, then it whooshes past.
#
# --------------------------------------------------------------------- #
#  RUNS ON RP2040 *OR* RP2350 — ONE CODEBASE, NO BRANCHING
# --------------------------------------------------------------------- #
#   * Flight math is plain float: hardware-fast on RP2350 (FPU), software
#     but lean on RP2040 (M0+, no FPU). Trig once/frame, never per star.
#   * Per-pixel hot loops (clear, trail raster) are integer @viper.
#   * MOTION IS WALL-CLOCK NORMALIZED (`step` = dt / 30fps reference) so it
#     flies at the same real-world speed on the faster and slower boards.
#
# --------------------------------------------------------------------- #
#  PERF NOTES / DIALS
# --------------------------------------------------------------------- #
#   * _NS (star count) is THE perf dial — every star pays a ~6-multiply
#     rotation every frame whether on-screen or not (that's what lets it
#     swing back into view). On the RP2040 this dominates `sim=`. Watch the
#     split timer and drop _NS if the slow board is starved.
#   * Trail raster: fixed-point DDA (divides per SEGMENT) + a segment-length
#     guard so a fast spin can't grind through a huge off-screen line.
#
# --------------------------------------------------------------------- #
#  POSSIBLE FUTURE IMPROVEMENTS
# --------------------------------------------------------------------- #
#   * gradient.py LUT palettes (procedural HSV, rebuilt per palette change).
#   * Fixed-point viper rotation loop for RP2040 headroom (~10.6 scale to
#     respect the M0+ 32-bit multiply); measure with the split timer first.
#   * Star size by proximity (draw near stars 2x2).
#   * Parallax dust layer (a second, slower, dimmer field).
#   * Per-star colour instead of one global palette.
#
#  Effect contract: graphics=None, cu=None, init(), draw(), deinit()
# ===================================================================== #

import math
import random
import time
from array import array
from micropython import const

graphics = None
cu       = None

_W    = const(32)
_H    = const(32)
_NPIX = const(1024)
_NS   = const(360)   # star count — THE perf dial. Wider cone => fewer of these
                     # land on-screen, so bumped to keep the field full. Drop on
                     # RP2040 if the split timer shows sim= dominating.
_TL   = const(16)    # trail ring-buffer depth per star

_FOCAL = const(12)

# Trail coord encoding: stored = screen_coord + _COORD_OFF, sentinel 65535.
_COORD_OFF = 2000

# ------------------------------------------------------------------
# Framerate normalization
# ------------------------------------------------------------------
_REF_MS   = 33.0
_STEP_MAX = 3.0
_STEP_MIN = 0.1

# ------------------------------------------------------------------
# Flight feel  (rates per 30fps-reference-frame)
# ------------------------------------------------------------------
_YAW_AMP   = 0.011
_PITCH_AMP = 0.0075
_ROLL_AMP  = 0.004

_STIFF  = 0.020
_DAMP   = 0.88
_BANK_K = 1.9

_IMP_DECAY  = 0.06
_IMP_MIN_MS = 2500
_IMP_MAX_MS = 6000

_BASE_SPD = 0.035
_SPD_EASE = 0.04

# ------------------------------------------------------------------
# Spawn / despawn volume
# ------------------------------------------------------------------
_Z_NEAR     = 0.08    # near clip — recycle when a star whips past the camera
_Z_FAR      = 15.0    # far clip  — deeper tunnel; respawn at this plane
_Z_SPAWN_LO = 10.0    # respawn z range is [_Z_SPAWN_LO, _Z_FAR] (stream in deep)
_CONE       = 2.5     # WIDE lateral spread per unit z. Screen frustum is only
                      # ~1.33; 2.5 stocks ~2x the screen at every depth so the
                      # periphery you bank into is already full. No lateral cull.

# ------------------------------------------------------------------
# Depth dimming:  brightness = _DIM_K / z, clamped to [_DIM_MIN, 255]
# ------------------------------------------------------------------
_DIM_K   = 600.0   # tuned for the deeper far plane: ~40 at z=15, full by z~2.4
_DIM_MIN = 18

# ------------------------------------------------------------------
# G-force speed coupling:  speed *= clamp(1 - _GFORCE_K*turn, _GFORCE_MIN, 1)
# ------------------------------------------------------------------
_GFORCE_K   = 6.0
_GFORCE_MIN = 0.6

# ------------------------------------------------------------------
# Near-miss clusters
# ------------------------------------------------------------------
_CLUSTER_MIN_MS = 9000
_CLUSTER_MAX_MS = 16000
_CLUSTER_DUR_MS = 2600
_CLUSTER_STEER  = 0.010   # intent bias toward the cluster (flip sign to veer away)
_CLUSTER_FRAC   = 0.6     # fraction of respawns that clump during a cluster

# ------------------------------------------------------------------
# Palettes: (tail_r,g,b, mid_r,g,b, head_r,g,b)
# ------------------------------------------------------------------
_PALETTES = [
    (  0,  0, 18,   20,  80, 200,  200, 230, 255),  # Deep space blue
    (  0, 15,  0,   20, 150,  40,   80, 255, 120),  # Matrix green
    ( 20,  0, 20,  150,   0, 180,  255,  80, 255),  # Nebula purple
    ( 30, 10,  0,  200,  80,   0,  255, 200,  80),  # Warp amber
    (  5,  0,  0,  180,  10,  40,  255,  40, 100),  # Deep red
]

# ------------------------------------------------------------------
# Module state
# ------------------------------------------------------------------
_x = [0.0] * _NS
_y = [0.0] * _NS
_z = [0.0] * _NS

_trail_x  = array('H', b'\xff\xff' * (_NS * _TL))
_trail_y  = array('H', b'\xff\xff' * (_NS * _TL))
_trail_hd = bytearray(_NS)
_px_cur   = array('H', b'\xff\xff' * (_NS * 2))
_star_bri = bytearray(_NS)

_rbuf = bytearray(_NPIX)
_gbuf = bytearray(_NPIX)
_bbuf = bytearray(_NPIX)
_pal_buf = bytearray(9)

# Flight dynamics
_yaw_rate = 0.0; _pitch_rate = 0.0; _roll_rate = 0.0
_yaw_vel  = 0.0; _pitch_vel  = 0.0; _roll_vel  = 0.0
_imp_yaw  = 0.0; _imp_pitch  = 0.0

_speed_cur = _BASE_SPD
_speed_tgt = _BASE_SPD

_last_ms      = 0
_next_impulse = 0
_warp_until   = 0
_next_mut     = 0
_palette      = 0

# Cluster state
_next_cluster  = 0
_cluster_until = 0
_cluster_cx    = 0.0
_cluster_cy    = 0.0
_cluster_spawn = False

# Debug timing
_t_last  = 0
_t_sim   = 0
_t_trail = 0
_t_push  = 0
_fcount  = 0
_DEBUG_EVERY = 30


# ------------------------------------------------------------------
# Palette
# ------------------------------------------------------------------
def _set_palette(idx):
    global _palette
    _palette = idx % len(_PALETTES)
    p = _PALETTES[_palette]
    for i in range(9):
        _pal_buf[i] = p[i]


# ------------------------------------------------------------------
# Star management — respawn AHEAD in the frustum cone
# ------------------------------------------------------------------
def _reset_star(i):
    z      = random.uniform(_Z_SPAWN_LO, _Z_FAR)
    spread = z * _CONE
    if _cluster_spawn and random.random() < _CLUSTER_FRAC:
        # clump around the cluster's lateral direction
        j  = spread * 0.22
        _x[i] = _cluster_cx * spread + random.uniform(-j, j)
        _y[i] = _cluster_cy * spread + random.uniform(-j, j)
    else:
        _x[i] = random.uniform(-spread, spread)
        _y[i] = random.uniform(-spread, spread)
    _z[i] = z
    base = i * _TL
    for k in range(_TL):
        _trail_x[base + k] = 65535
        _trail_y[base + k] = 65535
    _trail_hd[i] = 0
    _star_bri[i] = _DIM_MIN


def _scatter():
    for i in range(_NS):
        _reset_star(i)


# ------------------------------------------------------------------
# Near-miss cluster
# ------------------------------------------------------------------
def _start_cluster():
    global _cluster_until, _next_cluster, _cluster_cx, _cluster_cy
    now = time.ticks_ms()
    ang = random.uniform(0.0, 6.2832)
    r   = random.uniform(0.45, 0.9)
    _cluster_cx    = math.cos(ang) * r
    _cluster_cy    = math.sin(ang) * r
    _cluster_until = time.ticks_add(now, _CLUSTER_DUR_MS)
    _next_cluster  = time.ticks_add(now, random.randint(_CLUSTER_MIN_MS, _CLUSTER_MAX_MS))
    print("[hs3dnew] CLUSTER ({:.2f},{:.2f})".format(_cluster_cx, _cluster_cy))


# ------------------------------------------------------------------
# Mutation — rare speed / colour / scatter events (smoothly eased)
# ------------------------------------------------------------------
def _mutate():
    global _speed_tgt, _warp_until, _next_mut
    now       = time.ticks_ms()
    _next_mut = time.ticks_add(now, random.randint(6000, 10000))
    choice    = random.randint(0, 3)
    if choice == 0:
        _speed_tgt  = _BASE_SPD * 4.0
        _warp_until = time.ticks_add(now, random.randint(1500, 2200))
        print("[hs3dnew] WARP BURST")
    elif choice == 1:
        _speed_tgt  = _BASE_SPD * 0.15
        _warp_until = time.ticks_add(now, random.randint(2000, 3200))
        print("[hs3dnew] DRIFT")
    elif choice == 2:
        _scatter()
        print("[hs3dnew] SCATTER")
    else:
        _set_palette(_palette + 1)
        print("[hs3dnew] COLOR ->", _palette)


# ------------------------------------------------------------------
# Clear accumulation buffers (viper)
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _clear_bufs(rb: ptr8, gb: ptr8, bb: ptr8):
    for i in range(1024):
        rb[i] = 0
        gb[i] = 0
        bb[i] = 0


# ------------------------------------------------------------------
# Trail render (viper) — fixed-point DDA, additive blend, depth-dimmed.
#
# Each star's three gradient stops (tail/mid/head) are pre-scaled ONCE by
# the star's depth brightness, then the per-pixel age-lerp runs on the
# dimmed stops. Segment-length guard skips pathologically long lines.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _render_trails(rb: ptr8, gb: ptr8, bb: ptr8,
                   pxc: ptr16, txb: ptr16, tyb: ptr16, thb: ptr8,
                   pal: ptr8, sbri: ptr8):
    TL   = int(_TL)
    NS   = int(_NS)
    W    = int(_W)
    TLh  = int(8)
    d1   = int(7)
    d2   = int(7)
    OFF  = int(2000)
    SENT = int(65535)
    SMAX = int(96)        # segment-length guard

    tr = int(pal[0]); tg = int(pal[1]); tb = int(pal[2])
    mr = int(pal[3]); mg = int(pal[4]); mb = int(pal[5])
    hr = int(pal[6]); hg = int(pal[7]); hb = int(pal[8])

    for i in range(NS):
        # depth-dim the three gradient stops once per star
        bi  = int(sbri[i])
        tr2 = (tr * bi) >> 8; tg2 = (tg * bi) >> 8; tb2 = (tb * bi) >> 8
        mr2 = (mr * bi) >> 8; mg2 = (mg * bi) >> 8; mb2 = (mb * bi) >> 8
        hr2 = (hr * bi) >> 8; hg2 = (hg * bi) >> 8; hb2 = (hb * bi) >> 8

        # push current projected position into ring buffer
        hd = int(thb[i])
        txb[i * TL + hd] = int(pxc[i * 2])
        tyb[i * TL + hd] = int(pxc[i * 2 + 1])
        hd = (hd + 1) % TL
        thb[i] = hd

        has_p0 = int(0)
        px0    = int(0)
        py0    = int(0)

        for age in range(TL):
            slot  = (hd + age) % TL
            raw_x = int(txb[i * TL + slot])
            raw_y = int(tyb[i * TL + slot])

            if raw_x == SENT or raw_y == SENT:
                has_p0 = int(0)
                continue

            spx = raw_x - OFF
            spy = raw_y - OFF

            if age < TLh:
                gp = age * 255 // d1
                ng = 255 - gp
                rv = (tr2 * ng + mr2 * gp) >> 8
                gv = (tg2 * ng + mg2 * gp) >> 8
                bv = (tb2 * ng + mb2 * gp) >> 8
            else:
                gp = (age - TLh) * 255 // d2
                ng = 255 - gp
                rv = (mr2 * ng + hr2 * gp) >> 8
                gv = (mg2 * ng + hg2 * gp) >> 8
                bv = (mb2 * ng + hb2 * gp) >> 8

            if has_p0:
                dx  = spx - px0
                dy  = spy - py0
                adx = dx if dx >= 0 else -dx
                ady = dy if dy >= 0 else -dy
                steps = adx if adx > ady else ady
                if steps == 0:
                    steps = int(1)
                if steps <= SMAX:
                    fx   = px0 << 16
                    fy   = py0 << 16
                    xinc = (dx << 16) // steps
                    yinc = (dy << 16) // steps
                    for s in range(steps + 1):
                        lx = fx >> 16
                        ly = fy >> 16
                        if lx >= 0 and lx < W and ly >= 0 and ly < W:
                            idx = ly * W + lx
                            v = int(rb[idx]) + rv; rb[idx] = v if v < 255 else 255
                            v = int(gb[idx]) + gv; gb[idx] = v if v < 255 else 255
                            v = int(bb[idx]) + bv; bb[idx] = v if v < 255 else 255
                        fx += xinc
                        fy += yinc
            elif spx >= 0 and spx < W and spy >= 0 and spy < W:
                idx = spy * W + spx
                v = int(rb[idx]) + rv; rb[idx] = v if v < 255 else 255
                v = int(gb[idx]) + gv; gb[idx] = v if v < 255 else 255
                v = int(bb[idx]) + bv; bb[idx] = v if v < 255 else 255

            px0    = spx
            py0    = spy
            has_p0 = int(1)


# ------------------------------------------------------------------
# Push accumulation buffers to display (native)
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _push_display():
    gfx = graphics
    rb  = _rbuf; gb = _gbuf; bb = _bbuf
    gfx.set_pen(gfx.create_pen(0, 0, 0))
    gfx.clear()
    for y in range(32):
        row = y * 32
        for x in range(32):
            idx = row + x
            rv = int(rb[idx]); gv = int(gb[idx]); bv = int(bb[idx])
            if rv | gv | bv:
                gfx.set_pen(gfx.create_pen(rv, gv, bv))
                gfx.pixel(x, y)


# ------------------------------------------------------------------
# init / draw / deinit
# ------------------------------------------------------------------
def init():
    global _yaw_rate, _pitch_rate, _roll_rate
    global _yaw_vel, _pitch_vel, _roll_vel, _imp_yaw, _imp_pitch
    global _speed_cur, _speed_tgt, _last_ms, _next_impulse, _warp_until, _next_mut
    global _next_cluster, _cluster_until, _cluster_spawn
    global _t_last, _t_sim, _t_trail, _t_push, _fcount

    _yaw_rate = _pitch_rate = _roll_rate = 0.0
    _yaw_vel  = _pitch_vel  = _roll_vel  = 0.0
    _imp_yaw  = _imp_pitch  = 0.0

    _speed_cur = _BASE_SPD
    _speed_tgt = _BASE_SPD

    now           = time.ticks_ms()
    _last_ms      = now
    _next_impulse = time.ticks_add(now, random.randint(_IMP_MIN_MS, _IMP_MAX_MS))
    _warp_until   = 0
    _next_mut     = time.ticks_add(now, random.randint(6000, 10000))
    _next_cluster = time.ticks_add(now, random.randint(_CLUSTER_MIN_MS, _CLUSTER_MAX_MS))
    _cluster_until = 0
    _cluster_spawn = False

    _set_palette(random.randint(0, len(_PALETTES) - 1))

    # fill the whole depth so the field starts full, not just far
    for i in range(_NS):
        z      = random.uniform(_Z_NEAR + 0.5, _Z_FAR)
        spread = z * _CONE
        _x[i] = random.uniform(-spread, spread)
        _y[i] = random.uniform(-spread, spread)
        _z[i] = z
        base = i * _TL
        for j in range(_TL):
            _trail_x[base + j] = 65535
            _trail_y[base + j] = 65535
        _trail_hd[i] = 0
        _star_bri[i] = _DIM_MIN

    for j in range(_NS * 2):
        _px_cur[j] = 65535

    _t_last = now
    _t_sim = _t_trail = _t_push = 0
    _fcount = 0

    print("[hs3dnew] init stars={} trail={} cone={} zfar={} (wide-field)".format(
        _NS, _TL, _CONE, _Z_FAR))


@micropython.native  # noqa: F821
def draw():
    global _yaw_rate, _pitch_rate, _roll_rate
    global _yaw_vel, _pitch_vel, _roll_vel, _imp_yaw, _imp_pitch
    global _speed_cur, _speed_tgt, _last_ms, _next_impulse, _warp_until
    global _next_cluster, _cluster_until, _cluster_spawn
    global _t_last, _t_sim, _t_trail, _t_push, _fcount

    now = time.ticks_ms()

    # ---- framerate normalization ----
    dt   = time.ticks_diff(now, _last_ms)
    _last_ms = now
    step = dt / _REF_MS
    if step > _STEP_MAX:   step = _STEP_MAX
    elif step < _STEP_MIN: step = _STEP_MIN

    # ---- timers ----
    if _warp_until and time.ticks_diff(_warp_until, now) <= 0:
        _speed_tgt  = _BASE_SPD
        _warp_until = 0
    if time.ticks_diff(_next_mut, now) <= 0:
        _mutate()
    if time.ticks_diff(_next_impulse, now) <= 0:
        if random.randint(0, 2):
            _imp_yaw += random.uniform(-0.022, 0.022)
        else:
            _imp_pitch += random.uniform(-0.016, 0.016)
        _next_impulse = time.ticks_add(now, random.randint(_IMP_MIN_MS, _IMP_MAX_MS))
    if time.ticks_diff(_next_cluster, now) <= 0:
        _start_cluster()
    cluster_on = _cluster_until and time.ticks_diff(_cluster_until, now) > 0
    _cluster_spawn = cluster_on

    # ---- INTENT: smooth wander + impulses + cluster bias ----
    ts = (now & 0x3FFFFF) * 0.001
    yaw_intent   = _YAW_AMP   * (math.sin(ts * 0.37) + 0.5 * math.sin(ts * 0.91 + 1.7)) + _imp_yaw
    pitch_intent = _PITCH_AMP * (math.sin(ts * 0.31 + 0.5) + 0.5 * math.sin(ts * 0.73)) + _imp_pitch
    if cluster_on:
        yaw_intent   += _CLUSTER_STEER * _cluster_cx
        pitch_intent += _CLUSTER_STEER * _cluster_cy

    dk = _IMP_DECAY * step
    if dk > 0.9: dk = 0.9
    _imp_yaw   *= (1.0 - dk)
    _imp_pitch *= (1.0 - dk)

    # ---- RATE: second-order spring-damper (mass) ----
    _yaw_vel    = _yaw_vel   * _DAMP + (yaw_intent   - _yaw_rate)   * _STIFF
    _yaw_rate  += _yaw_vel
    _pitch_vel  = _pitch_vel * _DAMP + (pitch_intent - _pitch_rate) * _STIFF
    _pitch_rate += _pitch_vel

    # ---- BANK: coordinated turn ----
    roll_intent = -_yaw_rate * _BANK_K + _ROLL_AMP * math.sin(ts * 0.23)
    _roll_vel   = _roll_vel * _DAMP + (roll_intent - _roll_rate) * _STIFF
    _roll_rate += _roll_vel

    # ---- speed easing + g-force bleed in hard turns ----
    _speed_cur += (_speed_tgt - _speed_cur) * (_SPD_EASE * step)
    gload = (_yaw_rate if _yaw_rate >= 0 else -_yaw_rate) + \
            (_pitch_rate if _pitch_rate >= 0 else -_pitch_rate)
    sfac = 1.0 - _GFORCE_K * gload
    if sfac < _GFORCE_MIN: sfac = _GFORCE_MIN
    speed = _speed_cur * step * sfac

    # ---- per-frame angles (real-time scaled) ----
    ay = _yaw_rate   * step
    ap = _pitch_rate * step
    ar = _roll_rate  * step
    cy = math.cos(ay); sy = math.sin(ay)
    cp = math.cos(ap); sp = math.sin(ap)
    cr = math.cos(ar); sr = math.sin(ar)

    off   = _COORD_OFF
    dimk  = _DIM_K; dmin = _DIM_MIN
    znear = _Z_NEAR; zfar = _Z_FAR
    t0 = time.ticks_ms()

    for i in range(_NS):
        x = _x[i]; y = _y[i]; z = _z[i]
        z -= speed

        x, z = x * cy - z * sy, x * sy + z * cy   # yaw
        y, z = y * cp - z * sp, y * sp + z * cp   # pitch
        x, y = x * cr - y * sr, x * sr + y * cr   # roll

        _x[i] = x; _y[i] = y; _z[i] = z

        # ---- despawn: behind / too far / flung off-axis ----
        if z < znear or z > zfar:
            _reset_star(i)
            _px_cur[i * 2]     = 65535
            _px_cur[i * 2 + 1] = 65535
            continue

        iz = _FOCAL / z
        px = int(x * iz + 16)
        py = int(y * iz + 16)
        # no lateral despawn — off-screen stars are the periphery reserve we
        # bank into. The trail raster clips per pixel; x,y stay bounded (~cone).

        # ---- depth brightness ~ 1/z ----
        bri = dimk / z
        if bri > 255.0: bri = 255.0
        ib = int(bri)
        if ib < dmin: ib = dmin
        _star_bri[i] = ib

        _px_cur[i * 2]     = px + off
        _px_cur[i * 2 + 1] = py + off

    t1 = time.ticks_ms()

    _clear_bufs(_rbuf, _gbuf, _bbuf)
    _render_trails(_rbuf, _gbuf, _bbuf,
                   _px_cur, _trail_x, _trail_y, _trail_hd,
                   _pal_buf, _star_bri)
    t2 = time.ticks_ms()

    _push_display()
    t3 = time.ticks_ms()

    _t_sim   += time.ticks_diff(t1, t0)
    _t_trail += time.ticks_diff(t2, t1)
    _t_push  += time.ticks_diff(t3, t2)
    _fcount  += 1

    if _fcount % _DEBUG_EVERY == 0:
        wall = time.ticks_diff(now, _t_last)
        fps  = _DEBUG_EVERY * 1000 // max(wall, 1)
        print("[hs3dnew] fps={} | sim={}ms trail={}ms push={}ms".format(
            fps,
            _t_sim   // _DEBUG_EVERY,
            _t_trail // _DEBUG_EVERY,
            _t_push  // _DEBUG_EVERY))
        _t_last  = now
        _t_sim = _t_trail = _t_push = 0


def deinit():
    global _rbuf, _gbuf, _bbuf
    _rbuf = None
    _gbuf = None
    _bbuf = None
