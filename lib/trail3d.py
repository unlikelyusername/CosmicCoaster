# /lib/trail3d.py
#
# Motion history for 3D points: ring buffers, commit cadence, sliding
# tails, staggered groups. Everything an effect needs to leave a trail
# behind a moving thing, and nothing about how that thing moves.
#
# WHY THIS IS NOT PART OF wire3d. wire3d's job is: 3D points in,
# gradient-coloured screen segments out. It knows about projection,
# clipping and colour. A ring buffer is time-series storage — commit
# cadence, sliding tails, staggered retirement — and shares no vocabulary
# with any of that. Folding it in would quietly turn "3D wireframe
# library" into "3D wireframe library that also has opinions about
# animation", and the next effect that wants trails with a different
# retirement policy would have to fight it.
#
# The seam is already half-cut in the right place: build_path() takes
# data[20], a ring start index, so it can walk a path that wraps in a
# buffer. That is a RENDERING affordance — one modulo in the walk — and
# belongs where it is. What does not belong there is any knowledge of why
# the buffer wraps.
#
# WHAT THIS OWNS
#   * ring allocation and layout (N entities x L points x 3 coords)
#   * the wall-clock commit cadence
#   * the sliding tail, so retiring a slot costs nothing visually
#   * staggered commit groups, so the field never twitches in unison
#   * the frozen-history rule: committed points are never transformed again
#
# WHAT THIS DOES NOT OWN, because it differs per effect and pretending
# otherwise is how a library grows a starfield-shaped hole:
#   * how the head moves — hyperdrive advances z and rotates by the camera,
#     boids steer by flocking, an attractor integrates Lorenz
#   * culling, respawn, depth dimming, colour
#
# THE FROZEN-HISTORY RULE, because it is the whole reason trails curve.
# Only the LIVE head may be transformed each frame. Every committed point
# is a snapshot of where that entity actually sat in camera space at that
# instant, and must never be touched again. Rotating the whole ring — which
# hyperdrive originally did — mathematically guarantees straight radial
# trails no matter how hard the camera turns, because one rotation applied
# to every point preserves their relative arrangement. The curvature you
# are trying to draw cancels itself out.
#
# THE SLIDING TAIL. A ring that simply drops its oldest point every commit
# makes the trail's far end jump by a whole segment, and at 32x32 a whole
# segment is most of the trail. Instead the oldest slot is walked
# continuously toward the second oldest across the commit interval, so by
# the time it is retired it has already arrived and nothing moves. Measured
# on hyperdrive: worst single-frame tail loss went from 25px to 9px.
#
# LAYOUT — rings is N*L*3 int32, Q10 camera-space coords, entity i's
# points at [i*L*3 .. (i+1)*L*3). heads is N bytes, the index of the live
# head within each entity's block; history runs FORWARD from the head with
# wraparound, which is exactly what wire3d's data[20] ring start expects.
#
# The header (hdr) is shared with the caller's own viper passes, so the
# indices are fixed and documented rather than hidden:
#   0   entity count
#   1   trail length (power of two — the ring masks with trail-1)
#   14  slide phase 0..255 through the current commit interval
#   15  group mask (_GROUPS - 1)
#   16  group phase step (256 // _GROUPS)
#   17  group currently committing

from array import array
from micropython import const

GROUPS = const(8)        # must be a power of two: group is picked with a mask

H_COUNT = const(0)
H_TRAIL = const(1)
H_SLIDE = const(14)
H_GMASK = const(15)
H_GSTEP = const(16)
H_GROUP = const(17)


def new_rings(count, trail):
    """Allocate ring storage for `count` entities of `trail` points each.

    Returns (rings, heads, tails). `tails` holds each entity's slide
    origin — the position its oldest point is walking away from — and is
    why the tail can move continuously instead of in whole segments.
    """
    if trail & (trail - 1):
        raise ValueError("trail length must be a power of two, got %d" % trail)
    rings = array('i', bytes(4 * count * trail * 3))
    heads = bytearray(count)
    tails = array('i', bytes(4 * count * 3))
    return rings, heads, tails


def init_header(hdr, count, trail, groups=GROUPS):
    """Fill the trail3d-owned fields of a shared header array."""
    hdr[H_COUNT] = count
    hdr[H_TRAIL] = trail
    hdr[H_GMASK] = groups - 1
    hdr[H_GSTEP] = 256 // groups
    return hdr


def seed(rings, heads, tails, i, trail, x, y, z):
    """Point every slot of entity i at one position, so a newly spawned
    trail has no history streaking back to wherever it was last used."""
    base = i * trail * 3
    for k in range(trail):
        o = base + k * 3
        rings[o] = x
        rings[o + 1] = y
        rings[o + 2] = z
    heads[i] = 0
    t3 = i * 3
    tails[t3] = x
    tails[t3 + 1] = y
    tails[t3 + 2] = z


# ------------------------------------------------------------------
# Sliding tail (viper). Walk each entity's oldest point toward the second
# oldest, by however far through the commit interval its group is.
#
# Each group is offset through the interval, so groups are at different
# points in their own slide when they commit — the whole field never
# retires a slot on the same frame.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def slide_tail(rings: ptr32, heads: ptr8, tails: ptr32, hdr: ptr32):
    n = int(hdr[0]); trail = int(hdr[1]); slide = int(hdr[14])
    gmask = int(hdr[15]); gstep = int(hdr[16])
    mask = trail - 1
    for i in range(n):
        sl = (slide - (i & gmask) * gstep) & 255
        b = i * trail
        h = int(heads[i])
        old = (b + ((h + trail - 1) & mask)) * 3
        sec = (b + ((h + trail - 2) & mask)) * 3
        t3 = i * 3
        tx = int(tails[t3]); ty = int(tails[t3 + 1]); tz = int(tails[t3 + 2])
        rings[old] = tx + (((int(rings[sec]) - tx) * sl) >> 8)
        rings[old + 1] = ty + (((int(rings[sec + 1]) - ty) * sl) >> 8)
        rings[old + 2] = tz + (((int(rings[sec + 2]) - tz) * sl) >> 8)


# ------------------------------------------------------------------
# Ring commit (viper): the head becomes history.
#
# The head is copied into the next slot downward and the head pointer
# follows it, which retires the slot that was the oldest — by now the
# sliding tail has already walked it onto the second oldest, so nothing
# moves on screen. The new oldest is then snapshotted into `tails` to
# become the next interval's slide origin.
#
# Only the group named in hdr[17] commits, striding by the group count.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def commit(rings: ptr32, heads: ptr8, tails: ptr32, hdr: ptr32):
    n = int(hdr[0]); trail = int(hdr[1])
    mask = trail - 1
    stride = int(hdr[15]) + 1
    i = int(hdr[17])
    while i < n:
        h = int(heads[i])
        nh = (h - 1) & mask
        b = i * trail
        src = (b + h) * 3
        dst = (b + nh) * 3
        rings[dst] = int(rings[src])
        rings[dst + 1] = int(rings[src + 1])
        rings[dst + 2] = int(rings[src + 2])
        heads[i] = nh

        # snapshot the new oldest as the origin the tail slides FROM
        old = (b + ((nh + trail - 1) & mask)) * 3
        t3 = i * 3
        tails[t3] = int(rings[old])
        tails[t3 + 1] = int(rings[old + 1])
        tails[t3 + 2] = int(rings[old + 2])
        i += stride


class Cadence:
    """Wall-clock commit timing, so a trail spans the same flight time at
    any framerate. Kept in Python because it runs once per frame, not once
    per entity, and because time handling is exactly the sort of thing that
    should stay readable.

    Feed it the elapsed milliseconds each frame; it advances the slide
    phase and tells you which groups rolled over and now need committing.
    """

    def __init__(self, hdr, interval_ms, groups=GROUPS):
        self._hdr = hdr
        self.interval = float(interval_ms)
        self.groups = groups
        self._acc = 0.0
        self._prev_slide = 0

    def tick(self, dt_ms):
        """Advance by dt_ms. Returns the list of group indices to commit.

        A group commits when the staggered phase clock passes its offset.
        Comparing this frame's phase against last frame's catches every
        rollover even when a slow frame skips past several at once — which
        is exactly when dropping one would be most visible.
        """
        hdr = self._hdr
        self._acc += dt_ms
        if self._acc >= self.interval:
            self._acc -= self.interval * int(self._acc / self.interval)
        slide = int(self._acc * 256.0 / self.interval) & 255
        hdr[H_SLIDE] = slide

        due = []
        prev = self._prev_slide
        if slide != prev:
            step = 256 // self.groups
            for g in range(self.groups):
                mark = (g * step) & 255
                # did the phase clock cross this group's mark this frame?
                if prev < slide:
                    crossed = prev < mark <= slide
                else:                       # wrapped through 255 -> 0
                    crossed = mark > prev or mark <= slide
                if crossed:
                    due.append(g)
        self._prev_slide = slide
        return due

    def commit_group(self, rings, heads, tails, g):
        self._hdr[H_GROUP] = g
        commit(rings, heads, tails, self._hdr)
