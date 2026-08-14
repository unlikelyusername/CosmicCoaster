"""Load the on-device libraries into CPython so they can be tested on a host.

The libraries are written for MicroPython: they decorate functions with
@micropython.viper, annotate pointer types, and use const(). None of that
exists in CPython, so this module fakes it — viper and native become
identity decorators, const() returns its argument, and the pointer types
become bare objects that the annotations never actually consult.

Why bother: a viper function has no bounds checking. An out-of-range index
does not raise, it writes into the MicroPython heap, and the board answers
with a hard fault that takes the USB device down with it. Running the same
code in CPython against buffers that DO check bounds turns that class of
bug into a stack trace, on a machine where a stack trace is cheap. That is
the whole point of these tests, and it has already caught real defects.

What this cannot tell you: anything about speed, anything about how it
looks, and anything about viper's own code generation — the one failure
that cost this project five board lockups was a codegen problem that host
simulation reproduced perfectly happily and never flagged.
"""
import os
import sys
import types
from array import array

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(BASE, "lib")

if LIB not in sys.path:
    sys.path.insert(0, LIB)

_fake_mp = types.ModuleType("micropython")
_fake_mp.const = lambda x: x
_fake_mp.viper = lambda f: f
_fake_mp.native = lambda f: f
sys.modules["micropython"] = _fake_mp

_PTR_NAMES = {"micropython": _fake_mp, "ptr8": object,
              "ptr16": object, "ptr32": object}


def load_module(name):
    """exec lib/<name>.py with the viper builtins pre-injected."""
    if name in sys.modules and getattr(sys.modules[name], "_harness_loaded", False):
        return sys.modules[name]
    mod = types.ModuleType(name)
    mod.__dict__.update(_PTR_NAMES)
    mod._harness_loaded = True
    with open(os.path.join(LIB, name + ".py")) as f:
        exec(compile(f.read(), os.path.join(LIB, name + ".py"), "exec"), mod.__dict__)
    sys.modules[name] = mod
    return mod


class GuardedFB(list):
    """A framebuffer of packed 0x00RRGGBB words that refuses to be abused.

    Every read and write is range-checked and every stored value is checked
    for shape. On device these accesses are raw pointer arithmetic with no
    checks at all, so this is the only place a stray index shows itself as
    something other than a dead board.
    """

    def __init__(self, n):
        super().__init__([0] * n)

    def __getitem__(self, i):
        assert isinstance(i, int) and 0 <= i < len(self), "READ OOB %r" % (i,)
        return list.__getitem__(self, i)

    def __setitem__(self, i, v):
        assert isinstance(i, int) and 0 <= i < len(self), "WRITE OOB %r" % (i,)
        assert 0 <= v <= 0xFFFFFF, "pixel word out of range %r" % (v,)
        list.__setitem__(self, i, v)

    def zero(self):
        for i in range(len(self)):
            list.__setitem__(self, i, 0)

    def lit(self):
        return sum(1 for v in self if v)


class GuardedArray:
    """Bounds-checked stand-in for any array('i'/'b') handed to a viper pass."""

    def __init__(self, inner, name="buffer"):
        self._a = inner
        self._name = name

    def __len__(self):
        return len(self._a)

    def __getitem__(self, i):
        assert isinstance(i, int) and 0 <= i < len(self._a), \
            "%s READ OOB %r (len %d)" % (self._name, i, len(self._a))
        return self._a[i]

    def __setitem__(self, i, v):
        assert isinstance(i, int) and 0 <= i < len(self._a), \
            "%s WRITE OOB %r (len %d)" % (self._name, i, len(self._a))
        self._a[i] = v


def load_wire3d():
    """Return (wire3d, surface_rgb888) plus a combined polyline() helper.

    build_path and draw_path were one function before the Sprint 1 split;
    tests written against the old contract still want them as a pair.
    """
    gradient = load_module("gradient")
    w3 = load_module("wire3d")
    px = load_module("surface_rgb888")

    def polyline(fb, pts, lut, data):
        w3.build_path(pts, data)
        px.draw_path(fb, lut, data)

    w3.polyline = polyline
    w3.clear = lambda fb, data: None
    return w3, px, gradient


def make_path(w3, data, screen_points, z=1024, focal=12, centre=16):
    """Build an array of 3D points that project to the given screen pixels.

    Projection is sx = (focal * x) // z + centre with x in Q10, so a screen
    offset of n pixels needs x = n * z / focal. Tests that care about exact
    pixel geometry go through here rather than hand-computing world units.
    """
    pts = array('i', [0] * (len(screen_points) * 3))
    for i, (sx, sy) in enumerate(screen_points):
        pts[i * 3] = ((sx - centre) * z + focal // 2) // focal
        pts[i * 3 + 1] = ((sy - centre) * z + focal // 2) // focal
        pts[i * 3 + 2] = z
    return pts
