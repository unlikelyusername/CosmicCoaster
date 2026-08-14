"""The gradient must be parameterised by DISTANCE, not by pixel count.

This is the defect Sprint 2e existed to fix. The rasteriser walks a line
with a DDA whose step count is the Chebyshev distance, max(|dx|,|dy|), and
that number was doing double duty as the gradient's length metric. It is
proportional to true distance only for a fixed direction: a 45-degree
segment covers sqrt(2) times more ground per pixel than an axis-aligned
one, so on a path that changes angle the colour drifts out of register at
every corner. A 45-degree segment received about 71% of the gradient share
its length earned.

The headline case below is two segments of equal TRUE length, one
axis-aligned and one at 45 degrees. The joint between them must land at
the halfway point of the gradient. Before the fix it landed at 58.6%.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import load_wire3d, make_path, GuardedFB      # noqa: E402

w3, px, gradient = load_wire3d()

W = H = 32
SEGS = w3.SEGS

data = w3.new_data()
w3.set_view(data, W, H)
data[16] = 256      # brightness
data[17] = 0        # additive
data[18] = 0        # phase
data[20] = 0        # ring start
data[21] = 0        # open path
data[25] = 0        # base offset

# A LUT whose red channel IS the gradient index, so a drawn pixel reports
# its own gradient position.
lut = bytearray(768)
for i in range(256):
    lut[i * 3] = i

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
    return cond


def seg_param(k):
    """Gradient position 0..255 at the start of emitted segment k."""
    return (data[SEGS + k * 8 + 5] >> 16) & 0x1FF


# ---------------------------------------------------------------- 1
# Equal true length, different angle. Segment A is horizontal, 17px.
# Segment B is 45 degrees, 12px on each axis = 16.97px true. Their
# lengths match to 0.2%, so the joint belongs at 50% of the gradient.
pts = make_path(w3, data, [(1, 16), (18, 16), (30, 28)])
data[9] = 3
w3.build_path(pts, data)

check(data[26] == 2, "expected 2 segments, got %d" % data[26])
joint = seg_param(1)
pct = joint * 100.0 / 255.0
print("equal-length joint at %.1f%% of gradient (was 58.6%% pre-fix)" % pct)
check(abs(pct - 50.0) <= 3.0,
      "joint at %.1f%%, expected 50%% +/-3 -- gradient is not distance-based" % pct)

# The Chebyshev answer must NOT be what we get, or the test proves nothing.
cheb = 17 * 100.0 / (17 + 12)
check(abs(pct - cheb) > 5.0,
      "joint at %.1f%% matches the old Chebyshev answer %.1f%%" % (pct, cheb))

# ---------------------------------------------------------------- 2
# Equal PIXEL count, different angle: a 12px horizontal run and a 12px
# diagonal run. The diagonal is sqrt(2) longer, so it must take ~58.6%
# of the gradient -- the exact number the broken version gave the other
# case. Same arithmetic, opposite direction, which is the real proof.
pts = make_path(w3, data, [(2, 16), (14, 16), (26, 28)])
data[9] = 3
w3.build_path(pts, data)
joint2 = seg_param(1)
pct2 = joint2 * 100.0 / 255.0
want2 = 100.0 / (1.0 + 2 ** 0.5)
print("equal-pixel joint at %.1f%% of gradient (true-length share %.1f%%)"
      % (pct2, want2))
check(abs(pct2 - want2) <= 3.0,
      "joint at %.1f%%, expected %.1f%% for equal pixel counts" % (pct2, want2))

# ---------------------------------------------------------------- 3
# Straight line, split into segments at arbitrary points. Splitting a
# straight path must not change its colouring at all: the gradient
# position at a given pixel is a property of the geometry, not of how
# many vertices happen to describe it.
fb_a = GuardedFB(W * H)
pts = make_path(w3, data, [(2, 16), (30, 16)])
data[9] = 2
w3.build_path(pts, data)
px.draw_path(fb_a, lut, data)

fb_b = GuardedFB(W * H)
pts = make_path(w3, data, [(2, 16), (9, 16), (11, 16), (24, 16), (30, 16)])
data[9] = 5
w3.build_path(pts, data)
px.draw_path(fb_b, lut, data)

row = 16 * W
diffs = [abs(((fb_a[row + x] >> 16) & 255) - ((fb_b[row + x] >> 16) & 255))
         for x in range(W) if fb_a[row + x] and fb_b[row + x]]
worst = max(diffs) if diffs else 0
print("straight line, 1 segment vs 4: worst gradient difference %d/255" % worst)
check(len(diffs) > 20, "not enough overlapping pixels to compare (%d)" % len(diffs))
check(worst <= 4, "splitting a straight line shifted the gradient by %d" % worst)

# ---------------------------------------------------------------- 4
# Monotonic and full-range across a multi-segment zigzag: every drawn
# pixel's gradient position must rise along the path and span most of
# 0..255. Catches a segment that fails to hand off to the next.
fb = GuardedFB(W * H)
pts = make_path(w3, data, [(2, 4), (14, 16), (2, 28), (20, 28), (30, 18)])
data[9] = 5
w3.build_path(pts, data)
px.draw_path(fb, lut, data)
nseg = data[26]
params = [seg_param(k) for k in range(nseg)]
print("zigzag: %d segments, gradient starts %s" % (nseg, params))
check(all(params[i] <= params[i + 1] for i in range(len(params) - 1)),
      "segment gradient starts are not monotonic: %s" % params)
check(params[0] <= 4, "path does not start at gradient 0 (got %d)" % params[0])
last_end = params[-1] + ((data[SEGS + (nseg - 1) * 8 + 6] *
                          data[SEGS + (nseg - 1) * 8 + 4]) >> 16)
check(last_end >= 240,
      "path ends at gradient %d, expected to reach ~255" % last_end)

# ---------------------------------------------------------------- 5
# A 1-pixel segment must still carry non-zero length. In Q4 the octagonal
# formula gives an axis-aligned 1px segment 15 units; at Q8 it would round
# to zero and a path of short segments would divide by zero.
pts = make_path(w3, data, [(10, 16), (11, 16), (12, 17), (13, 17)])
data[9] = 4
w3.build_path(pts, data)
check(data[26] >= 1, "1px segments produced no output")
ps = [seg_param(k) for k in range(data[26])]
print("1px segments: gradient starts %s" % ps)
check(len(set(ps)) > 1, "1px segments all share a gradient position: %s" % ps)

# ---------------------------------------------------------------- 6
# Off-screen lead-in. A path that starts outside the viewport still owns
# that length: the visible part must begin partway into the gradient,
# not at 0.
pts = make_path(w3, data, [(-40, 16), (30, 16)])
data[9] = 2
w3.build_path(pts, data)
lead = seg_param(0)
print("off-screen lead-in: visible part starts at gradient %d" % lead)
check(lead > 60, "clipped-off lead-in did not advance the gradient (%d)" % lead)

print()
if failures:
    for f in failures:
        print("FAIL:", f)
    print("DISTANCE GRADIENT: %d FAILURES" % len(failures))
    sys.exit(1)
print("DISTANCE GRADIENT: ALL CHECKS PASSED")
