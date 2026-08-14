"""lib/trail3d.py — ring buffers, sliding tails, staggered commits.

The properties that matter here are the ones that were bugs before the
machinery existed:

  * committed history is FROZEN. If anything rewrites old points, trails
    become straight radial lines no matter how the camera turns, and that
    failure is invisible in any single frame.
  * the tail SLIDES rather than jumping, so retiring a slot moves nothing.
  * groups commit at staggered phases, so the whole field never twitches
    at once.
  * the cadence is wall-clock, so a slow frame does not silently drop a
    commit — it catches up.
"""
import os
import sys
from array import array

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import load_module      # noqa: E402

t3 = load_module("trail3d")

N = 16
L = 8
failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
    return cond


def fresh(interval=100.0):
    rings, heads, tails = t3.new_rings(N, L)
    hdr = array('i', bytes(4 * 32))
    t3.init_header(hdr, N, L)
    for i in range(N):
        t3.seed(rings, heads, tails, i, L, i * 1000, 0, 5000)
    return rings, heads, tails, hdr, t3.Cadence(hdr, interval)


# ---------------------------------------------------------------- alloc
check(len(t3.new_rings(N, L)[0]) == N * L * 3, "ring array wrong size")
try:
    t3.new_rings(4, 7)
    failures.append("new_rings accepted a non-power-of-two trail length")
except ValueError:
    pass
print("allocation: sizes correct, non-power-of-two rejected")

# ---------------------------------------------------------------- seed
rings, heads, tails, hdr, cad = fresh()
for i in range(N):
    base = i * L * 3
    pts = {(rings[base + k * 3], rings[base + k * 3 + 1],
            rings[base + k * 3 + 2]) for k in range(L)}
    check(len(pts) == 1, "seed left entity %d with %d distinct points" % (i, len(pts)))
print("seed: every slot collapsed to one position (no phantom history)")

# ---------------------------------------------------------------- frozen history
# Move only the head, commit repeatedly, and confirm the older points keep
# exactly the values they were committed with. This is the property whose
# absence made every trail straight.
rings, heads, tails, hdr, cad = fresh()
written = []
for step in range(L - 1):
    h = heads[0]
    o = (0 * L + h) * 3
    rings[o] = step * 100
    rings[o + 1] = step * 7
    rings[o + 2] = 5000 - step
    written.append((step * 100, step * 7, 5000 - step))
    hdr[t3.H_GROUP] = 0
    t3.commit(rings, heads, tails, hdr)

# Age 0 is the LIVE head, not history: commit copies the head into the new
# head slot, so a fresh head starts life as a copy of the point just
# committed and the effect moves it from there. History therefore begins at
# age 1, and that is the contract, not an off-by-one.
h = heads[0]
for age, want in enumerate(reversed(written), start=1):
    # Walking history means walking the RING: without the mask this runs
    # off the end of entity 0's block and starts reading entity 1's points,
    # which is precisely the class of mistake the mask exists to prevent.
    idx = (0 * L + ((h + age) & (L - 1))) * 3
    got = (rings[idx], rings[idx + 1], rings[idx + 2])
    check(got == want,
          "history slot age %d is %s, was committed as %s" % (age, got, want))
live = (rings[h * 3], rings[h * 3 + 1], rings[h * 3 + 2])
check(live == written[-1],
      "live head is %s, expected a copy of the last commit %s" % (live, written[-1]))
print("frozen history: %d committed points intact, live head is a copy of the last"
      % len(written))

# ---------------------------------------------------------------- sliding tail
# Across one interval the oldest point must travel continuously from its
# snapshot origin toward the second-oldest -- not sit still and then jump.
rings, heads, tails, hdr, cad = fresh()
# Fill the whole ring with DISTINCT positions first. Seeding collapses every
# slot onto one point, so a freshly seeded trail has an oldest and a
# second-oldest that are identical and there is nothing to slide toward --
# which is correct behaviour and useless as a test.
for step in range(L + 1):
    o = (0 * L + heads[0]) * 3
    rings[o] = step * 1000
    rings[o + 1] = 0
    rings[o + 2] = 5000
    hdr[t3.H_GROUP] = 0
    t3.commit(rings, heads, tails, hdr)

positions = []
for slide in range(0, 256, 16):
    hdr[t3.H_SLIDE] = slide
    t3.slide_tail(rings, heads, tails, hdr)
    hh = heads[0]
    old = (0 * L + ((hh + L - 1) & (L - 1))) * 3
    positions.append(rings[old])
steps = [positions[i + 1] - positions[i] for i in range(len(positions) - 1)]
check(all(s >= 0 for s in steps), "tail moved backwards: %s" % steps)
check(max(steps) - min(steps) <= max(abs(max(steps)), 1) * 0.5,
      "tail slide is not smooth, per-step deltas %s" % steps)
check(positions[-1] != positions[0], "tail did not move at all")
print("sliding tail: monotonic, even steps (%d -> %d over one interval)"
      % (positions[0], positions[-1]))

# ---------------------------------------------------------------- stagger
# Over one full interval every group must commit exactly once, and never
# all on the same tick.
rings, heads, tails, hdr, cad = fresh(interval=100.0)
fired = {}
ticks_with_commits = 0
for tick in range(100):
    due = cad.tick(1.0)
    if due:
        ticks_with_commits += 1
    for g in due:
        fired[g] = fired.get(g, 0) + 1
check(set(fired.keys()) == set(range(t3.GROUPS)),
      "not every group committed in one interval: %s" % sorted(fired))
check(all(v == 1 for v in fired.values()),
      "some group committed more than once: %s" % fired)
check(ticks_with_commits >= t3.GROUPS - 1,
      "groups bunched onto %d ticks, expected ~%d" % (ticks_with_commits, t3.GROUPS))
print("stagger: all %d groups fired once, spread over %d ticks"
      % (len(fired), ticks_with_commits))

# ---------------------------------------------------------------- slow frame
# A frame long enough to skip several group marks must still commit all of
# them. Dropping commits here would show up as trails of uneven length.
rings, heads, tails, hdr, cad = fresh(interval=100.0)
due = cad.tick(1.0)
due = cad.tick(60.0)          # jumps most of the way through the interval
check(len(due) >= 4,
      "a 60%% frame committed only %d of 8 groups: %s" % (len(due), due))
print("slow frame: a 60%% interval jump committed %d groups, none dropped"
      % len(due))

# A frame longer than the whole interval must not spin or double-commit.
rings, heads, tails, hdr, cad = fresh(interval=100.0)
due = cad.tick(450.0)
check(len(due) <= t3.GROUPS,
      "a 4.5x interval frame committed %d groups (max %d)" % (len(due), t3.GROUPS))
print("very slow frame: 4.5x interval committed %d groups, no runaway" % len(due))

# ---------------------------------------------------------------- wraparound
# heads walk backwards with wraparound; nothing may index outside the
# entity's own block.
rings, heads, tails, hdr, cad = fresh()
for step in range(L * 3):
    hdr[t3.H_GROUP] = step % t3.GROUPS
    t3.commit(rings, heads, tails, hdr)
    hdr[t3.H_SLIDE] = (step * 37) & 255
    t3.slide_tail(rings, heads, tails, hdr)
check(all(0 <= heads[i] < L for i in range(N)),
      "a head escaped its ring: %s" % list(heads))
print("wraparound: %d commits, every head still in range" % (L * 3))

print()
if failures:
    for f in failures:
        print("FAIL:", f)
    print("TRAIL3D: %d FAILURES" % len(failures))
    sys.exit(1)
print("TRAIL3D: ALL CHECKS PASSED")
