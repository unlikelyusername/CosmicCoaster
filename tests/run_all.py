"""Run every host test. `python3 tests/run_all.py` before any deploy.

These run the REAL lib/ modules under CPython with viper faked out, against
buffers that check their own bounds. On device those buffers are raw
pointers: an out-of-range index does not raise, it corrupts the heap, and
the board answers with a hard fault that drops the USB device. Here the
same mistake is a stack trace.

They cannot tell you about speed, about how anything looks, or about
viper's code generation — the crash that cost this project five board
lockups was a codegen fault that these tests reproduced happily and never
flagged. Green here means "not obviously broken", not "ready to ship".

Each test is a standalone script that exits non-zero on failure, so they
can also be run individually while working on one of them.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

TESTS = [
    ("test_viper_decorators.py",
     "every pointer-taking function is actually @micropython.viper compiled"),
    ("test_wire3d.py",
     "geometry: transform vs float, 8000-polyline raster fuzz, near clip, "
     "ring ordering, base offset"),
    ("test_distance_gradient.py",
     "the gradient is parameterised by distance, not pixel count"),
    ("test_recipes.py",
     "palette recipes produce ramps a trail can wear: dark tail, ascending stops"),
    ("test_raster_modes.py",
     "blend modes, anti-aliasing, stroke width, deep LUT, all bounds-checked"),
    ("test_bake.py",
     "integer LUT baking stays within tolerance of the float reference"),
    ("test_trail3d.py",
     "ring buffers: frozen history, sliding tail, staggered commits, cadence"),
    ("test_chunking.py",
     "paths longer than one data array, with a gradient continuous across chunks"),
    ("test_guarded.py",
     "both effects, every buffer bounds-checked, hundreds of frames"),
]


def main():
    width = max(len(t) for t, _ in TESTS)
    failed = []
    for name, blurb in TESTS:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            print("%-*s  MISSING" % (width, name))
            failed.append(name)
            continue
        r = subprocess.run([sys.executable, path],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        print("%-*s  %s   %s" % (width, name, "ok  " if ok else "FAIL", blurb))
        if not ok:
            failed.append(name)
            out = (r.stdout + r.stderr).strip().splitlines()
            for line in out[-25:]:
                print("      | " + line)

    print()
    if failed:
        print("%d of %d FAILED: %s" % (len(failed), len(TESTS), ", ".join(failed)))
        return 1
    print("all %d host tests passed" % len(TESTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
