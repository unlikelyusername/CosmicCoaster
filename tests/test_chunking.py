"""Long paths: more points than one data array can hold.

build_path's scratch lives inside the data array, so it handles at most
_MAX_PTS points. A Lorenz orbit or a long ribbon wants hundreds. The thing
that makes this more than a loop is the GRADIENT: each chunk has to be
told what share of the whole path it represents, or every chunk restarts
the ramp and a single orbit comes out looking like a barber's pole.

The decisive test is that a chunked path is coloured the same as an
unchunked one, which is checkable because a 64-point path fits in one
chunk and can be drawn both ways.
"""
import os
import sys
import math
from array import array

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import load_wire3d, GuardedFB      # noqa: E402

w3, px, gradient = load_wire3d()

W = H = 32
NPIX = W * H
failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
    return cond


# red channel encodes the gradient index
lut = bytearray(768)
for i in range(256):
    lut[i * 3] = i
    lut[i * 3 + 1] = 255 - i


def fresh(n):
    d = w3.new_data()
    w3.set_view(d, W, H)
    d[9] = n
    d[21] = 0
    d[25] = 0
    return d


def spiral(n, turns=2.0, z=6.0):
    """A path that changes direction constantly, so segment lengths vary
    and a naive per-chunk gradient shows up immediately."""
    pts = array('i', [0] * (n * 3))
    for i in range(n):
        t = i / float(n - 1)
        a = t * turns * 2.0 * math.pi
        r = 0.35 + 0.55 * t
        pts[i * 3] = int(math.cos(a) * r * 1024)
        pts[i * 3 + 1] = int(math.sin(a) * r * 1024)
        pts[i * 3 + 2] = int((z - 2.0 * t) * 1024)
    return pts


# ---------------------------------------------------------------- ranges
rs = list(w3.chunk_ranges(64))
check(rs == [(0, 64)], "a 64-point path should be one chunk, got %s" % rs)
rs = list(w3.chunk_ranges(200))
covered = set()
for off, take in rs:
    for i in range(off, off + take - 1):
        covered.add(i)              # segments, not points
check(covered == set(range(199)),
      "chunking skipped segments: missing %s" % sorted(set(range(199)) - covered))
check(all(t <= w3.MAX_PTS for _o, t in rs),
      "a chunk exceeded MAX_PTS: %s" % rs)
check(all(rs[i][0] + rs[i][1] - 1 == rs[i + 1][0] for i in range(len(rs) - 1)),
      "chunks do not overlap by exactly one point: %s" % rs)
print("chunk_ranges(200): %d chunks, every segment covered exactly once" % len(rs))

try:
    list(w3.chunk_ranges(200, closed=1))
    failures.append("chunking a closed path should raise")
except ValueError:
    print("closed paths correctly refused (the wrap segment cannot be chunked)")

# ---------------------------------------------------------------- identity
# 64 points fits one chunk, so the chunked and unchunked routes must agree
# pixel for pixel. If the gradient bookkeeping is wrong at all, it shows here.
pts = spiral(64)
d1 = fresh(64)
fb1 = GuardedFB(NPIX)
w3.build_path(pts, d1)
px.draw_path(fb1, lut, d1)

d2 = fresh(64)
fb2 = GuardedFB(NPIX)
w3.draw_long_path(pts, d2, fb2, lut, px.draw_path)

diff = sum(1 for i in range(NPIX) if fb1[i] != fb2[i])
print("64-point path: direct vs draw_long_path differ in %d of %d px"
      % (diff, NPIX))
check(diff == 0, "draw_long_path changed a path that needs no chunking")

# ---------------------------------------------------------------- continuity
# 200 points, four chunks. The gradient must rise across the WHOLE path and
# not reset at each boundary. Sample the drawn pixels in path order by
# rebuilding chunk by chunk and reading each chunk's emitted spans.
n = 200
pts = spiral(n)
d = fresh(n)
fb = GuardedFB(NPIX)
nchunks = w3.draw_long_path(pts, d, fb, lut, px.draw_path)
print("%d-point path drew as %d chunks" % (n, nchunks))
check(nchunks > 1, "200 points should have needed several chunks")

# Re-run the two passes by hand to read each chunk's gradient range.
ranges = list(w3.chunk_ranges(n))
total = 0
lens = []
for off, take in ranges:
    d[9] = take; d[25] = off
    L = int(w3.measure_path(pts, d))
    lens.append(L)
    total += L
starts = []
cum = 0
d[31] = total
for (off, take), L in zip(ranges, lens):
    d[9] = take; d[25] = off; d[30] = cum
    w3.build_path(pts, d)
    if d[26]:
        starts.append((d[w3.SEGS + 5] >> 16) & 0x3FF)
    cum += L
d[30] = 0; d[31] = 0

print("chunk gradient starts: %s" % starts)
check(all(starts[i] <= starts[i + 1] for i in range(len(starts) - 1)),
      "chunk gradient starts are not ascending: %s" % starts)
check(starts[0] <= 4, "first chunk does not start at 0 (got %d)" % starts[0])
check(starts[-1] >= 150,
      "last chunk starts at only %d/255 -- the gradient is restarting per chunk"
      % starts[-1])

# The failure this guards against: each chunk restarting from 0. Prove the
# test can see it, by drawing without the total and confirming it looks wrong.
bad_starts = []
for off, take in ranges:
    d[9] = take; d[25] = off; d[30] = 0; d[31] = 0
    w3.build_path(pts, d)
    if d[26]:
        bad_starts.append((d[w3.SEGS + 5] >> 16) & 0x3FF)
print("without the shared total, starts would be: %s" % bad_starts)
check(max(bad_starts) < starts[-1],
      "the per-chunk-restart failure mode is not distinguishable from correct")

# ---------------------------------------------------------------- long fuzz
import random      # noqa: E402
random.seed(5)
for trial in range(200):
    n = random.choice((65, 100, 127, 128, 129, 300, 512))
    pts = array('i', [0] * (n * 3))
    for i in range(n):
        for c in range(3):
            pts[i * 3 + c] = int(random.uniform(-40.0, 40.0) * 1024)
    d = fresh(n)
    d[17] = trial & 3
    fb = GuardedFB(NPIX)
    w3.draw_long_path(pts, d, fb, lut, px.draw_path)
    check(d[9] == n, "draw_long_path left data[9] as %d, expected %d" % (d[9], n))
    check(d[30] == 0 and d[31] == 0,
          "draw_long_path left chunk state behind: %d %d" % (d[30], d[31]))
print("fuzz: 200 long paths up to 512 points, no OOB, state restored")

print()
if failures:
    for f in failures:
        print("FAIL:", f)
    print("CHUNKING: %d FAILURES" % len(failures))
    sys.exit(1)
print("CHUNKING: ALL CHECKS PASSED")
