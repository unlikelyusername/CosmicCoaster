# Gradient interpolation 
# A gradient is a list of stops: (position_0_255, r, g, b)
# Positions ascending, first stop at 0, last at 255.
#
# RGB in, RGB out. Interpolation between two adjacent stops happens in
# HSV so colours stay vivid instead of dipping through grey. Hue takes
# the shortest path; want the long way round, drop in a midpoint stop.

import random


def _rgb_to_hsv(r, g, b):
    mx = r if r > g else g
    if b > mx: mx = b
    mn = r if r < g else g
    if b < mn: mn = b
    v = mx
    if mx == 0:
        return 0.0, 0.0, 0.0
    s = (mx - mn) / mx
    if mx == mn:
        return 0.0, s, v / 255.0
    d = mx - mn
    if mx == r:
        h = (g - b) / d
    elif mx == g:
        h = 2.0 + (b - r) / d
    else:
        h = 4.0 + (r - g) / d
    h = (h * 60.0) % 360.0
    return h, s, v / 255.0


def hsv_to_rgb(h, s, v):
    if s == 0.0:
        c = int(v * 255); return c, c, c
    hi = int(h / 60.0) % 6
    f  = h / 60.0 - int(h / 60.0)
    p  = v * (1.0 - s)
    q  = v * (1.0 - f * s)
    t  = v * (1.0 - (1.0 - f) * s)
    if   hi == 0: r, g, b = v, t, p
    elif hi == 1: r, g, b = q, v, p
    elif hi == 2: r, g, b = p, v, t
    elif hi == 3: r, g, b = p, q, v
    elif hi == 4: r, g, b = t, p, v
    else:         r, g, b = v, p, q
    return int(r * 255), int(g * 255), int(b * 255)


def sample(stops, pos):
    """Return (r, g, b) at position 0-255, interpolating in HSV."""
    if pos <= stops[0][0]:
        return stops[0][1], stops[0][2], stops[0][3]
    if pos >= stops[-1][0]:
        return stops[-1][1], stops[-1][2], stops[-1][3]

    # find the bracketing pair
    for i in range(len(stops) - 1):
        p0 = stops[i][0]
        p1 = stops[i + 1][0]
        if p0 <= pos <= p1:
            break

    span = p1 - p0
    t = (pos - p0) / span if span else 0.0

    h0, s0, v0 = _rgb_to_hsv(stops[i][1],     stops[i][2],     stops[i][3])
    h1, s1, v1 = _rgb_to_hsv(stops[i + 1][1], stops[i + 1][2], stops[i + 1][3])

    # Black, white and grey have no hue — _rgb_to_hsv reports 0.0 because it
    # has to report something — and interpolating toward that nominal zero
    # drags the hue around the wheel on the way: purple fading to black would
    # pass through magenta and red before arriving. An achromatic endpoint
    # borrows the other end's hue instead.
    #
    # Black additionally has no saturation, and that one is not free either:
    # letting s fall to 0 alongside v greys the ramp out, so a purple fade
    # goes chalky before it goes dark. Fading to black should hold hue AND
    # saturation and move only value. Grey and white are the opposite case —
    # there the saturation really is meant to drop, so only hue is borrowed.
    dark0 = v0 == 0.0
    dark1 = v1 == 0.0
    flat0 = dark0 or s0 == 0.0
    flat1 = dark1 or s1 == 0.0
    if flat0 and not flat1:
        h0 = h1
        if dark0:
            s0 = s1
    elif flat1 and not flat0:
        h1 = h0
        if dark1:
            s1 = s0

    # shortest-path hue
    dh = (h1 - h0 + 180.0) % 360.0 - 180.0
    h  = (h0 + dh * t) % 360.0
    s  = s0 + (s1 - s0) * t
    v  = v0 + (v1 - v0) * t
    return hsv_to_rgb(h, s, v)


# ------------------------------------------------------------------ #
# Integer HSV, for baking LUTs.
#
# The float path above is fine for a one-off lookup and pathological for
# filling a table. MicroPython boxes every float on the heap — a measured
# 16 bytes per arithmetic result, against 0 for a small int — so the old
# 256-entry bake allocated 191KB and took 267ms on the Cosmic, stalling
# the panel for six frames every time a palette changed.
#
# None of that float work buys anything. The destination is RGB888: eight
# bits per channel. So hue lives in 0..1535 (256 units per 60-degree
# sector, which makes the sector split a shift and a mask), saturation and
# value in 0..255, and the whole ramp fills in integer arithmetic that
# allocates nothing at all.
#
# Output differs from the float path by at most 1 per channel — checked
# over the full palette space in tests/test_bake.py.
# ------------------------------------------------------------------ #

_HUE_ONE  = 1536     # full turn: 6 sectors x 256
_HUE_HALF = 768
_SV_ONE   = 4080     # saturation/value 1.0 in Q4 (255 << 4)


def _rgb_to_hsv_i(r, g, b):
    """RGB 0-255 -> (h 0-1535, s 0-4080 Q4, v 0-255). No floats.

    Saturation comes out in Q4 rather than whole units because it is about
    to be interpolated: quantising it to 8 bits here and only then ramping
    left the darkest channel of a ramp up to 3 low.
    """
    mx = r if r > g else g
    if b > mx: mx = b
    mn = r if r < g else g
    if b < mn: mn = b
    if mx == 0:
        return 0, 0, 0
    d = mx - mn
    s = (d * _SV_ONE) // mx
    if d == 0:
        return 0, s, mx
    if mx == r:
        h = (256 * (g - b)) // d
    elif mx == g:
        h = 512 + (256 * (b - r)) // d
    else:
        h = 1024 + (256 * (r - g)) // d
    if h < 0:
        h += _HUE_ONE
    return h, s, mx


def bake(stops, lut=None, size=256):
    """Fill a `size`-entry RGB LUT (3 bytes per entry) from a stops list.

    Two things make this fast, and they are independent.

    1. It walks stop PAIRS, not entries. sample() linear-searches the stop
       list and converts BOTH bracketing stops to HSV on every call, so a
       256-entry table over a 3-stop palette cost ~512 HSV conversions to
       produce something needing 3. Here each pair's endpoints and deltas
       are computed once and the whole LUT range between two stops fills
       in one tight loop.
    2. It is integer throughout — see the note above. Nothing in the inner
       loop allocates: no floats, no intermediate tuples, bytes written
       straight into the LUT.

    Positions are carried in Q8 so a deep LUT actually gains resolution;
    at size=256 the arithmetic reduces to pos == index exactly.

    The two flat regions are copied verbatim rather than interpolated,
    matching sample(), which returns the endpoint stop's RGB directly
    outside the stop range — an HSV round trip does not always land back
    on the exact bytes it started from.
    """
    if lut is None:
        lut = bytearray(size * 3)
    n    = len(stops)
    last = size - 1
    lo_q = stops[0][0] << 8
    hi_q = stops[-1][0] << 8
    i = 0
    o = 0

    # Leading flat region: pos <= first stop.
    r, g, b = stops[0][1], stops[0][2], stops[0][3]
    while i < size:
        posq = (i * 65280) // last if last else 0
        if posq > lo_q:
            break
        lut[o] = r; lut[o + 1] = g; lut[o + 2] = b
        i += 1; o += 3

    for k in range(n - 1):
        if i >= size:
            break
        p0q  = stops[k][0] << 8
        p1q  = stops[k + 1][0] << 8
        spanq = p1q - p0q

        # --- pair-invariant: computed once, not per entry ---
        h0, s0, v0 = _rgb_to_hsv_i(stops[k][1],     stops[k][2],     stops[k][3])
        h1, s1, v1 = _rgb_to_hsv_i(stops[k + 1][1], stops[k + 1][2], stops[k + 1][3])
        # Achromatic endpoints borrow hue — and, for black, saturation —
        # from the other end. Same rule as sample(); the long explanation
        # of why lives there.
        dark0 = v0 == 0
        dark1 = v1 == 0
        flat0 = dark0 or s0 == 0
        flat1 = dark1 or s1 == 0
        if flat0 and not flat1:
            h0 = h1
            if dark0:
                s0 = s1
        elif flat1 and not flat0:
            h1 = h0
            if dark1:
                s1 = s0
        dh  = (h1 - h0 + _HUE_HALF) % _HUE_ONE - _HUE_HALF   # shortest path
        # Saturation and value interpolate in Q4, not whole units. A long
        # gentle ramp moves s or v by well under 1/255 per entry, and at
        # integer resolution that truncates to nothing: white fading into a
        # blue over 128 entries held s=0 for the first several, so the ramp
        # started with a flat grey run. Q4 gives 1/16-unit steps, and keeps
        # every product below 2^24 so nothing promotes to a bignum.
        # s is already Q4 from _rgb_to_hsv_i; v is exact, so shifting is free.
        v0q = v0 << 4; v1q = v1 << 4
        dsq = s1 - s0
        dvq = v1q - v0q
        s0q = s0
        # ---

        while i < size:
            posq = (i * 65280) // last if last else 0
            # The trailing guard wins over the last pair, matching sample(),
            # which tests pos >= stops[-1][0] before it searches at all.
            if posq > p1q or posq >= hi_q:
                break
            tq = ((posq - p0q) << 12) // spanq if spanq else 0
            h  = (h0 + ((dh * tq) >> 12)) % _HUE_ONE
            sq = s0q + ((dsq * tq) >> 12)
            vq = v0q + ((dvq * tq) >> 12)
            if sq == 0:
                v = vq >> 4
                lut[o] = v; lut[o + 1] = v; lut[o + 2] = v
            else:
                # sector switch, inlined so nothing builds a tuple
                f  = h & 255
                sc = h >> 8
                p  = (vq * (_SV_ONE - sq)) // _SV_ONE >> 4
                q  = (vq * (_SV_ONE - ((sq * f) >> 8))) // _SV_ONE >> 4
                t  = (vq * (_SV_ONE - ((sq * (256 - f)) >> 8))) // _SV_ONE >> 4
                v  = vq >> 4
                if   sc == 0: lut[o] = v; lut[o + 1] = t; lut[o + 2] = p
                elif sc == 1: lut[o] = q; lut[o + 1] = v; lut[o + 2] = p
                elif sc == 2: lut[o] = p; lut[o + 1] = v; lut[o + 2] = t
                elif sc == 3: lut[o] = p; lut[o + 1] = q; lut[o + 2] = v
                elif sc == 4: lut[o] = t; lut[o + 1] = p; lut[o + 2] = v
                else:         lut[o] = v; lut[o + 1] = p; lut[o + 2] = q
            i += 1; o += 3

    # Trailing flat region: pos >= last stop.
    r, g, b = stops[-1][1], stops[-1][2], stops[-1][3]
    while i < size:
        lut[o] = r; lut[o + 1] = g; lut[o + 2] = b
        i += 1; o += 3
    return lut


# ------------------------------------------------------------------ #
# Procedural palettes — random base hue + a colour-theory scheme.
# Produces a harmonious, seamless-looping gradient every call.
# ------------------------------------------------------------------ #

_SCHEME_OFFSETS = {
    "analogous":     (0, 25, 50, 25),    # neighbours — gentle, moody
    "complementary": (0, 180),           # base <-> opposite
    "triadic":       (0, 120, 240),      # three evenly spaced
    "split":         (0, 150, 210),      # base + two near-opposite
    "tetradic":      (0, 90, 180, 270),  # four-way
}
_SCHEME_NAMES = ("analogous", "complementary", "triadic", "split", "tetradic")


def random_palette(scheme=None):
    """Build a looping gradient from a random base hue + colour scheme.
    Returns (stops, description). stops feed straight into sample()."""
    if scheme is None:
        scheme = _SCHEME_NAMES[random.randint(0, len(_SCHEME_NAMES) - 1)]
    offsets = _SCHEME_OFFSETS[scheme]
    h0 = random.uniform(0.0, 360.0)
    n  = len(offsets)
    stops = []
    first = None
    for i in range(n):
        pos = i * 255 // n
        h   = (h0 + offsets[i]) % 360.0
        s   = random.uniform(0.75, 1.0)
        v   = random.uniform(0.70, 1.0)
        r, g, b = hsv_to_rgb(h, s, v)
        if i == 0:
            first = (r, g, b)
        stops.append((pos, r, g, b))
    # close the loop so g_pos wrapping at 256 has no colour snap
    stops.append((255, first[0], first[1], first[2]))
    return stops, "{} h={}".format(scheme, int(h0))


def solid_lut(r, g, b, lut=None, size=256):
    """A LUT of one colour, for paths that want no gradient at all.

    A solid-coloured line does not need a code path of its own: the
    raster's per-pixel work is a LUT lookup either way, and a flat table
    makes every lookup return the same colour. Filling one costs a few
    microseconds once, against adding a branch to the inner loop forever.
    """
    if lut is None:
        lut = bytearray(size * 3)
    for i in range(0, size * 3, 3):
        lut[i] = r; lut[i + 1] = g; lut[i + 2] = b
    return lut


# ------------------------------------------------------------------ #
# Palette recipes — rules that generate a whole ramp from one base hue.
#
# random_palette() above builds a CYCLE: its last stop repeats the first so
# the gradient can loop. That is right for an effect that scrolls a palette
# and wrong for one that colours a path, where position 0 is the head and
# 255 is the tail. A trail needs a RAMP, and it needs to end dark, because
# the tail is the end that gets retired — a trail whose last stop is bright
# blinks out of existence when its final vertex is dropped.
#
# So every recipe here runs head (0) to tail (255) and ends at black. They
# were prototyped in hyperdrive-sandbox.html, where a palette can be judged
# in a second instead of waiting out a 6-10s timer on the panel.
# ------------------------------------------------------------------ #


def _st(pos, h, sat, val):
    """A stop from HSV, with hue wrapped and s/v clamped."""
    if sat < 0.0: sat = 0.0
    elif sat > 1.0: sat = 1.0
    if val < 0.0: val = 0.0
    elif val > 1.0: val = 1.0
    r, g, b = hsv_to_rgb(h % 360.0, sat, val)
    return (int(pos), r, g, b)


def recipe_white_hot(h):
    """White core, a fast dive into one saturated hue, then a long fade.
    The incandescent look: the head reads as heat rather than as colour."""
    return [(0, 255, 255, 255),
            _st(22, h, 0.30, 1.0),
            _st(58, h, 0.85, 1.0),
            _st(110, h, 1.0, 0.80),
            _st(180, h, 1.0, 0.40),
            _st(255, h, 1.0, 0.0)]


def recipe_ember(h):
    """The old HSV interpolation artifact, on purpose.

    Before the achromatic fix in sample(), fading a colour into black swept
    its hue toward red — black reports hue 0, so the interpolation dragged
    the whole way round — while saturation and value collapsed. Trails
    picked up a warm bloom in the middle and a red ember near the tail, and
    it looked like something physically hot cooling down, because that is
    what hot things do. The fix was still correct: it only fired when an
    endpoint happened to be black, could not be aimed, and brightened the
    tail exactly where it needed to vanish. Here the same sweep is data
    rather than accident, so it can be aimed and it survives the fix.
    """
    n = 7
    out = []
    for i in range(n):
        t = i / (n - 1.0)
        # sweep hue up to 360 (red) as value falls
        out.append(_st(round(t * 255), h + ((360.0 - (h % 360.0)) % 360.0) * t,
                       1.0 - 0.6 * t, 1.0 - t * t))
    return out


def recipe_harmony(h):
    """Colour-theory partners, with brightness FORCED to descend.

    This is random_palette()'s scheme table used as a ramp instead of a
    cycle — the difference that makes it usable on a trail.
    """
    offs = _SCHEME_OFFSETS[_SCHEME_NAMES[random.randint(0, len(_SCHEME_NAMES) - 1)]]
    n = len(offs)
    out = []
    for i in range(n):
        t = i / float(n)
        out.append(_st(round(t * 255), h + offs[i],
                       0.80 + random.uniform(0.0, 0.20), 1.0 - t * 0.75))
    out.append(_st(255, h + offs[n - 1], 1.0, 0.0))
    return out


def recipe_duotone(h):
    """Two contrasting hues meeting mid-trail.

    The most legible recipe at 32x32: a trail is 10-20px, and the eye reads
    a hue CHANGE far more easily than it reads a ramp that short.
    """
    h2 = h + 150.0 + random.uniform(0.0, 60.0)
    return [_st(0, h, 0.55, 1.0),
            _st(60, h, 1.0, 0.95),
            _st(140, h2, 1.0, 0.75),
            _st(200, h2, 1.0, 0.35),
            _st(255, h2, 1.0, 0.0)]


def recipe_blackbody(h):
    """Cool-to-hot regardless of base hue: blue core, amber body, red tail.
    Reads as temperature rather than as a colour choice, so it is the one
    recipe that ignores its argument."""
    return [(0, 255, 255, 255),
            _st(30, 210, 0.45, 1.0),
            _st(90, 45, 0.95, 1.0),
            _st(165, 20, 1.0, 0.65),
            _st(220, 0, 1.0, 0.28),
            _st(255, 0, 1.0, 0.0)]


RECIPES = {
    "white-hot": recipe_white_hot,
    "ember":     recipe_ember,
    "harmony":   recipe_harmony,
    "duotone":   recipe_duotone,
    "blackbody": recipe_blackbody,
}
RECIPE_NAMES = ("white-hot", "ember", "harmony", "duotone", "blackbody")


def random_recipe(name=None, hue=None):
    """Roll a trail ramp. Returns (stops, description), like random_palette."""
    if name is None:
        name = RECIPE_NAMES[random.randint(0, len(RECIPE_NAMES) - 1)]
    if hue is None:
        hue = random.uniform(0.0, 360.0)
    return RECIPES[name](hue), "{} h={}".format(name, int(hue))


# ------------------------------------------------------------------ #
# Example gradients
# ------------------------------------------------------------------ #

WHITE_BLUE_PURPLE = [
    (  0, 255, 255, 255),
    (128, 128, 128, 255),
    (255, 128,   0, 128),
]

FIRE = [
    (  0,   0,   0,   0),
    ( 64, 180,   0,   0),
    (128, 255, 100,   0),
    (200, 255, 255,  80),
    (255, 255, 255, 255),
]

OCEAN = [
    (  0,   0,   0,  30),
    ( 80,   0,  40, 120),
    (160,   0, 160, 220),
    (255, 180, 255, 255),
]
