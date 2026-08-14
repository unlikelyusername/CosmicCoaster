"""Host-side tests for lib/wire3d.py: transform accuracy, raster bounds
fuzz, near-plane clip, ring ordering, arc-length gradient continuity."""
import math
import random
from array import array

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import load_module, load_wire3d, GuardedFB      # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"

w3m, pxm, gradient = load_wire3d()
ns = w3m.__dict__

W = H = 32
NPIX = W * H


data = ns['new_data']()
ns['set_view'](data, W, H)
acc = GuardedFB(NPIX)

# ---- 1. transform vs float reference ----------------------------------
random.seed(7)
for trial in range(2000):
    yaw = random.uniform(-0.15, 0.15)
    pitch = random.uniform(-0.15, 0.15)
    roll = random.uniform(-0.15, 0.15)
    tx, ty, tz = (random.uniform(-5, 5) for _ in range(3))
    x, y, z = (random.uniform(-37, 37) for _ in range(3))
    ns['set_rotation'](data, yaw, pitch, roll)
    ns['set_translation'](data, tx, ty, tz)
    data[9] = 1
    pts = array('i', [int(x * 1024), int(y * 1024), int(z * 1024)])
    out = array('i', [0, 0, 0])
    ns['transform'](pts, out, data)
    # float reference, same rotation order
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    fx = x * cy - z * sy; fz = x * sy + z * cy
    fy = y * cp - fz * sp; fz = y * sp + fz * cp
    gx = fx * cr - fy * sr; gy = fx * sr + fy * cr
    ref = ((gx + tx) * 1024, (gy + ty) * 1024, (fz + tz) * 1024)
    for got, want in zip(out, ref):
        assert abs(got - want) < 24, "transform err %d vs %.1f" % (got, want)
print("transform: 2000 random cases within 24 LSB of float reference")

# ---- 2. raster bounds fuzz --------------------------------------------
lut = bytearray(768)
for i in range(256):
    lut[i*3] = i; lut[i*3+1] = 255 - i; lut[i*3+2] = 128
data[16] = 256
data[18] = 0
random.seed(42)
for trial in range(8000):
    n = random.choice((2, 3, 5, 12, 16, 64))
    m = random.choice((2.0, 40.0, 75.0))
    pts = array('i', [0] * (n * 3))
    for i in range(n):
        pts[i*3]   = int(random.uniform(-m, m) * 1024)
        pts[i*3+1] = int(random.uniform(-m, m) * 1024)
        pts[i*3+2] = int(random.uniform(-m, m) * 1024)   # incl. z behind camera
    data[9] = n
    data[17] = trial & 1
    data[20] = random.randint(0, n - 1)
    data[21] = 1 if trial % 3 == 0 else 0
    data[25] = 0
    ns['polyline'](acc, pts, lut, data)
print("polyline fuzz: 8000 random polylines, no OOB writes")

# ---- 3. near-plane clip sanity ----------------------------------------
acc.zero()
data[9] = 2; data[17] = 0; data[20] = 0; data[21] = 0; data[25] = 0
pts = array('i', [0, 0, -5 * 1024,   0, 0, 5 * 1024])  # through the camera
ns['polyline'](acc, pts, lut, data)
assert any(acc), "through-camera segment drew nothing"
acc.zero()
pts = array('i', [1024, 1024, -5 * 1024,  -1024, 512, -2 * 1024])  # fully behind
ns['polyline'](acc, pts, lut, data)
assert not any(acc), "fully-behind segment drew pixels"
print("near clip: through-camera draws, fully-behind draws nothing")

# ---- 4. arc-length gradient continuity --------------------------------
# Horizontal 3-point polyline across the screen at fixed z: red channel of
# the LUT is the gradient index, so drawn reds must ramp 0 -> ~255
# monotonically left to right.
acc.zero()
z = 1024   # z=1.0: focal=12, cx=16 -> x of +/-1.25 units spans px 1..31
pts = array('i', [int(-1.25*1024), 0, z,  0, 0, z,  int(1.25*1024), 0, z])
data[9] = 3
ns['polyline'](acc, pts, lut, data)
row = 16 * W
reds = [(x, (acc[row + x] >> 16) & 255) for x in range(W) if acc[row + x]]
assert len(reds) > 20, "gradient line too short: %d px" % len(reds)
vals = [r for _, r in reds]
assert vals[0] < 30 and vals[-1] > 220, "gradient endpoints wrong: %d..%d" % (vals[0], vals[-1])
assert all(b >= a for a, b in zip(vals, vals[1:])), "gradient not monotonic: %r" % vals
print("arc-length gradient: %d px, monotonic %d -> %d across two segments" %
      (len(reds), vals[0], vals[-1]))

# ---- 5. ring ordering --------------------------------------------------
# A ring whose physical slots are shuffled must draw the same pixels as
# the same path laid out linearly, when start points at the logical head.
path = [(-1.0 + 0.15 * i, -0.5 + 0.08 * i, 4.0) for i in range(16)]
lin = array('i', [0] * 48)
for i, (x, y, zz) in enumerate(path):
    lin[i*3], lin[i*3+1], lin[i*3+2] = int(x*1024), int(y*1024), int(zz*1024)
acc.zero()
data[9] = 16; data[20] = 0; data[21] = 0
ns['polyline'](acc, lin, lut, data)
ref_img = list(acc)
for shift in (1, 5, 15):
    ring = array('i', [0] * 48)
    for i in range(16):
        phys = (shift + i) % 16
        ring[phys*3], ring[phys*3+1], ring[phys*3+2] = lin[i*3], lin[i*3+1], lin[i*3+2]
    acc.zero()
    data[20] = shift
    ns['polyline'](acc, ring, lut, data)
    assert list(acc) == ref_img, "ring shift %d mismatch" % shift
data[20] = 0
print("ring ordering: shifted rings identical to linear path")

# ---- 6. base offset ----------------------------------------------------
big = array('i', [0] * (48 * 3))
for i in range(48):
    big[(96 + i) if False else 0] = 0  # noqa placeholder
packed = array('i', bytes(4 * 3 * 48))
for i in range(16):
    packed[(32 + i) * 3] = lin[i*3]
    packed[(32 + i) * 3 + 1] = lin[i*3+1]
    packed[(32 + i) * 3 + 2] = lin[i*3+2]
acc.zero()
data[9] = 16; data[25] = 32
ns['polyline'](acc, packed, lut, data)
assert list(acc) == ref_img, "base offset mismatch"
data[25] = 0
print("base offset: path at offset 32 identical to path at 0")

# ---- 7. bake() ---------------------------------------------------------
fire = ns['bake'](gradient.FIRE)
assert len(fire) == 768
assert fire[0] == 0 and fire[765] == 255, "FIRE endpoints wrong"
print("bake: FIRE LUT endpoints correct")

print("ALL WIRE3D CHECKS PASSED")
