"""gradient.bake() replaced a sample()-per-entry loop that cost 267ms and
191KB of heap for one 256-entry table -- a six-frame stall and a forced GC
sweep every time a palette changed. The replacement is integer throughout,
because the destination is RGB888 and float arithmetic bought nothing but
heap churn (MicroPython boxes every float: 16 bytes per operation).

Being integer, it is no longer bit-exact against the float path, so the
contract is a bounded deviation instead."""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import load_module      # noqa: E402

gradient = load_module("gradient")


def reference(stops, size=256):
    """Exactly what wire3d.bake() used to do."""
    lut = bytearray(size * 3)
    for i in range(size):
        r, g, b = gradient.sample(stops, i)
        lut[i * 3] = r
        lut[i * 3 + 1] = g
        lut[i * 3 + 2] = b
    return lut


TOL = 2          # integer bake may differ from the float path by this much

# Hue pairs close to 180 degrees apart have TWO equally short paths around the
# wheel, so which way a ramp turns is decided by rounding noise -- in the float
# path too, where the stop colours have already been squashed through 8-bit RGB.
# The "complementary" scheme is built on a literal 180-degree offset, so this is
# a whole palette family, not a curiosity. Such palettes are excluded from the
# match-the-float check and get a smoothness check instead: either direction is
# a legitimate ramp, but neither may contain a discontinuity.
ANTIPODAL_DEG = 3.0


def near_antipodal(stops):
    for k in range(len(stops) - 1):
        h0, s0, v0 = gradient._rgb_to_hsv(*stops[k][1:])
        h1, s1, v1 = gradient._rgb_to_hsv(*stops[k + 1][1:])
        if v0 == 0.0 or v1 == 0.0 or s0 == 0.0 or s1 == 0.0:
            continue         # achromatic ends borrow hue; no ambiguity
        sep = abs((h1 - h0 + 180.0) % 360.0 - 180.0)
        if abs(sep - 180.0) < ANTIPODAL_DEG:
            return True
    return False


def max_step(lut, size=256):
    """Largest single-entry channel jump -- a proxy for 'no discontinuity'."""
    return max(abs(lut[i * 3 + c] - lut[(i - 1) * 3 + c])
               for i in range(1, size) for c in range(3))


fails = 0
checked = 0
skipped = 0
worst = 0
hist = {}


def check(stops, label):
    """bake() is integer, sample() is float, so they are not byte-identical.
    Assert the deviation stays within TOL per channel and track the worst."""
    global fails, checked, skipped, worst
    checked += 1
    got = gradient.bake(stops)

    if near_antipodal(stops):
        skipped += 1
        # Direction is arbitrary, but the ramp must still be continuous. The
        # float reference sets the bar: allow the same worst jump it produces.
        js, jf = max_step(got), max_step(reference(stops))
        if js > max(jf, 8) * 2:
            fails += 1
            print("  DISCONTINUOUS %-18s int jump %d vs float %d  stops=%s"
                  % (label, js, jf, stops))
        return

    want = reference(stops)
    bad = 0
    for j in range(768):
        d = abs(want[j] - got[j])
        hist[d] = hist.get(d, 0) + 1
        if d > worst:
            worst = d
        if d > TOL:
            bad += 1
    if bad:
        fails += 1
        if fails <= 5:
            for j in range(768):
                if abs(want[j] - got[j]) > TOL:
                    print("  OVER TOL %-22s entry %3d ch %d: float %d int %d  stops=%s"
                          % (label, j // 3, j % 3, want[j], got[j], stops))
                    break


# named palettes
check(gradient.WHITE_BLUE_PURPLE, "WHITE_BLUE_PURPLE")
check(gradient.FIRE, "FIRE")
check(gradient.OCEAN, "OCEAN")

# every scheme
for scheme in gradient._SCHEME_NAMES:
    for _ in range(40):
        st, d = gradient.random_palette(scheme)
        check(st, "random/" + scheme)

# hyperdrive strips the loop-closing stop
for _ in range(60):
    st, d = gradient.random_palette()
    check(st[:-1], "random/trimmed")

random.seed(1234)

# adversarial: black endpoints, greys, duplicate positions, offset ranges,
# single stops -- every branch of the flat/dark borrowing and both guards
edge = [
    [(0, 0, 0, 0), (255, 255, 255, 255)],
    [(0, 0, 0, 0), (128, 200, 0, 255), (255, 0, 0, 0)],
    [(0, 128, 128, 128), (255, 0, 0, 0)],
    [(0, 255, 255, 255), (255, 128, 128, 128)],
    [(0, 10, 0, 90), (255, 10, 0, 90)],
    [(64, 255, 0, 0), (192, 0, 0, 255)],          # leading + trailing flat
    [(0, 5, 5, 5)],                                # single stop
    [(0, 255, 0, 0), (100, 0, 255, 0), (100, 0, 0, 255), (255, 9, 9, 9)],  # dup pos
    [(0, 0, 0, 0), (1, 255, 255, 255), (255, 0, 0, 0)],  # 1px ramp
    [(0, 0, 0, 0), (254, 0, 0, 0), (255, 255, 255, 255)],
]
for i, st in enumerate(edge):
    check(st, "edge#%d" % i)

# fuzz
for n in range(400):
    k = random.randint(2, 6)
    pos = sorted(random.sample(range(256), k))
    pos[0] = 0 if random.random() < 0.7 else pos[0]
    st = [(p,
           random.choice([0, 0, random.randint(0, 255), 255]),
           random.choice([0, random.randint(0, 255), 255]),
           random.choice([0, random.randint(0, 255), 255])) for p in pos]
    check(st, "fuzz")

total_ch = sum(hist.values())
print("checked %d palettes, %d antipodal (smoothness-only), %d channel values"
      % (checked, skipped, total_ch))
for d in sorted(hist):
    print("   delta %d : %7d  (%5.2f%%)" % (d, hist[d], hist[d] * 100.0 / total_ch))
print("worst deviation: %d   palettes over tolerance: %d" % (worst, fails))

# Deep LUT: entry i covers gradient position i*255/(size-1), so a 1024-entry
# table shares only 4 indices with a 256-entry one (gcd(1023,255)=3). The real
# invariant is that every entry equals sample() at that same position.
mism = 0
for size in (16, 256, 512, 1024):
    for _ in range(8):
        st, d = gradient.random_palette()
        st = st[:-1]
        while near_antipodal(st):      # direction is arbitrary; see above
            st, d = gradient.random_palette()
            st = st[:-1]
        deep = gradient.bake(st, size=size)
        assert len(deep) == size * 3, "wrong LUT length for size=%d" % size
        for i in range(size):
            pos = i * 255.0 / (size - 1)
            want = gradient.sample(st, pos)
            got = (deep[i * 3], deep[i * 3 + 1], deep[i * 3 + 2])
            if max(abs(a - b) for a, b in zip(want, got)) > TOL:
                mism += 1
                if mism < 4:
                    print("  DEEP OVER TOL size=%d i=%d pos=%.4f float=%s int=%s"
                          % (size, i, pos, want, got))
print("deep LUTs: %d entries beyond tolerance (expect 0)" % mism)

# A deep LUT must actually be smoother than a shallow one, not just longer:
# consecutive-entry channel jumps should shrink roughly with the size ratio.
st, _ = gradient.random_palette()
st = st[:-1]


def max_jump(size):
    t = gradient.bake(st, size=size)
    return max(abs(t[i * 3 + c] - t[(i - 1) * 3 + c])
               for i in range(1, size) for c in range(3))


j256, j1024 = max_jump(256), max_jump(1024)
print("max channel step: 256-entry %d -> 1024-entry %d" % (j256, j1024))
assert j1024 <= j256, "deep LUT is not smoother than the shallow one"

print("BAKE TEST", "PASS" if fails == 0 and mism == 0 else "FAIL")
sys.exit(1 if (fails or mism) else 0)
