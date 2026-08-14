"""Blend modes, anti-aliasing, stroke width, and the deep LUT.

Every raster in surface_rgb888 writes through a raw pointer with no bounds
checking of its own, so each one is exercised here against a framebuffer
that checks every access. draw_path_aa and draw_path_wide are the risky
ones: they deliberately touch pixels NEXT to the line, and the geometry
rails only ever promised that the line itself is on screen.
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import load_wire3d, make_path, GuardedFB      # noqa: E402

w3, px, gradient = load_wire3d()

W = H = 32
NPIX = W * H
SEGS = w3.SEGS

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
    return cond


def fresh(lut_size=256):
    d = w3.new_data(lut_size)
    w3.set_view(d, W, H)
    d[9] = 2
    d[20] = 0
    d[21] = 0
    d[25] = 0
    return d


def flat_lut(r, g, b, size=256):
    lut = bytearray(size * 3)
    for i in range(size):
        lut[i * 3] = r; lut[i * 3 + 1] = g; lut[i * 3 + 2] = b
    return lut


def draw(fb, data, lut, pts, fn=None):
    w3.build_path(pts, data)
    (fn or px.draw_path)(fb, lut, data)


HLINE = [(4, 16), (27, 16)]

# ---------------------------------------------------------------- blend
grey = flat_lut(100, 100, 100)

for mode, name, want in ((0, "additive", 200), (1, "max", 100), (2, "opaque", 100)):
    d = fresh()
    d[17] = mode
    fb = GuardedFB(NPIX)
    draw(fb, d, grey, make_path(w3, d, HLINE))
    draw(fb, d, grey, make_path(w3, d, HLINE))       # same line twice
    got = (fb[16 * W + 16] >> 16) & 255
    print("blend %-8s two passes -> %3d (expect %d)" % (name, got, want))
    check(got == want, "blend %s gave %d, expected %d" % (name, got, want))

# opaque must actually REPLACE, not merge: a dark line over a bright one
d = fresh(); d[17] = 2
fb = GuardedFB(NPIX)
draw(fb, d, flat_lut(240, 240, 240), make_path(w3, d, HLINE))
draw(fb, d, flat_lut(20, 20, 20), make_path(w3, d, HLINE))
got = (fb[16 * W + 16] >> 16) & 255
print("blend opaque   dark over bright -> %d (expect 20)" % got)
check(got == 20, "opaque did not replace: got %d" % got)

# alpha at 128/256 halfway between old and new
d = fresh(); d[17] = 3; d[29] = 128
fb = GuardedFB(NPIX)
draw(fb, d, flat_lut(200, 200, 200), make_path(w3, d, HLINE))
draw(fb, d, flat_lut(0, 0, 0), make_path(w3, d, HLINE))
got = (fb[16 * W + 16] >> 16) & 255
print("blend alpha    50%% of 200 over 0 -> %d (expect ~100)" % got)
check(abs(got - 100) <= 2, "alpha blend gave %d, expected ~100" % got)

# additive and max are order-independent; opaque is not. This is the
# property that decides which modes need depth sorting.
bright, dark = flat_lut(200, 0, 0), flat_lut(60, 0, 0)
for mode, name, commutes in ((0, "additive", True), (1, "max", True),
                             (2, "opaque", False)):
    outs = []
    for order in ((bright, dark), (dark, bright)):
        d = fresh(); d[17] = mode
        fb = GuardedFB(NPIX)
        for lut in order:
            draw(fb, d, lut, make_path(w3, d, HLINE))
        outs.append((fb[16 * W + 16] >> 16) & 255)
    same = outs[0] == outs[1]
    print("blend %-8s order-independent: %-5s (%s)" % (name, same, outs))
    check(same == commutes,
          "blend %s commutativity is %s, expected %s" % (name, same, commutes))

# ---------------------------------------------------------------- AA
# A 45-degree line is the case anti-aliasing exists for. Without AA every
# lit pixel is full strength; with it, the line spreads onto neighbours.
diag = [(4, 4), (27, 27)]
d = fresh()
fb_plain = GuardedFB(NPIX)
draw(fb_plain, d, grey, make_path(w3, d, diag))
d_aa = fresh(); d_aa[28] = 1
fb_aa = GuardedFB(NPIX)
draw(fb_aa, d_aa, grey, make_path(w3, d_aa, diag), px.draw_path_aa)

print("AA: %d lit plain, %d lit anti-aliased" % (fb_plain.lit(), fb_aa.lit()))
check(fb_aa.lit() >= fb_plain.lit(),
      "AA lit fewer pixels (%d) than plain (%d)" % (fb_aa.lit(), fb_plain.lit()))

# Total emitted light must be roughly conserved -- AA redistributes, it
# does not brighten. (Not exact: weights are integer and a pixel exactly
# on the line takes the whole weight.)
sum_plain = sum((fb_plain[i] >> 16) & 255 for i in range(NPIX))
sum_aa = sum((fb_aa[i] >> 16) & 255 for i in range(NPIX))
ratio = sum_aa / float(sum_plain)
print("AA: total intensity ratio %.2f (expect ~1.0)" % ratio)
check(0.8 <= ratio <= 1.25, "AA changed total intensity by %.0f%%" % ((ratio - 1) * 100))

# select() must route correctly
check(px.select(fresh()) is px.draw_path, "select() picked wrong raster for defaults")
_d = fresh(); _d[28] = 1
check(px.select(_d) is px.draw_path_aa, "select() ignored the AA flag")
_d = fresh(); _d[27] = 3
check(px.select(_d) is px.draw_path_wide, "select() ignored stroke width")
_d = fresh(); _d[27] = 3; _d[28] = 1
check(px.select(_d) is px.draw_path_wide, "width should win over AA")
print("select(): routes defaults/AA/width correctly")

# ---------------------------------------------------------------- width
for thick in (2, 3):
    d = fresh(); d[27] = thick
    fb = GuardedFB(NPIX)
    draw(fb, d, grey, make_path(w3, d, HLINE), px.draw_path_wide)
    col = [y for y in range(H) if fb[y * W + 16]]
    print("width %d: horizontal line occupies rows %s" % (thick, col))
    check(len(col) == thick,
          "width %d produced %d rows: %s" % (thick, len(col), col))

# A vertical line must come out the same width -- thickness is laid
# perpendicular to the major axis, so it must not depend on direction.
for thick in (2, 3):
    d = fresh(); d[27] = thick
    fb = GuardedFB(NPIX)
    draw(fb, d, grey, make_path(w3, d, [(16, 4), (16, 27)]), px.draw_path_wide)
    row = [x for x in range(W) if fb[16 * W + x]]
    check(len(row) == thick,
          "vertical width %d produced %d columns: %s" % (thick, len(row), row))
print("width: vertical lines match horizontal")

# ---------------------------------------------------------------- deep LUT
# With a 1024-entry table the gradient index must span the whole table,
# not wrap at 256. Encode the index in two channels to read it back.
deep = bytearray(1024 * 3)
for i in range(1024):
    deep[i * 3] = i >> 2            # 0..255 across the whole table
    deep[i * 3 + 1] = 255
d = fresh(1024)
check(d[19] == 1023, "new_data(1024) set mask to %d, expected 1023" % d[19])
fb = GuardedFB(NPIX)
draw(fb, d, deep, make_path(w3, d, HLINE))
reds = [(fb[16 * W + x] >> 16) & 255 for x in range(W) if fb[16 * W + x]]
print("deep LUT: red ramps %d -> %d over %d px" % (reds[0], reds[-1], len(reds)))
check(reds[0] < 20 and reds[-1] > 200,
      "deep LUT did not span the table: %d -> %d" % (reds[0], reds[-1]))
check(all(reds[i] <= reds[i + 1] for i in range(len(reds) - 1)),
      "deep LUT gradient is not monotonic")

# ---------------------------------------------------------------- fuzz
# Every raster, random geometry including off-screen and behind-camera,
# against a bounds-checked framebuffer.
from array import array      # noqa: E402

random.seed(11)
for fn, name, size in ((px.draw_path, "draw_path", 256),
                       (px.draw_path_aa, "draw_path_aa", 256),
                       (px.draw_path_wide, "draw_path_wide", 256),
                       (px.draw_path, "draw_path/deep", 1024)):
    lut = flat_lut(120, 200, 90, size)
    d = fresh(size)
    d[27] = 3 if "wide" in name else 1
    d[28] = 1 if "aa" in name else 0
    fb = GuardedFB(NPIX)
    for trial in range(3000):
        n = random.choice((2, 3, 5, 16, 64))
        pts = array('i', [0] * (n * 3))
        for i in range(n):
            for c in range(3):
                pts[i * 3 + c] = int(random.uniform(-75.0, 75.0) * 1024)
        d[9] = n
        d[17] = trial & 3
        d[29] = random.randint(0, 256)
        d[20] = random.randint(0, n - 1)
        d[21] = trial % 3 == 0
        w3.build_path(pts, d)
        fn(fb, lut, d)
    print("%-16s 3000 random paths, no OOB" % name)

print()
if failures:
    for f in failures:
        print("FAIL:", f)
    print("RASTER MODES: %d FAILURES" % len(failures))
    sys.exit(1)
print("RASTER MODES: ALL CHECKS PASSED")
