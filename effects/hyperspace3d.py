# /effects/hyperspace3d_new.py
#
# ===================================================================== #
#  HYPERSPACE 3D  —  natural-flight starfield
# ===================================================================== #
#
# --------------------------------------------------------------------- #
#  PERFORMANCE — the star transform runs in fixed-point viper.
#
#  Profiled at 9.9 fps with the transform in float Python: sim 51ms, trail
#  24ms, push 20ms per frame, plus 1.6 KB/frame of garbage. The transform was
#  half the frame because star coordinates lived in Python lists of floats, so
#  every read and write boxed a float object.
#
#  Coordinates are now one interleaved int32 array in Q10 fixed point, and the
#  per-star rotate / advance / project / dim loop is a single viper call.
#  Viper cannot do floating point at all, which is why fixed point is not an
#  optimisation choice here but the entry fee.
#
#  Scale choice: coords Q10 (1024), trig Q14 (16384). The binding constraint is
#  that a coordinate-times-trig product must stay inside int32 — the widest
#  case is a star at the far plane's cone edge, x = _CONE * _Z_FAR, and two
#  such products are summed per axis. Q10/Q14 leaves ~40% headroom; Q10/Q16
#  overflows. Respawn still happens in Python, since viper has no RNG: the
#  viper pass records dead star indices and the caller recycles just those.
#
#  If it still needs to be faster, reduce _STAR_COUNT first, then _TRAIL_LEN. Both visibly
#  cost content, so they come after code.
#
#  SEPARATE ISSUE — the 3D does not read on screen. There is no horizon, no
#  ground plane, no oriented reference of any kind, and a rotationally
#  symmetric point cloud makes ROLL essentially imperceptible. All the flight
#  modelling is invisible for want of something to bank relative to.
#
#  Trails are 2D: a ring buffer of already-projected screen coords, drawn as
#  Bresenham segments between consecutive history points. Positions and the
#  perspective divide are genuinely 3D; the trails approximate the projected
#  curve piecewise-linearly.
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
#   * _STAR_COUNT (star count) is THE perf dial — every star pays a ~6-multiply
#     rotation every frame whether on-screen or not (that's what lets it
#     swing back into view). On the RP2040 this dominates `sim=`. Watch the
#     split timer and drop _STAR_COUNT if the slow board is starved.
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

GEOMETRY = "any"

W = 0    # injected by the loader
H = 0

_WIDTH    = 32
_HEIGHT    = 32
_PIXEL_COUNT = 1024

# Fixed-point scales for the viper transform (see header for why these two).
_COORD_SHIFT = const(10)          # coords: Q10
_TRIG_SHIFT = const(14)          # trig:   Q14
_COORD_ONE = const(1024)
_TRIG_ONE = const(16384)
_STAR_COUNT   = const(360)   # star count — THE perf dial. Wider cone => fewer of these
                     # land on-screen, so bumped to keep the field full. Drop on
                     # RP2040 if the split timer shows sim= dominating.
_TRAIL_LEN   = const(16)    # trail ring-buffer depth per star

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
# Star coords, interleaved [x0,y0,z0, x1,y1,z1, ...] in Q10 fixed point.
# One array rather than three so the viper transform stays inside its 4-argument
# limit and each star's three values sit adjacent in memory.
_coords = array('i', bytes(4 * 3 * _STAR_COUNT))

# Scratch passed to the viper transform: parameters in, dead-star list out.
#   0..5  cy sy cp sp cr sr   (Q14)
#   6     speed               (Q10)
#   7..8  z_near z_far        (Q10)
#   9     focal
#   10    dim_k               (Q10 numerator)
#   11    dim_min
#   12    coord_off
#   13..14 screen centre x, y
#   15    star count
#   16    dead count          (out)
#   17..  dead star indices   (out)
_PARAM_HEADER = const(20)
_params = array('i', bytes(4 * (_PARAM_HEADER + _STAR_COUNT)))

_trail_x  = array('H', b'\xff\xff' * (_STAR_COUNT * _TRAIL_LEN))
_trail_y  = array('H', b'\xff\xff' * (_STAR_COUNT * _TRAIL_LEN))
_trail_head = bytearray(_STAR_COUNT)
_screen_xy   = array('H', b'\xff\xff' * (_STAR_COUNT * 2))
_star_brightness = bytearray(_STAR_COUNT)

_red_buf = bytearray(_PIXEL_COUNT)
_green_buf = bytearray(_PIXEL_COUNT)
_blue_buf = bytearray(_PIXEL_COUNT)
_palette_buf = bytearray(9)

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
        _palette_buf[i] = p[i]


# ------------------------------------------------------------------
# Star management — respawn AHEAD in the frustum cone
# ------------------------------------------------------------------
def _reset_star(i):
    z      = random.uniform(_Z_SPAWN_LO, _Z_FAR)
    spread = z * _CONE
    if _cluster_spawn and random.random() < _CLUSTER_FRAC:
        # clump around the cluster's lateral direction
        j  = spread * 0.22
        x = _cluster_cx * spread + random.uniform(-j, j)
        y = _cluster_cy * spread + random.uniform(-j, j)
    else:
        x = random.uniform(-spread, spread)
        y = random.uniform(-spread, spread)
    b = i * 3
    _coords[b]     = int(x * _COORD_ONE)
    _coords[b + 1] = int(y * _COORD_ONE)
    _coords[b + 2] = int(z * _COORD_ONE)
    base = i * _TRAIL_LEN
    for k in range(_TRAIL_LEN):
        _trail_x[base + k] = 65535
        _trail_y[base + k] = 65535
    _trail_head[i] = 0
    _star_brightness[i] = _DIM_MIN


def _scatter():
    for i in range(_STAR_COUNT):
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
# Star transform (viper, fixed point)
#
# Per star: advance z, apply yaw/pitch/roll, clip, project, depth-dim.
# Stars that fall outside the z range are not respawned here — viper has no
# access to random() — their indices are appended to the dead list in params
# for the caller to recycle.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _transform(coords: ptr32, screen_xy: ptr16, brightness: ptr8, params: ptr32):
    cos_yaw = int(params[0]);  sin_yaw = int(params[1])
    cos_pitch = int(params[2]);  sin_pitch = int(params[3])
    cos_roll = int(params[4]);  sin_roll = int(params[5])
    speed = int(params[6])
    z_near = int(params[7]); z_far = int(params[8])
    focal_len = int(params[9])
    dim_scale  = int(params[10]); dim_floor = int(params[11])
    coord_offset   = int(params[12])
    centre_x   = int(params[13]); centre_y = int(params[14])
    star_count    = int(params[15])

    trig_shift   = int(14)      # trig shift
    round_half = int(8192)    # 1 << (trig_shift - 1), for round-to-nearest
    dead_count = int(0)

    for i in range(star_count):
        base = i * 3
        x = int(coords[base]); y = int(coords[base + 1]); z = int(coords[base + 2])
        z -= speed

        # yaw (x,z), pitch (y,z), roll (x,y) — each a 2x2 rotation, Q14 trig.
        #
        # `+ round_half` before every shift is not cosmetic. A signed >> floors toward
        # -infinity, so each shift loses up to a full LSB on negative values and
        # none on positive ones. That is six biased shifts per star per frame,
        # which walks coordinates steadily negative and drags z toward the
        # camera — stars pile up close, where they are big and bright, and the
        # lit-pixel count (and so the push cost) climbs. Rounding to nearest
        # makes the error symmetric and it cancels instead of accumulating.
        rot_x = (x * cos_yaw - z * sin_yaw + round_half) >> trig_shift
        rot_z = (x * sin_yaw + z * cos_yaw + round_half) >> trig_shift
        x = rot_x; z = rot_z
        rot_y = (y * cos_pitch - z * sin_pitch + round_half) >> trig_shift
        rot_z = (y * sin_pitch + z * cos_pitch + round_half) >> trig_shift
        y = rot_y; z = rot_z
        rot_x = (x * cos_roll - y * sin_roll + round_half) >> trig_shift
        rot_y = (x * sin_roll + y * cos_roll + round_half) >> trig_shift
        x = rot_x; y = rot_y

        coords[base] = x; coords[base + 1] = y; coords[base + 2] = z

        if z < z_near or z > z_far:
            # Out of the volume — mark coord_offset-screen and queue for respawn.
            screen_xy[i * 2]     = 65535
            screen_xy[i * 2 + 1] = 65535
            params[17 + dead_count]  = i
            dead_count += 1
            continue

        # perspective divide: screen_px = focal_len * x / z, both coords in Q10 so the
        # scale cancels and the result is already in pixels
        screen_px = (focal_len * x) // z + centre_x
        screen_py = (focal_len * y) // z + centre_y

        # depth dimming: brightness = dim_k / z
        level = dim_scale // z
        if level > 255: level = 255
        elif level < dim_floor: level = dim_floor
        brightness[i] = level

        screen_xy[i * 2]     = screen_px + coord_offset
        screen_xy[i * 2 + 1] = screen_py + coord_offset

    params[16] = dead_count


# ------------------------------------------------------------------
# Clear accumulation buffers (viper)
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def _clear_bufs(rb: ptr8, gb: ptr8, bb: ptr8):
    for i in range(int(_PIXEL_COUNT)):
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
def _render_trails(red: ptr8, green: ptr8, blue: ptr8,
                   screen_xy: ptr16, trail_x: ptr16, trail_y: ptr16, trail_head: ptr8,
                   palette: ptr8, star_brightness: ptr8):
    trail_len   = int(_TRAIL_LEN)
    star_count   = int(_STAR_COUNT)
    width    = int(_WIDTH)
    height    = int(_HEIGHT)
    trail_mid  = int(8)
    tail_to_mid_span   = int(7)
    mid_to_head_span   = int(7)
    coord_offset  = int(2000)
    sentinel = int(65535)
    max_segment_px = int(96)        # segment-length guard

    tail_r = int(palette[0]); tail_g = int(palette[1]); tail_b = int(palette[2])
    mid_r = int(palette[3]); mid_g = int(palette[4]); mid_b = int(palette[5])
    head_r = int(palette[6]); head_g = int(palette[7]); head_b = int(palette[8])

    for i in range(star_count):
        # depth-dim the three gradient stops once per star
        bi  = int(star_brightness[i])
        tr2 = (tail_r * bi) >> 8; tg2 = (tail_g * bi) >> 8; tb2 = (tail_b * bi) >> 8
        mr2 = (mid_r * bi) >> 8; mg2 = (mid_g * bi) >> 8; mb2 = (mid_b * bi) >> 8
        hr2 = (head_r * bi) >> 8; hg2 = (head_g * bi) >> 8; hb2 = (head_b * bi) >> 8

        # push current projected position into ring buffer
        hd = int(trail_head[i])
        trail_x[i * trail_len + hd] = int(screen_xy[i * 2])
        trail_y[i * trail_len + hd] = int(screen_xy[i * 2 + 1])
        hd = (hd + 1) % trail_len
        trail_head[i] = hd

        has_p0 = int(0)
        px0    = int(0)
        py0    = int(0)

        for age in range(trail_len):
            slot  = (hd + age) % trail_len
            raw_x = int(trail_x[i * trail_len + slot])
            raw_y = int(trail_y[i * trail_len + slot])

            if raw_x == sentinel or raw_y == sentinel:
                has_p0 = int(0)
                continue

            spx = raw_x - coord_offset
            spy = raw_y - coord_offset

            if age < trail_mid:
                gp = age * 255 // tail_to_mid_span
                ng = 255 - gp
                rv = (tr2 * ng + mr2 * gp) >> 8
                gv = (tg2 * ng + mg2 * gp) >> 8
                bv = (tb2 * ng + mb2 * gp) >> 8
            else:
                gp = (age - trail_mid) * 255 // mid_to_head_span
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
                if steps <= max_segment_px:
                    fx   = px0 << 16
                    fy   = py0 << 16
                    xinc = (dx << 16) // steps
                    yinc = (dy << 16) // steps
                    for s in range(steps + 1):
                        lx = fx >> 16
                        ly = fy >> 16
                        if lx >= 0 and lx < width and ly >= 0 and ly < height:
                            idx = ly * width + lx
                            v = int(red[idx]) + rv; red[idx] = v if v < 255 else 255
                            v = int(green[idx]) + gv; green[idx] = v if v < 255 else 255
                            v = int(blue[idx]) + bv; blue[idx] = v if v < 255 else 255
                        fx += xinc
                        fy += yinc
            elif spx >= 0 and spx < width and spy >= 0 and spy < height:
                idx = spy * width + spx
                v = int(red[idx]) + rv; red[idx] = v if v < 255 else 255
                v = int(green[idx]) + gv; green[idx] = v if v < 255 else 255
                v = int(blue[idx]) + bv; blue[idx] = v if v < 255 else 255

            px0    = spx
            py0    = spy
            has_p0 = int(1)


# ------------------------------------------------------------------
# Push accumulation buffers to display (native)
# ------------------------------------------------------------------
@micropython.native  # noqa: F821
def _push_display():
    gfx = graphics
    red  = _red_buf; green = _green_buf; blue = _blue_buf
    gfx.set_pen(gfx.create_pen(0, 0, 0))
    gfx.clear()
    width = _WIDTH; height = _HEIGHT
    for y in range(height):
        row = y * width
        for x in range(width):
            idx = row + x
            r = int(red[idx]); g = int(green[idx]); b = int(blue[idx])
            if r | g | b:
                gfx.set_pen(gfx.create_pen(r, g, b))
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
    global _WIDTH, _HEIGHT, _PIXEL_COUNT, _red_buf, _green_buf, _blue_buf

    _WIDTH    = W if W else 32
    _HEIGHT    = H if H else 32
    _PIXEL_COUNT = _WIDTH * _HEIGHT
    _red_buf = bytearray(_PIXEL_COUNT)
    _green_buf = bytearray(_PIXEL_COUNT)
    _blue_buf = bytearray(_PIXEL_COUNT)

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
    for i in range(_STAR_COUNT):
        z      = random.uniform(_Z_NEAR + 0.5, _Z_FAR)
        spread = z * _CONE
        b = i * 3
        _coords[b]     = int(random.uniform(-spread, spread) * _COORD_ONE)
        _coords[b + 1] = int(random.uniform(-spread, spread) * _COORD_ONE)
        _coords[b + 2] = int(z * _COORD_ONE)
        base = i * _TRAIL_LEN
        for j in range(_TRAIL_LEN):
            _trail_x[base + j] = 65535
            _trail_y[base + j] = 65535
        _trail_head[i] = 0
        _star_brightness[i] = _DIM_MIN

    for j in range(_STAR_COUNT * 2):
        _screen_xy[j] = 65535

    _t_last = now
    _t_sim = _t_trail = _t_push = 0
    _fcount = 0

    print("[hs3d] init {}x{} stars={} trail={} cone={} zfar={}".format(
        _WIDTH, _HEIGHT, _STAR_COUNT, _TRAIL_LEN, _CONE, _Z_FAR))


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
    seconds = (now & 0x3FFFFF) * 0.001
    yaw_intent   = _YAW_AMP   * (math.sin(seconds * 0.37) + 0.5 * math.sin(seconds * 0.91 + 1.7)) + _imp_yaw
    pitch_intent = _PITCH_AMP * (math.sin(seconds * 0.31 + 0.5) + 0.5 * math.sin(seconds * 0.73)) + _imp_pitch
    if cluster_on:
        yaw_intent   += _CLUSTER_STEER * _cluster_cx
        pitch_intent += _CLUSTER_STEER * _cluster_cy

    decay = _IMP_DECAY * step
    if decay > 0.9: decay = 0.9
    _imp_yaw   *= (1.0 - decay)
    _imp_pitch *= (1.0 - decay)

    # ---- RATE: second-order spring-damper (mass) ----
    _yaw_vel    = _yaw_vel   * _DAMP + (yaw_intent   - _yaw_rate)   * _STIFF
    _yaw_rate  += _yaw_vel
    _pitch_vel  = _pitch_vel * _DAMP + (pitch_intent - _pitch_rate) * _STIFF
    _pitch_rate += _pitch_vel

    # ---- BANK: coordinated turn ----
    roll_intent = -_yaw_rate * _BANK_K + _ROLL_AMP * math.sin(seconds * 0.23)
    _roll_vel   = _roll_vel * _DAMP + (roll_intent - _roll_rate) * _STIFF
    _roll_rate += _roll_vel

    # ---- speed easing + g-force bleed in hard turns ----
    _speed_cur += (_speed_tgt - _speed_cur) * (_SPD_EASE * step)
    gload = (_yaw_rate if _yaw_rate >= 0 else -_yaw_rate) + \
            (_pitch_rate if _pitch_rate >= 0 else -_pitch_rate)
    speed_factor = 1.0 - _GFORCE_K * gload
    if speed_factor < _GFORCE_MIN: speed_factor = _GFORCE_MIN
    speed = _speed_cur * step * speed_factor

    # ---- per-frame angles (real-time scaled) ----
    yaw_angle = _yaw_rate   * step
    pitch_angle = _pitch_rate * step
    roll_angle = _roll_rate  * step
    cos_yaw = math.cos(yaw_angle); sin_yaw = math.sin(yaw_angle)
    cos_pitch = math.cos(pitch_angle); sin_pitch = math.sin(pitch_angle)
    cos_roll = math.cos(roll_angle); sin_roll = math.sin(roll_angle)

    t0 = time.ticks_ms()

    # ---- pack this frame's parameters for the viper transform ----
    # The flight model above stays in float: it is a few dozen operations per
    # frame, not per star, so it is not worth the precision loss.
    params = _params
    params[0]  = int(cos_yaw * _TRIG_ONE); params[1] = int(sin_yaw * _TRIG_ONE)
    params[2]  = int(cos_pitch * _TRIG_ONE); params[3] = int(sin_pitch * _TRIG_ONE)
    params[4]  = int(cos_roll * _TRIG_ONE); params[5] = int(sin_roll * _TRIG_ONE)
    params[6]  = int(speed * _COORD_ONE)
    params[7]  = int(_Z_NEAR * _COORD_ONE)
    params[8]  = int(_Z_FAR  * _COORD_ONE)
    params[9]  = _FOCAL
    params[10] = int(_DIM_K * _COORD_ONE)
    params[11] = _DIM_MIN
    params[12] = _COORD_OFF
    params[13] = _WIDTH >> 1
    params[14] = _HEIGHT >> 1
    params[15] = _STAR_COUNT

    _transform(_coords, _screen_xy, _star_brightness, params)

    # ---- recycle the stars the transform retired ----
    # No lateral despawn: off-screen stars are the periphery reserve we bank
    # into. The trail raster clips per pixel and x,y stay bounded by the cone.
    dead_count = params[16]
    for k in range(dead_count):
        _reset_star(params[17 + k])

    t1 = time.ticks_ms()

    _clear_bufs(_red_buf, _green_buf, _blue_buf)
    _render_trails(_red_buf, _green_buf, _blue_buf,
                   _screen_xy, _trail_x, _trail_y, _trail_head,
                   _palette_buf, _star_brightness)
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
    global _red_buf, _green_buf, _blue_buf
    _red_buf = None
    _green_buf = None
    _blue_buf = None
