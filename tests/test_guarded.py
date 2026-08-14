"""Bounds-guard EVERY buffer the effects hand to a viper pass.

On device those casts are raw pointers with no checking, so a single
out-of-range index corrupts the heap and the board hard-faults with USB
dropping off. Here each buffer is wrapped so any read or write outside
its real length raises immediately, naming the buffer and index.

Runs both hyperdrive and wireframe on the real wire3d + surface_rgb888.
"""
import random
import sys
import types

import os
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"
sys.path.insert(0, BASE + "lib")

fake_mp = types.ModuleType('micropython')
fake_mp.const = lambda x: x
fake_mp.viper = lambda f: f
fake_mp.native = lambda f: f
sys.modules['micropython'] = fake_mp

_clock = [0]
faketime = types.ModuleType('time')
faketime.ticks_ms = lambda: _clock[0]
faketime.ticks_add = lambda a, b: a + b
faketime.ticks_diff = lambda a, b: a - b
sys.modules['time'] = faketime

import gradient  # noqa: E402  (real module)

W = H = 32
NPIX = W * H


def _load_module(name, path):
    m = types.ModuleType(name)
    m.__dict__.update({'micropython': fake_mp, 'ptr8': object,
                       'ptr16': object, 'ptr32': object})
    with open(BASE + path) as f:
        exec(f.read(), m.__dict__)
    sys.modules[name] = m
    return m


# trail3d also decorates with the bare @micropython.viper name, so it must
# go through the shim loader too -- a plain `import trail3d` from inside the
# effect would hit the real CPython namespace and fail at decoration time.
t3mod = _load_module('trail3d', 'lib/trail3d.py')
w3mod = _load_module('wire3d', 'lib/wire3d.py')
pxmod = _load_module('surface_rgb888', 'lib/surface_rgb888.py')


class Guard:
    """Wraps an array/bytearray; any OOB or negative index raises."""
    def __init__(self, buf, name):
        self._b = buf
        self._n = name

    def __len__(self):
        return len(self._b)

    def __getitem__(self, i):
        if isinstance(i, int):
            if i < 0 or i >= len(self._b):
                raise AssertionError("%s READ OOB: index %d, len %d"
                                     % (self._n, i, len(self._b)))
        return self._b[i]

    def __setitem__(self, i, v):
        if isinstance(i, int):
            if i < 0 or i >= len(self._b):
                raise AssertionError("%s WRITE OOB: index %d, len %d"
                                     % (self._n, i, len(self._b)))
        self._b[i] = v


class GuardedFB(list):
    """Packed 0x00RRGGBB words, bounds- and range-checked."""
    def __init__(self, n):
        super().__init__([0] * n)

    def __getitem__(self, i):
        assert isinstance(i, int) and 0 <= i < len(self), "fb READ OOB %r" % (i,)
        return list.__getitem__(self, i)

    def __setitem__(self, i, v):
        assert isinstance(i, int) and 0 <= i < len(self), "fb WRITE OOB %r" % (i,)
        assert 0 <= v <= 0xFFFFFF, "fb pixel word out of range %r" % (v,)
        list.__setitem__(self, i, v)


class FakeGraphics:
    def create_pen(self, r, g, b):
        assert 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255
        return (r, g, b)

    def set_pen(self, p):
        pass

    def clear(self):
        pass

    def pixel(self, x, y):
        assert 0 <= x < W and 0 <= y < H, (x, y)


# surface.acquire can't memoryview a fake display, so stub it to hand back
# the guarded framebuffer and the real pixel layer.
FB = GuardedFB(NPIX)
srf = types.ModuleType('surface')
srf.acquire = lambda graphics, w, h: (FB, pxmod)


def _clear(graphics):
    for i in range(NPIX):
        list.__setitem__(FB, i, 0)


srf.clear = _clear
sys.modules['surface'] = srf


def load_effect(name):
    ns = {'__name__': name, 'micropython': fake_mp,
          'ptr8': object, 'ptr16': object, 'ptr32': object}
    with open(BASE + "effects/" + name + ".py") as f:
        exec(f.read(), ns)
    ns['graphics'] = FakeGraphics()
    ns['W'] = W
    ns['H'] = H
    return ns


# ---------------- hyperdrive ----------------
random.seed(4242)
hd = load_effect('hyperdrive')
hd['init']()
for nm in ('_rings', '_heads', '_bright', '_hdr', '_lut', '_w3'):
    hd[nm] = Guard(hd[nm], nm)

print("hyperdrive sizes: rings=%d heads=%d hdr=%d lut=%d w3=%d" % (
    len(hd['_rings']), len(hd['_heads']), len(hd['_hdr']),
    len(hd['_lut']), len(hd['_w3'])))

N = hd['_STAR_COUNT']
TRAIL = hd['_TRAIL_LEN']
zn = hd['_hdr'][3]
zf = hd['_hdr'][4]
dead_total = 0
max_lit = 0
for frame in range(400):
    r = random.random()
    _clock[0] += 100 if r < 0.05 else (12 if r < 0.15 else 33)
    hd['draw']()
    dead_total += hd['_hdr'][7]
    max_lit = max(max_lit, sum(1 for v in FB if v))
    heads = hd['_heads']
    rings = hd['_rings']
    for i in range(N):
        z = rings[(i * TRAIL + heads[i]) * 3 + 2]
        assert zn <= z <= zf + 1, "star %d head z=%d out of volume" % (i, z)
print("hyperdrive: 400 frames, no OOB, max_lit=%d dead_recycled=%d"
      % (max_lit, dead_total))
assert max_lit > 40, "hyperdrive screen nearly empty"
assert dead_total > 100, "no star recycling"
hd['deinit']()
hd['draw']()
print("hyperdrive: deinit + post-deinit draw ok")

# ---------------- wireframe ----------------
random.seed(77)
for i in range(NPIX):
    list.__setitem__(FB, i, 0)
wf = load_effect('wireframe')
wf['init']()
wf['_w3'] = Guard(wf['_w3'], '_w3')
max_lit = 0
for frame in range(1500):
    _clock[0] += 33 if frame % 9 else 90
    wf['draw']()
    max_lit = max(max_lit, sum(1 for v in FB if v))
print("wireframe: 1500 frames, no OOB, max_lit=%d" % max_lit)
assert max_lit > 40, "wireframe screen nearly empty"
wf['deinit']()
wf['draw']()
print("wireframe: deinit + post-deinit draw ok")

print("ALL GUARDED CHECKS PASSED")
