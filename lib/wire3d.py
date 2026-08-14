# /lib/wire3d.py
#
# 3D polyline geometry for gradient-coloured line rendering. Everything
# drawn is a polyline: a star trail, a wireframe edge loop, an attractor
# orbit, a blade of grass.
#
# This module is FORMAT-AGNOSTIC — it never touches a framebuffer. It
# turns 3D points into a list of screen-space segments, and a pixel-layer
# module (surface_rgb888 / surface_rgb565) rasterises that list. The split
# exists because viper cannot parameterise a pointer type: RGB888 needs
# ptr32 and RGB565 needs ptr16, so per-format raster functions are forced.
# Cutting here means ~200 lines of geometry live once and only ~40 lines
# of pixel poking are duplicated per format.
#
# PIPELINE — transform (rotate Q14 + translate) -> project (focal*x/z)
# -> 3D near-plane clip -> 2D Cohen-Sutherland clip -> emit segments.
# Each emitted segment carries its own fixed-point DDA increments and
# gradient span, so the pixel layer performs no division at all.
#
# COLOR — a path is coloured by a 256-entry RGB LUT baked from a
# gradient.py stops list (bake(); the float HSV sampling runs 256 times
# per palette change, never per pixel). A pixel's gradient position is
# its fraction of the path's total length; `phase` offsets the lookup
# (mod 256) so looping gradients flow along the line. The parameter is
# computed before 2D clipping and advances across clipped-off pixels, so
# the gradient does not compress when a line crosses the screen edge.
# Segments fully behind the near plane contribute no length: the gradient
# parameterises the path from its first visible point.
#
# DISTANCE — the gradient is parameterised by true path length, and the
# pixel count is a separate quantity. They are not proportional: a
# 45-degree segment and an axis-aligned one with the same DDA step count
# differ in length by 41%. Using steps for both (which this did until
# Sprint 2e) gave a 45-degree segment ~71% of the gradient share its
# length earns, so colour drifted out of register at every corner.
#
# Length uses the octagonal approximation, alpha=0.9604 beta=0.3978:
#     seglen = (max(|dx|,|dy|) * 246 + min(|dx|,|dy|) * 102) >> 4
# No sqrt, no division, under 4% absolute error — and because the error
# is nearly equal at 0 and 45 degrees, the ratio between two segments,
# which is all the gradient depends on, is good to ~1%. Kept in Q4 so a
# 1-pixel segment still has a non-zero length to contribute.
#
# FIXED POINT — coords are Q10 int32, trig Q14 (products bounded well
# inside int32 for coords up to ~37 units). The near-clip lerp uses a
# Q12 t so (dx * t) stays inside int32 for spans up to ~75 units.
#
# LIMITS — max 64 points per path (scratch lives inside the data array).
#
# DEAD END — do not re-attempt "batching" (one viper call looping over N
# path records). It hard-faulted the board on frame 0, every time, from a
# fresh boot, and was bisected to ground truth by reverting it alone.
# Host simulation with every buffer bounds-guarded never reproduced it.
#
# The `data` int32 array (new_data()) carries params, scratch and output:
#   0..5   cos/sin yaw, pitch, roll (Q14)      — transform
#   6..8   translate x, y, z (Q10)             — transform, after rotation
#   9      point count                         — transform & build_path
#   10     z_near (Q10)
#   11     focal
#   12..13 screen centre x, y
#   14..15 width, height
#   16     brightness 0..256                   — pixel layer
#   17     blend: 0 add, 1 max, 2 opaque, 3 alpha  — pixel layer
#   18     gradient phase (masked by [19])     — pixel layer
#   19     LUT index mask: 255 or 1023         — pixel layer
#   20     ring start index (path starts at this point)
#   21     closed: 1 connects last point back to first
#   22..24 fade decay r, g, b (Q8)             — pixel layer
#   25     point base offset (path indexes pts from base*3)
#   26     emitted segment count  (OUT: build_path -> pixel layer)
#   27     stroke width in pixels (1 = the fast raster)  — pixel layer
#   28     anti-alias flag                     — pixel layer
#   29     alpha 0..256, read only when blend == 3       — pixel layer
#   30     gradient length already consumed by earlier chunks (chunking)
#   31     gradient length of the WHOLE path; 0 = this path is the whole
#   32..   point cache, 2 ints per point
#   160..  segment list, 8 ints per segment:
#            0..1  x0, y0     start pixel, guaranteed on-screen
#            2..3  xi, yi     16.16 DDA increments per pixel
#            4     steps      pixels to draw = steps + inclusive
#            5     pstart     16.16 gradient parameter at x0
#            6     pstep      16.16 gradient increment per pixel
#            7     inclusive  1 if this segment draws its end pixel

import math
from array import array
from micropython import const

import gradient

_TRIG_ONE  = const(16384)
_TRIG_HALF = const(8192)
_COORD_ONE = const(1024)
_MAX_PTS   = const(64)
_PT_CACHE  = const(32)     # 2 ints per point
SEGS       = const(160)    # _PT_CACHE + 2 * _MAX_PTS; 8 ints per segment
_DATA_LEN  = const(672)    # SEGS + 8 * _MAX_PTS
_HIDDEN    = const(536870912)    # point-cache marker: behind near plane
                                 # (2**29: stays a MicroPython small int)
_PROJ_MAX  = const(16384)        # hard rail on projected coords. Nothing real
                                 # reaches it (a star at the near plane
                                 # projects to ~5700 px), but it bounds every
                                 # downstream product no matter what a caller
                                 # passes in.


def new_data(lut_size=256):
    """Allocate a path data array with working defaults.

    Several fields are actively wrong when zero — a zero LUT mask sends
    every gradient lookup to entry 0, and a zero alpha makes an
    alpha-blended line invisible — so this cannot just hand back a zeroed
    array and trust callers to fill it in.
    """
    d = array('i', bytes(4 * _DATA_LEN))
    d[16] = 256          # brightness, 1.0
    d[17] = 0            # blend: additive
    d[19] = lut_size - 1  # LUT index mask (255 for 256 entries, 1023 for 1024)
    d[27] = 1            # stroke width in pixels
    d[28] = 0            # anti-aliasing off
    d[29] = 256          # alpha, opaque (only read when blend == 3)
    return d


def bake(stops, lut=None, size=256):
    """Bake a gradient.py stops list into a `size`-entry RGB LUT.

    Thin wrapper over gradient.bake(), which is where the ramp-filling
    lives. This used to call gradient.sample() once per entry; that cost
    267ms and 191KB of heap per palette change on the Cosmic. See the
    note on gradient.bake().
    """
    return gradient.bake(stops, lut, size)


def set_view(data, width, height, focal=12, z_near=0.08):
    data[10] = int(z_near * _COORD_ONE)
    data[11] = focal
    data[12] = width >> 1
    data[13] = height >> 1
    data[14] = width
    data[15] = height


def set_rotation(data, yaw, pitch, roll):
    data[0] = int(math.cos(yaw) * _TRIG_ONE)
    data[1] = int(math.sin(yaw) * _TRIG_ONE)
    data[2] = int(math.cos(pitch) * _TRIG_ONE)
    data[3] = int(math.sin(pitch) * _TRIG_ONE)
    data[4] = int(math.cos(roll) * _TRIG_ONE)
    data[5] = int(math.sin(roll) * _TRIG_ONE)


def set_translation(data, tx, ty, tz):
    data[6] = int(tx * _COORD_ONE)
    data[7] = int(ty * _COORD_ONE)
    data[8] = int(tz * _COORD_ONE)


# ------------------------------------------------------------------
# Transform: dst = rotate(src) + translate, data[9] points.
# src and dst may be the same array (incremental camera rotation).
# Rounding note: signed >> floors on both signs; +_TRIG_HALF rounds each
# shift to nearest so the error is zero-mean and does not accumulate.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def transform(src: ptr32, dst: ptr32, data: ptr32):
    cos_y = int(data[0]); sin_y = int(data[1])
    cos_p = int(data[2]); sin_p = int(data[3])
    cos_r = int(data[4]); sin_r = int(data[5])
    tx = int(data[6]); ty = int(data[7]); tz = int(data[8])
    n = int(data[9])
    for i in range(n):
        b = i * 3
        x = int(src[b]); y = int(src[b + 1]); z = int(src[b + 2])
        rx = (x * cos_y - z * sin_y + _TRIG_HALF) >> 14
        rz = (x * sin_y + z * cos_y + _TRIG_HALF) >> 14
        x = rx; z = rz
        ry = (y * cos_p - z * sin_p + _TRIG_HALF) >> 14
        rz = (y * sin_p + z * cos_p + _TRIG_HALF) >> 14
        y = ry; z = rz
        rx = (x * cos_r - y * sin_r + _TRIG_HALF) >> 14
        ry = (x * sin_r + y * cos_r + _TRIG_HALF) >> 14
        dst[b] = rx + tx
        dst[b + 1] = ry + ty
        dst[b + 2] = z + tz


# ------------------------------------------------------------------
# Geometry pass. Points are camera-space Q10 at pts[(base+i)*3]; the path
# visits data[9] points starting at ring index data[20]. Writes the
# emitted segment count to data[26] and the segments from SEGS.
# ------------------------------------------------------------------
@micropython.viper  # noqa: F821
def measure_path(pts: ptr32, data: ptr32) -> int:
    """Screen-space length of a path, in the same Q4 units build_path uses.

    Needed only for chunking. A path over _MAX_PTS points is drawn as
    several build_path calls, and none of them can know the whole path's
    length — which is exactly what the gradient has to be divided by. So
    the caller measures every chunk first, sums, and then draws with that
    total in data[31].

    Projection and near-clipping are repeated here, so a chunked path
    costs roughly two passes. That is affordable because long paths are
    drawn a handful of times per frame, not hundreds: an attractor orbit
    is one path, where a starfield is 360.
    """
    n = int(data[9])
    z_near = int(data[10]); focal = int(data[11])
    cx = int(data[12]); cy = int(data[13])
    start = int(data[20]); closed = int(data[21])
    base = int(data[25])
    nseg = n if closed else n - 1

    total = int(0)
    for k in range(nseg):
        ja = k
        jb = k + 1
        if jb >= n: jb = 0
        ia = start + ja
        if ia >= n: ia -= n
        ib = start + jb
        if ib >= n: ib -= n
        pa = (base + ia) * 3
        pb = (base + ib) * 3
        za = int(pts[pa + 2]); zb = int(pts[pb + 2])
        if za < z_near and zb < z_near:
            continue
        xa = int(pts[pa]); ya = int(pts[pa + 1])
        xb = int(pts[pb]); yb = int(pts[pb + 1])
        if za < z_near:
            t = ((z_near - za) << 12) // (zb - za)
            xa += ((xb - xa) * t) >> 12
            ya += ((yb - ya) * t) >> 12
            za = z_near
        elif zb < z_near:
            t = ((z_near - zb) << 12) // (za - zb)
            xb += ((xa - xb) * t) >> 12
            yb += ((ya - yb) * t) >> 12
            zb = z_near
        x0 = (focal * xa) // za + cx
        y0 = (focal * ya) // za + cy
        x1 = (focal * xb) // zb + cx
        y1 = (focal * yb) // zb + cy
        if x0 > _PROJ_MAX: x0 = _PROJ_MAX
        elif x0 < -_PROJ_MAX: x0 = -_PROJ_MAX
        if y0 > _PROJ_MAX: y0 = _PROJ_MAX
        elif y0 < -_PROJ_MAX: y0 = -_PROJ_MAX
        if x1 > _PROJ_MAX: x1 = _PROJ_MAX
        elif x1 < -_PROJ_MAX: x1 = -_PROJ_MAX
        if y1 > _PROJ_MAX: y1 = _PROJ_MAX
        elif y1 < -_PROJ_MAX: y1 = -_PROJ_MAX
        dx = x1 - x0
        dy = y1 - y0
        adx = dx if dx >= 0 else -dx
        ady = dy if dy >= 0 else -dy
        if adx > ady:
            total += (adx * 246 + ady * 102) >> 4
        else:
            total += (ady * 246 + adx * 102) >> 4
    return total


@micropython.viper  # noqa: F821
def build_path(pts: ptr32, data: ptr32):
    n = int(data[9])
    z_near = int(data[10]); focal = int(data[11])
    cx = int(data[12]); cy = int(data[13])
    width = int(data[14]); height = int(data[15])
    start = int(data[20]); closed = int(data[21])
    base = int(data[25])
    nseg = n if closed else n - 1
    data[26] = 0

    # ---- project each path point exactly once ----
    # Also builds the screen bounding box. Measured on this workload, 62%
    # of paths draw nothing, and rejecting them here — after projecting, so
    # the test is exact and can never drop a visible path — skips the
    # segment assembly and clipping below, roughly half of this function's
    # work. (A cheaper cull on the head point alone was tried and is
    # unsound: see the note in hyperdrive's _advance.)
    any_hidden = int(0)
    vis = int(0)
    bb_x0 = int(_PROJ_MAX); bb_y0 = int(_PROJ_MAX)
    bb_x1 = -int(_PROJ_MAX); bb_y1 = -int(_PROJ_MAX)
    for j in range(n):
        ia = start + j
        if ia >= n: ia -= n
        pa = (base + ia) * 3
        pc = _PT_CACHE + j * 2
        za = int(pts[pa + 2])
        if za < z_near:
            data[pc] = _HIDDEN
            any_hidden = 1
        else:
            sxp = (focal * int(pts[pa])) // za + cx
            syp = (focal * int(pts[pa + 1])) // za + cy
            if sxp > _PROJ_MAX: sxp = _PROJ_MAX
            elif sxp < -_PROJ_MAX: sxp = -_PROJ_MAX
            if syp > _PROJ_MAX: syp = _PROJ_MAX
            elif syp < -_PROJ_MAX: syp = -_PROJ_MAX
            data[pc] = sxp
            data[pc + 1] = syp
            vis += 1
            if sxp < bb_x0: bb_x0 = sxp
            if sxp > bb_x1: bb_x1 = sxp
            if syp < bb_y0: bb_y0 = syp
            if syp > bb_y1: bb_y1 = syp

    if vis == 0:
        return
    # Only sound when nothing was near-clipped: a hidden point's clipped
    # replacement lies somewhere unknown and is not represented in the box.
    if any_hidden == 0:
        if bb_x1 < 0 or bb_x0 >= width or bb_y1 < 0 or bb_y0 >= height:
            return

    # ---- assemble unclipped segments, near-clipping the crossers ----
    # Endpoints are stashed in the segment slots and rewritten in place by
    # the clip pass below; slot 4 holds the unclipped step count, or -1 for
    # a segment entirely behind the camera.
    total = int(0)
    have_pt = int(0)
    dot_x = int(0); dot_y = int(0)
    for k in range(nseg):
        ja = k
        jb = k + 1
        if jb >= n: jb = 0
        ca = _PT_CACHE + ja * 2
        cb = _PT_CACHE + jb * 2
        a_hid = int(1) if int(data[ca]) == _HIDDEN else int(0)
        b_hid = int(1) if int(data[cb]) == _HIDDEN else int(0)
        sc = SEGS + k * 8
        if a_hid and b_hid:
            data[sc + 4] = -1
            continue
        if a_hid or b_hid:
            # crosses the near plane: lerp the hidden end in 3D and
            # project the clipped point (the only extra projection)
            ia = start + ja
            if ia >= n: ia -= n
            ib = start + jb
            if ib >= n: ib -= n
            pa = (base + ia) * 3
            pb = (base + ib) * 3
            xa = int(pts[pa]); ya = int(pts[pa + 1]); za = int(pts[pa + 2])
            xb = int(pts[pb]); yb = int(pts[pb + 1]); zb = int(pts[pb + 2])
            if a_hid:
                t = ((z_near - za) << 12) // (zb - za)
                xa += ((xb - xa) * t) >> 12
                ya += ((yb - ya) * t) >> 12
                x0 = (focal * xa) // z_near + cx
                y0 = (focal * ya) // z_near + cy
                x1 = int(data[cb]); y1 = int(data[cb + 1])
            else:
                t = ((z_near - zb) << 12) // (za - zb)
                xb += ((xa - xb) * t) >> 12
                yb += ((ya - yb) * t) >> 12
                x0 = int(data[ca]); y0 = int(data[ca + 1])
                x1 = (focal * xb) // z_near + cx
                y1 = (focal * yb) // z_near + cy
        else:
            x0 = int(data[ca]); y0 = int(data[ca + 1])
            x1 = int(data[cb]); y1 = int(data[cb + 1])
        if have_pt == 0:
            dot_x = x0; dot_y = y0
            have_pt = 1
        dx = x1 - x0
        dy = y1 - y0
        adx = dx if dx >= 0 else -dx
        ady = dy if dy >= 0 else -dy
        steps = adx if adx > ady else ady
        # Two different metrics, deliberately. `steps` is Chebyshev — it is
        # the DDA pixel count and nothing else. `seglen` is the octagonal
        # approximation of true distance, alpha=0.9604 beta=0.3978 in Q8,
        # kept in Q4 units (~15.375 per pixel of length): under 4% absolute
        # error, and the error is nearly identical at 0 and 45 degrees so
        # the RATIO between segments — which is all the gradient cares
        # about — is good to ~1%. No sqrt, no division.
        mn = adx if adx < ady else ady
        seglen = (steps * 246 + mn * 102) >> 4
        data[sc] = x0; data[sc + 1] = y0
        data[sc + 2] = x1; data[sc + 3] = y1
        data[sc + 4] = steps
        data[sc + 5] = seglen
        total += seglen

    if total == 0:
        # degenerate (coincident or single visible point): emit one pixel
        # so the pixel layer needs no special case of its own
        if have_pt:
            if dot_x >= 0 and dot_x < width and dot_y >= 0 and dot_y < height:
                data[SEGS] = dot_x; data[SEGS + 1] = dot_y
                data[SEGS + 2] = 0; data[SEGS + 3] = 0
                data[SEGS + 4] = 0
                data[SEGS + 5] = 0; data[SEGS + 6] = 0
                data[SEGS + 7] = 1
                data[26] = 1
        return

    # `total` is a length in Q4 units, so it can be ~15x the pixel count.
    # dlen must keep enough bits to be a useful rate, and (cum + fc) * dlen
    # must stay inside int32. Both hold once total is under ~4M, which only
    # a pathological path exceeds — 64 segments each thousands of pixels
    # long, i.e. mostly off-screen, where gradient precision is moot anyway.
    # CHUNKING. A path longer than _MAX_PTS points is drawn as several
    # calls, and the gradient has to run continuously across them rather
    # than restarting at every chunk boundary. data[31] carries the whole
    # path's length (from measure_path) and data[30] how much of it earlier
    # chunks already consumed; with both zero this path is the whole path
    # and the parameterisation is exactly as before.
    span = int(data[31])
    cum = int(data[30])
    if span == 0:
        span = total
        cum = 0

    lsh = int(0)
    while span > 4000000:
        span >>= 1
        lsh += 1
    # The parameter spans the LUT, not a fixed 0..255. A 1024-entry table
    # is pointless if the gradient position saturates a quarter of the way
    # through it, and the pixel layer's mask cannot fix that — masking only
    # wraps a value that was never large enough to begin with.
    dlen = (int(data[19]) << 16) // span

    # ---- 2D clip, then emit drawable segments compacted from index 0 ----
    # Reading slot k and writing slot `out` is safe because out <= k always,
    # so the lookahead to slot k+1 is never stale. The closed-path wrap
    # looks at slot 0, which compaction CAN have overwritten, so its
    # original state is captured first.
    first_hidden = int(1) if int(data[SEGS + 4]) < 0 else int(0)
    out = int(0)
    for k in range(nseg):
        sc = SEGS + k * 8
        steps = int(data[sc + 4])
        if steps <= 0:
            continue
        x0 = int(data[sc]); y0 = int(data[sc + 1])
        x1 = int(data[sc + 2]); y1 = int(data[sc + 3])
        px0 = x0; py0 = y0     # pre-clip start, for the front-clip count

        c0 = int(0)
        if x0 < 0: c0 = 1
        elif x0 >= width: c0 = 2
        if y0 < 0: c0 |= 4
        elif y0 >= height: c0 |= 8
        c1 = int(0)
        if x1 < 0: c1 = 1
        elif x1 >= width: c1 = 2
        if y1 < 0: c1 |= 4
        elif y1 >= height: c1 |= 8

        accept = int(0)
        for _clip in range(8):
            if (c0 | c1) == 0:
                accept = 1
                break
            if (c0 & c1) != 0:
                break
            c = c0 if c0 != 0 else c1
            dx = x1 - x0
            dy = y1 - y0
            if c & 1:
                ny = y0 + dy * (0 - x0) // dx
                nx = 0
            elif c & 2:
                nx = width - 1
                ny = y0 + dy * (nx - x0) // dx
            elif c & 4:
                nx = x0 + dx * (0 - y0) // dy
                ny = 0
            else:
                ny = height - 1
                nx = x0 + dx * (ny - y0) // dy
            if c == c0:
                x0 = nx; y0 = ny
                c0 = 0
                if x0 < 0: c0 = 1
                elif x0 >= width: c0 = 2
                if y0 < 0: c0 |= 4
                elif y0 >= height: c0 |= 8
            else:
                x1 = nx; y1 = ny
                c1 = 0
                if x1 < 0: c1 = 1
                elif x1 >= width: c1 = 2
                if y1 < 0: c1 |= 4
                elif y1 >= height: c1 |= 8
        if accept == 0:
            # entirely off-screen, but it still occupies its share of the
            # path's length, so the gradient must skip past it
            cum += int(data[sc + 5])
            continue

        # HARD RAILS. Cohen-Sutherland should already guarantee both
        # endpoints are on-screen, bounding the DDA to max(width, height)
        # steps. Enforcing it here too costs eight comparisons per segment
        # and makes an out-of-range raster write structurally impossible
        # rather than merely proven — worth it for a pointer loop that has
        # no bounds check of its own.
        if x0 < 0: x0 = 0
        elif x0 >= width: x0 = width - 1
        if y0 < 0: y0 = 0
        elif y0 >= height: y0 = height - 1
        if x1 < 0: x1 = 0
        elif x1 >= width: x1 = width - 1
        if y1 < 0: y1 = 0
        elif y1 >= height: y1 = height - 1

        # Pixels clipped off the front still advance the gradient. Measure
        # that lead-in with the same octagonal metric as the segment itself,
        # not the Chebyshev step count — the two are only proportional along
        # one direction, and this piece is collinear with the segment, so the
        # same formula makes the ratio exact instead of merely close.
        dx = x0 - px0
        dy = y0 - py0
        adx = dx if dx >= 0 else -dx
        ady = dy if dy >= 0 else -dy
        if adx > ady:
            fclen = (adx * 246 + ady * 102) >> 4
        else:
            fclen = (ady * 246 + adx * 102) >> 4
        fclen = fclen >> lsh

        dx = x1 - x0
        dy = y1 - y0
        adx = dx if dx >= 0 else -dx
        ady = dy if dy >= 0 else -dy
        csteps = adx if adx > ady else ady
        max_span = width if width > height else height
        if csteps > max_span: csteps = max_span

        # A segment omits its end pixel — the next segment draws it — so
        # shared vertices land exactly once under additive blend. The end
        # pixel is drawn only when no successor will: last segment of an
        # open path, or the successor is behind the camera.
        inclusive = int(0)
        knext = k + 1
        if knext >= nseg:
            if closed:
                inclusive = first_hidden
            else:
                inclusive = 1
        elif int(data[SEGS + knext * 8 + 4]) < 0:
            inclusive = 1

        if csteps == 0:
            xi = int(0); yi = int(0)
        else:
            xi = (dx << 16) // csteps
            yi = (dy << 16) // csteps

        # Gradient span. `seglen` must be read before slot 5 is overwritten:
        # compaction can make od == sc, and slot 5 holds the segment length
        # on the way in and the gradient start on the way out.
        seglen_raw = int(data[sc + 5])
        seglen = seglen_raw >> lsh
        if fclen > seglen:
            fclen = seglen
        # Two roles, two metrics: csteps is how many pixels to draw,
        # seglen is how much gradient this segment is entitled to. The
        # per-pixel rate divides by the segment's ORIGINAL step count —
        # clipping removes pixels but does not change the rate along the
        # line, which is what keeps a part-clipped segment in register
        # with its neighbours.
        od = SEGS + out * 8
        data[od] = x0; data[od + 1] = y0
        data[od + 2] = xi; data[od + 3] = yi
        data[od + 4] = csteps
        data[od + 5] = ((cum >> lsh) + fclen) * dlen
        data[od + 6] = (seglen * dlen) // steps
        data[od + 7] = inclusive
        out += 1
        cum += seglen_raw

    data[26] = out


# ------------------------------------------------------------------
# Long paths: more points than one data array can hold.
#
# The scratch for projection and segments lives inside the data array, so
# a single build_path handles at most _MAX_PTS points. An attractor orbit
# or a long ribbon wants hundreds. Chunking splits the point list into
# overlapping runs — consecutive chunks SHARE a point, or the segment
# spanning the boundary would never be drawn — and carries the gradient
# across by measuring the whole path first.
#
# Two passes over the geometry, which is why this is a helper and not the
# default: paying it for hyperdrive's 360 short trails would be absurd,
# paying it for one 500-point orbit is nothing.
# ------------------------------------------------------------------
MAX_PTS = _MAX_PTS


def chunk_ranges(n, closed=0, size=_MAX_PTS):
    """Yield (offset, count) covering n points in overlapping chunks."""
    if n <= size:
        yield 0, n
        return
    if closed:
        raise ValueError("closed paths cannot be chunked: the wrap segment "
                         "joins the last point to the first, which are never "
                         "in the same chunk")
    off = 0
    while off < n - 1:
        take = size if n - off > size else n - off
        yield off, take
        off += take - 1          # overlap by one so the joint is drawn


def draw_long_path(pts, data, fb, lut, draw_fn):
    """Draw a path of any length with one continuous gradient.

    `pts` is a flat Q10 x,y,z array already in camera space. Returns the
    number of chunks drawn.
    """
    n = int(data[9])
    closed = int(data[21])
    if n <= _MAX_PTS:
        build_path(pts, data)
        if data[26]:
            draw_fn(fb, lut, data)
        return 1

    saved_n = n
    saved_base = int(data[25])
    ranges = list(chunk_ranges(n, closed))

    # pass 1: total length, so every chunk can be given its true share
    total = 0
    lengths = []
    data[21] = 0
    for off, take in ranges:
        data[9] = take
        data[25] = saved_base + off
        seg = int(measure_path(pts, data))
        lengths.append(seg)
        total += seg

    # pass 2: draw, handing each chunk the running total before it
    cum = 0
    data[31] = total
    for (off, take), seg in zip(ranges, lengths):
        data[9] = take
        data[25] = saved_base + off
        data[30] = cum
        build_path(pts, data)
        if data[26]:
            draw_fn(fb, lut, data)
        cum += seg

    data[9] = saved_n
    data[25] = saved_base
    data[21] = closed
    data[30] = 0
    data[31] = 0
    return len(ranges)
