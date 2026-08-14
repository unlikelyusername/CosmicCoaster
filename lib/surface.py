# /lib/surface.py
#
# Picks the pixel layer that matches the display's framebuffer format and
# hands back a writable view of it, so effects never contain
# device-specific framebuffer code.
#
# Detection is by bytes-per-pixel, which is the only thing that actually
# determines which raster functions are valid:
#     4 bytes/px -> surface_rgb888  (PicoGraphics RGB888, 0x00RRGGBB)
#     2 bytes/px -> surface_rgb565  (not written yet)
# Anything else, or a display with no buffer protocol at all, yields
# (None, None) and the caller falls back to the pen API.
#
# Device *flush* (unicorn.update() vs presto.update()) is a separate axis
# and stays in board.py — a Presto and a Cosmic can share a pixel layer
# while differing in how a finished frame is pushed.

import wire3d  # noqa: F401  (pixel layers import SEGS from it)


def acquire(graphics, width, height):
    """Return (framebuffer_memoryview, pixel_layer_module).

    (None, None) if the display exposes no framebuffer this module knows
    how to write, in which case the caller must use the pen API.
    """
    npix = width * height
    try:
        mv = memoryview(graphics)
    except TypeError:
        return None, None

    n = len(mv)
    if n == npix * 4:
        import surface_rgb888
        return mv, surface_rgb888
    if n == npix * 2:
        try:
            import surface_rgb565
        except ImportError:
            print("[surface] RGB565 display but surface_rgb565.py is missing")
            return None, None
        return mv, surface_rgb565

    print("[surface] unrecognised framebuffer: {} bytes for {} px".format(n, npix))
    return None, None


def clear(graphics):
    """Blank the frame. PicoGraphics' own clear is a C memset, so it beats
    any viper loop and is format-agnostic."""
    graphics.set_pen(graphics.create_pen(0, 0, 0))
    graphics.clear()
