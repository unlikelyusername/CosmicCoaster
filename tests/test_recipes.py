"""Palette recipes must produce ramps a trail can actually wear.

A recipe is not just "some colours". It colours a path whose tail is
continuously retired, so it has hard structural requirements that
random_palette() — which builds a repeating cycle — does not meet:

  * stop positions ascend, start at 0 and end at 255, or bake() will
    mis-fill the LUT
  * channels stay in 0..255
  * the tail is dark, so a vertex being dropped does not blink
  * brightness generally descends, so the head reads as the leading end

These are checked over many random base hues because the recipes contain
randomness of their own, and a rule that holds for one hue and fails for
another is exactly the kind of thing that only shows up on the panel at
three in the morning.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import load_module      # noqa: E402

gradient = load_module("gradient")

N_HUES = 200
failures = []


def fail(msg):
    failures.append(msg)


def luma(stop):
    return 0.299 * stop[1] + 0.587 * stop[2] + 0.114 * stop[3]


def value(stop):
    """HSV value, i.e. the brightest channel.

    The head is checked with this rather than with luma. Luma weights green
    at 0.587 and blue at 0.114, so a fully saturated blue at value 1.0 has a
    luma of 29 while a fully saturated green has 150 — both are as bright as
    that hue can be, and demanding a luma floor would just be demanding that
    no recipe ever picks blue. That a saturated-hue head is dimmer than a
    white one is real, and it is why 'white-hot' and 'blackbody' start white:
    a design difference between recipes, not a defect in either.
    """
    return max(stop[1], stop[2], stop[3])


for name in gradient.RECIPE_NAMES:
    tails = []
    heads = []
    for k in range(N_HUES):
        hue = k * 360.0 / N_HUES
        stops, desc = gradient.random_recipe(name, hue)

        if len(stops) < 2:
            fail("%s: only %d stops" % (name, len(stops)))
            continue

        pos = [s[0] for s in stops]
        if pos[0] != 0:
            fail("%s h=%.0f: first stop at %d, not 0" % (name, hue, pos[0]))
        if pos[-1] != 255:
            fail("%s h=%.0f: last stop at %d, not 255" % (name, hue, pos[-1]))
        if any(pos[i] > pos[i + 1] for i in range(len(pos) - 1)):
            fail("%s h=%.0f: stop positions not ascending: %s" % (name, hue, pos))

        for s in stops:
            for c in s[1:]:
                if not (0 <= c <= 255):
                    fail("%s h=%.0f: channel out of range in %s" % (name, hue, s))
                    break

        tails.append(luma(stops[-1]))
        heads.append(value(stops[0]))

        # bake() must accept it and produce a dark tail in the LUT too --
        # the stops being right does not guarantee the ramp is.
        lut = gradient.bake(stops)
        tail_px = max(lut[-3:])
        if tail_px > 8:
            fail("%s h=%.0f: baked tail is not dark (max channel %d)"
                 % (name, hue, tail_px))

    if tails and max(tails) > 4.0:
        fail("%s: tail not black across hues (worst luma %.1f)" % (name, max(tails)))
    if heads and min(heads) < 200:
        fail("%s: head is not at full value (worst %d of 255)"
             % (name, min(heads)))
    print("%-10s %d hues: head value %d-%d, tail luma %.1f"
          % (name, N_HUES, min(heads), max(heads), max(tails)))

# Overall brightness trend: sample the baked ramp and check it descends.
# Not monotonically -- 'harmony' and 'duotone' deliberately bloom in the
# middle -- but the last quarter must be clearly darker than the first.
print()
for name in gradient.RECIPE_NAMES:
    worst = None
    for k in range(40):
        stops, _d = gradient.random_recipe(name, k * 9.0)
        lut = gradient.bake(stops)
        head = sum(lut[0:64 * 3]) / (64.0 * 3)
        tail = sum(lut[192 * 3:]) / (64.0 * 3)
        ratio = tail / max(head, 1.0)
        if worst is None or ratio > worst:
            worst = ratio
    print("%-10s tail/head brightness %.2f" % (name, worst))
    if worst > 0.55:
        fail("%s: tail is not meaningfully darker than head (%.2f)" % (name, worst))

# random_recipe with no arguments must roll every recipe eventually and
# never return something bake() chokes on.
seen = set()
for _ in range(400):
    stops, desc = gradient.random_recipe()
    seen.add(desc.split()[0])
    gradient.bake(stops)
missing = set(gradient.RECIPE_NAMES) - seen
if missing:
    fail("random_recipe() never rolled: %s" % sorted(missing))
print("\nrandom_recipe() rolled all %d recipes in 400 draws" % len(seen))

print()
if failures:
    for f in failures[:20]:
        print("FAIL:", f)
    if len(failures) > 20:
        print("... and %d more" % (len(failures) - 20))
    print("RECIPES: %d FAILURES" % len(failures))
    sys.exit(1)
print("RECIPES: ALL CHECKS PASSED")
