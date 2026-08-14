# lib/board.py
#
# Board abstraction for the Pimoroni Unicorn family.
#
# Selects the hardware driver from config.json's "model" key and exposes a
# uniform surface, so nothing else in the tree names a specific board:
#
#   unicorn    the board object (CosmicUnicorn / GalacticUnicorn / StellarUnicorn)
#   graphics   PicoGraphics bound to the right display constant
#   WIDTH      display width  in pixels
#   HEIGHT     display height in pixels
#   MODEL      "cosmic" | "galactic" | "stellar"
#   SWITCH_*   button constants (identical names across all three boards)
#
# Importing this module brings the display up. That is deliberate: it lets
# effect development drive the panel straight from the REPL without main.py's
# boot sequence running —
#
#   from board import graphics, unicorn
#   graphics.set_pen(graphics.create_pen(255, 0, 0)); graphics.clear()
#   unicorn.update(graphics)

import json

_DEFAULT_MODEL = "cosmic"

# Fallback dimensions, used only if the firmware class lacks WIDTH/HEIGHT.
_SIZES = {
    "cosmic":   (32, 32),
    "galactic": (53, 11),
    "stellar":  (16, 16),
}


def _read_model():
    try:
        with open("config.json") as f:
            model = json.load(f).get("model")
    except (OSError, ValueError) as e:
        print("[board] config.json unreadable ({}) — assuming {}".format(
            e, _DEFAULT_MODEL))
        return _DEFAULT_MODEL

    if not model:
        return _DEFAULT_MODEL

    model = str(model).strip().lower()
    if model not in _SIZES:
        print("[board] unknown model {!r} — assuming {}".format(
            model, _DEFAULT_MODEL))
        return _DEFAULT_MODEL
    return model


MODEL = _read_model()

if MODEL == "galactic":
    from galactic import GalacticUnicorn as _Unicorn
    from picographics import PicoGraphics, DISPLAY_GALACTIC_UNICORN as _DISPLAY
elif MODEL == "stellar":
    from stellar import StellarUnicorn as _Unicorn
    from picographics import PicoGraphics, DISPLAY_STELLAR_UNICORN as _DISPLAY
else:
    from cosmic import CosmicUnicorn as _Unicorn
    from picographics import PicoGraphics, DISPLAY_COSMIC_UNICORN as _DISPLAY

# Prefer the firmware's own constants over the table above — they are
# authoritative, and disagreeing with them would be a silent buffer-size bug.
WIDTH  = getattr(_Unicorn, "WIDTH",  _SIZES[MODEL][0])
HEIGHT = getattr(_Unicorn, "HEIGHT", _SIZES[MODEL][1])

unicorn  = _Unicorn()
graphics = PicoGraphics(_DISPLAY)

# Switch constants — same names on all three boards, so callers never branch.
SWITCH_A = _Unicorn.SWITCH_A
SWITCH_B = _Unicorn.SWITCH_B
SWITCH_C = _Unicorn.SWITCH_C
SWITCH_D = _Unicorn.SWITCH_D
SWITCH_SLEEP           = _Unicorn.SWITCH_SLEEP
SWITCH_VOLUME_UP       = _Unicorn.SWITCH_VOLUME_UP
SWITCH_VOLUME_DOWN     = _Unicorn.SWITCH_VOLUME_DOWN
SWITCH_BRIGHTNESS_UP   = _Unicorn.SWITCH_BRIGHTNESS_UP
SWITCH_BRIGHTNESS_DOWN = _Unicorn.SWITCH_BRIGHTNESS_DOWN


def is_pressed(switch):
    return unicorn.is_pressed(switch)


def set_brightness(value):
    unicorn.set_brightness(value)


def update():
    unicorn.update(graphics)


print("[board] {} {}x{}".format(MODEL, WIDTH, HEIGHT))
