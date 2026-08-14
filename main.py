"""
main.py — Unicorn effect player

One image for every board. The model is selected by config.json's "model" key
and resolved in lib/board.py; nothing here names a specific Unicorn.

Boot sequence:
  1. read config.json
  2. bring the board up (lib/board)
  3. connect WiFi and set the RTC from NTP (lib/wifi), then release the radio
  4. discover /effects, load the first one
  5. loop — button A advances, brightness buttons adjust

EFFECT FILE CONTRACT — each /effects/*.py defines module-level:

    GEOMETRY = "any"   # "cosmic" | "galactic" | "stellar" | "square" | "any"
    graphics = None    # injected by the loader
    cu       = None    # optional; injected if present
    W        = 0       # optional; injected with the display width
    H        = 0       # optional; injected with the display height
    init(); draw(); deinit()

GEOMETRY declares what the effect needs to render correctly:

    "any"       runs on any board
    "square"    needs a roughly 1:1 canvas (Cosmic, Stellar)
    "cosmic" /  locked to that board — usually a hardcoded dimension the
    "galactic"  effect cannot be separated from (power-of-two masks, fixed
    "stellar"   geometry, or detail that vanishes at lower resolution)

A filename may carry the same fact as a suffix — glitch_cosmic.py — which lets
the loader skip incompatible effects without opening them. GEOMETRY is the
authoritative declaration and is checked against the suffix at load time.
"""

import gc
import os
import time
import json

import board

# ------------------------------------------------------------------ #
# CONFIG
# ------------------------------------------------------------------ #

try:
    with open("config.json") as f:
        _cfg = json.load(f)
except (OSError, ValueError) as e:
    print("[boot] config.json unreadable:", e)
    _cfg = {}

DEFAULT_BRIGHTNESS = _cfg.get("brightness", 0.5)
EFFECTS_DIR = "/effects"

# Suffix → the models a suffixed effect will run on.
_GEOMETRY_MODELS = {
    "cosmic":   ("cosmic",),
    "galactic": ("galactic",),
    "stellar":  ("stellar",),
    "square":   ("cosmic", "stellar"),
}


# ------------------------------------------------------------------ #
# HARDWARE
# ------------------------------------------------------------------ #

graphics = board.graphics
unicorn  = board.unicorn
WIDTH    = board.WIDTH
HEIGHT   = board.HEIGHT

brightness = DEFAULT_BRIGHTNESS
unicorn.set_brightness(brightness)


def _status_screen(line1, line2, r, g, b):
    """Two-line status indicator, laid out for the actual display height."""
    graphics.set_font("bitmap6")
    graphics.set_pen(graphics.create_pen(0, 0, 0))
    graphics.clear()
    graphics.set_pen(graphics.create_pen(r, g, b))

    if HEIGHT >= 24:
        # Room for two stacked lines, vertically centred.
        graphics.text(line1, 1, HEIGHT // 2 - 9, WIDTH, 1)
        if line2:
            graphics.text(line2, 1, HEIGHT // 2 + 2, WIDTH, 1)
    else:
        # Short panel (Galactic is 11px): one line only. Join with a space so
        # a wide display shows both words and a narrow one clips gracefully.
        text = "{} {}".format(line1, line2) if line2 else line1
        graphics.text(text, 1, max(0, HEIGHT // 2 - 3), WIDTH, 1)

    unicorn.update(graphics)


# ------------------------------------------------------------------ #
# EFFECT LOADER
# ------------------------------------------------------------------ #

effect_ns = None


def _suffix_geometry(name):
    """Geometry declared by a filename suffix, or 'any' if unsuffixed."""
    idx = name.rfind("_")
    if idx > 0:
        candidate = name[idx + 1:]
        if candidate in _GEOMETRY_MODELS:
            return candidate
    return "any"


def _runs_here(geometry):
    if geometry == "any":
        return True
    return board.MODEL in _GEOMETRY_MODELS.get(geometry, ())


def discover_effects():
    """Effect names this board can run, sorted. Cheap — never opens a file."""
    try:
        files = os.listdir(EFFECTS_DIR)
    except OSError:
        return []

    names = []
    for f in files:
        if not f.endswith(".py") or f.startswith("_"):
            continue
        name = f[:-3]
        if _runs_here(_suffix_geometry(name)):
            names.append(name)
    return sorted(names)


def load_effect(name):
    """Tear down the current effect, load `name` into a fresh namespace."""
    global effect_ns

    if effect_ns is not None:
        if 'deinit' in effect_ns:
            try:
                effect_ns['deinit']()
            except Exception as e:
                print("[loader] deinit error:", e)
        effect_ns = None

    gc.collect()
    gc.collect()

    ns = {'__name__': name}
    try:
        with open(EFFECTS_DIR + '/' + name + '.py') as f:
            code = f.read()
        exec(code, ns)

        # GEOMETRY is authoritative; the filename suffix is only a pre-filter.
        # A disagreement means a rename went wrong — fail loudly rather than
        # render garbage on a board the effect was never written for.
        declared = ns.get('GEOMETRY', 'any')
        suffix   = _suffix_geometry(name)
        if declared != suffix:
            print("[loader] {}: GEOMETRY={!r} contradicts filename ({!r})".format(
                name, declared, suffix))
            return None
        if not _runs_here(declared):
            print("[loader] {}: needs {!r}, board is {!r}".format(
                name, declared, board.MODEL))
            return None

        ns['graphics'] = graphics
        if 'cu' in ns:
            ns['cu'] = unicorn
        if 'W' in ns:
            ns['W'] = WIDTH
        if 'H' in ns:
            ns['H'] = HEIGHT

        ns['init']()
        effect_ns = ns
        print("[loader] loaded:", name)
        return ns
    except Exception as e:
        print("[loader] error loading {}: {}".format(name, e))
        effect_ns = None
        return None


# ------------------------------------------------------------------ #
# BOOT
# ------------------------------------------------------------------ #

try:
    import wifi
    _status_screen("WIFI", "SYNC", 0, 80, 160)
    wifi.connect_and_sync(_cfg)
except Exception as e:
    print("[boot] clock sync skipped:", e)

effects = discover_effects()

if not effects:
    _status_screen("NO", "EFFECTS", 200, 0, 0)
    while True:
        time.sleep(1)

print("[boot] {} effects for {}".format(len(effects), board.MODEL))

current_index = 0
effect_ns = load_effect(effects[current_index])


# ------------------------------------------------------------------ #
# MAIN LOOP
# ------------------------------------------------------------------ #

def _read_brightness():
    global brightness
    if unicorn.is_pressed(board.SWITCH_BRIGHTNESS_UP):
        brightness = min(brightness + 0.01, 1.0)
    if unicorn.is_pressed(board.SWITCH_BRIGHTNESS_DOWN):
        brightness = max(brightness - 0.01, 0.0)


prev_a = False
while True:
    a = unicorn.is_pressed(board.SWITCH_A)
    if a and not prev_a:
        current_index = (current_index + 1) % len(effects)
        effect_ns = load_effect(effects[current_index])
    prev_a = a

    _read_brightness()
    unicorn.set_brightness(brightness)

    if effect_ns is not None:
        effect_ns['draw']()

    unicorn.update(graphics)
    time.sleep_ms(1)
