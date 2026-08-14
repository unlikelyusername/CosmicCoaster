# /effects/clock_cosmic.py
#
# GEOMETRY = "cosmic" — locked to the 32x32 Cosmic Unicorn.
#
# Not portable, and not worth making portable: the clock face carries fine
# detail (_HR_LEN = 7 hour hand, sky horizon at y=27) that is illegible below
# 32px, and the layout is radial so a letterbox panel cannot show it either.
# It also imports `cosmic` directly for the audio channel.
#
# The loader skips this file on any other board without opening it, on the
# strength of the _cosmic filename suffix.
#
# Animated analog clock with day/night sky, sun + moon arcs,
# eclipse simulation, blood moon, and twinkling stars.
#
# Port of the 32x32 LED Matrix Clock browser simulation.
# Optimized for RP2040: integer inner loop, sin LUT, precomputed
# glow map and marker positions, per-minute gradient rebuild.
#
# RTC must be set before this effect loads.
# NTP sync: planned future feature (see requirements doc v0.3+).
#
# Effect contract:
#   graphics = None     set by loader before init()
#   cu       = None     set by loader if present — used for audio
#   init()              called once on load
#   draw()              called every frame

GEOMETRY = "cosmic"

import math
import time
import machine
from micropython import const

graphics = None

# ====================================================================
# Geometry
# ====================================================================

_CX = const(16); _CY = const(16)
_CLOCK_R = const(14)
_MIN_LEN  = const(11)
_HR_LEN   = const(7)

_LATITUDE = 39.1

_SKY_HY = const(27)
_SKY_TY = const(2)
_SKY_LX = const(2)
_SKY_RX = const(29)

_SUN_R  = const(3)
_MOON_R = const(2)

# ====================================================================
# Time period boundaries (hours)
# ====================================================================
_PH = (6, 12, 17, 20)

# ====================================================================
# Wave animation
# ====================================================================
# _WTX/_WTY: ms per LUT step — lower = faster
# _WAM: amplitude scale — higher = more displacement
# At _WTX=8, _WAM=14: full cycle ~2s, max displacement ~7px
_WX  = const(9)
_WY  = const(6)
_WTX = const(8)
_WTY = const(12)
_WAM = const(14)

# ====================================================================
# Glow tier multipliers * 256
# ====================================================================
_TM = (192, 230, 282, 346)

# ====================================================================
# FG type codes
# ====================================================================
_CLR  = const(0)
_HAND = const(1)
_MRK  = const(2)
_CARD = const(3)
_CTR  = const(4)
_GLOW = const(5)
_STAR = const(6)
_SCOR = const(16)
_SGLO = const(17)
_MLIT = const(18)
_MDRK = const(19)

# ====================================================================
# Palettes
# ====================================================================
_GSTOP = (
    ((0x73,0x61,0xB5),(0xF2,0xBC,0x42),(0x5D,0x6A,0x2F)),
    ((0x31,0x6B,0xB4),(0xC4,0xDC,0xF4),(0xD7,0xB9,0x96)),
    ((0x63,0x9B,0xAC),(0xB9,0x65,0x65),(0x7C,0xA0,0xD2)),
    ((0xCD,0x41,0x7E),(0x64,0x29,0x69),(0x2F,0x6E,0x99)),
)
_HCOL = (
    (0x00, 0xC6, 0xFF),
    (0xDD, 0x55, 0x00),
    (0xEA, 0xFF, 0x00),
    (0xFF, 0xDD, 0x00),
)

_STARS   = ((3,3),(27,5),(2,24),(28,27),(11,2),(22,29),(8,10),(24,12),(9,22),(23,21))
_STAR_RV = const(0xFF)
_STAR_GV = const(0xFA)
_STAR_BV = const(0xE8)

# ====================================================================
# Module state
# ====================================================================

_rtc     = None
_sin_lut = None
_gmap    = None
_fg      = None
_fgz     = bytes(1024)
_wx      = None
_wy      = None
_mpos    = None
_grad    = None

_srise = 6.0;  _sset = 18.0;  _salt = 50.0
_mphase = 0.0; _mrise = 18.0; _mset = 6.0

_hr = 255; _hg = 255; _hb = 255
_gr = 64;  _gg = 64;  _gb = 64
_sa = 0

_cached_min = -1
_cached_doy = -1

# ====================================================================
# FPS state
# ====================================================================
_fps_frames  = 0
_fps_last_ms = 0

# ====================================================================
# Helpers
# ====================================================================

def _lerp(a, b, t256):
    return a + ((b - a) * t256 >> 8)

def _lerp3(a, b, t256):
    return (_lerp(a[0],b[0],t256), _lerp(a[1],b[1],t256), _lerp(a[2],b[2],t256))

def _doy(year, month, day):
    _DIM = (0,31,28,31,30,31,30,31,31,30,31,30,31)
    d = day
    for m in range(1, month):
        d += _DIM[m]
    if month > 2 and year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
        d += 1
    return d

def _solar(doy):
    lat = _LATITUDE * math.pi / 180
    decl = 23.45 * math.sin(2 * math.pi * (doy - 81) / 365)
    cha = -math.tan(lat) * math.tan(decl * math.pi / 180)
    if cha < -1: return 0.0, 24.0, 90.0 - _LATITUDE + decl
    if cha >  1: return 12.0, 12.0, 0.0
    ha = math.acos(cha) * 180 / math.pi / 15
    return 12 - ha, 12 + ha, 90.0 - abs(_LATITUDE - decl)

def _moon(year, doy):
    days = (year - 2000) * 365 + (year - 2000) // 4 + doy - 6
    p = days % 29.53058770576 / 29.53058770576
    return p + 1 if p < 0 else p

def _palette(fh):
    global _grad, _hr, _hg, _hb, _gr, _gg, _gb, _sa
    fh = fh % 24
    fi = 3
    for i in range(3, -1, -1):
        if fh >= _PH[i]: fi = i; break
    ti = (fi + 1) % 4
    fH = _PH[fi]; tH = _PH[ti]
    span = (tH - fH) if tH > fH else (24 - fH + tH)
    el   = (fh - fH) if fh >= fH else (fh + 24 - fH)
    t256 = int(el / span * 256)
    if t256 < 0:   t256 = 0
    if t256 > 256: t256 = 256
    gF = _GSTOP[fi]; gT = _GSTOP[ti]
    top = _lerp3(gF[0], gT[0], t256)
    mid = _lerp3(gF[1], gT[1], t256)
    bot = _lerp3(gF[2], gT[2], t256)
    g = []
    for r in range(32):
        if r < 16: g.append(_lerp3(top, mid, r * 256 // 15))
        else:      g.append(_lerp3(mid, bot, (r - 16) * 256 // 15))
    _grad = g
    hc = _lerp3(_HCOL[fi], _HCOL[ti], t256)
    _hr, _hg, _hb = hc
    _gr, _gg, _gb = hc[0] >> 2, hc[1] >> 2, hc[2] >> 2
    if   fi == 2 and ti == 3: _sa = t256
    elif fi == 3 and ti == 0: _sa = 256 - t256
    elif fi == 3:              _sa = 256
    else:                      _sa = 0

def _sky_pos(prog, max_alt):
    x = int(_SKY_LX + prog * (_SKY_RX - _SKY_LX))
    peak_y = int(_SKY_HY - (max_alt / 90.0) * (_SKY_HY - _SKY_TY))
    y = int(_SKY_HY - math.sin(prog * math.pi) * (_SKY_HY - peak_y))
    return x, y

def _ecl_overlap(x1, y1, r1, x2, y2, r2):
    d = math.sqrt((x2-x1)**2 + (y2-y1)**2)
    if d >= r1+r2: return 0.0
    if d+r2 <= r1: return math.pi * r2 * r2
    if d+r1 <= r2: return math.pi * r1 * r1
    a1 = math.acos((d*d+r1*r1-r2*r2) / (2*d*r1))
    a2 = math.acos((d*d+r2*r2-r1*r1) / (2*d*r2))
    disc = (-d+r1+r2) * (d+r1-r2) * (d-r1+r2) * (d+r1+r2)
    return r1*r1*a1 + r2*r2*a2 - 0.5 * math.sqrt(max(0.0, disc))

def _bres(x0, y0, x1, y1, fg, v):
    dx = abs(x1-x0); dy = -abs(y1-y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    e = dx + dy
    while True:
        if 0 <= x0 < 32 and 0 <= y0 < 32: fg[y0*32+x0] = v
        if x0 == x1 and y0 == y1: break
        e2 = e << 1
        if e2 >= dy: e += dy; x0 += sx
        if e2 <= dx: e += dx; y0 += sy

def _thick(x0, y0, x1, y1, fg, v):
    _bres(x0,   y0,   x1,   y1,   fg, v)
    _bres(x0+1, y0,   x1+1, y1,   fg, v)
    _bres(x0,   y0+1, x1,   y1+1, fg, v)

def _disc(cx, cy, r, fg, v):
    r2 = r * r
    for dy in range(-r, r+1):
        for dx in range(-r, r+1):
            if dx*dx + dy*dy <= r2:
                px = cx+dx; py = cy+dy
                if 0 <= px < 32 and 0 <= py < 32:
                    fg[py*32+px] = v

# ====================================================================
# init()
# ====================================================================

def init():
    global _rtc, _sin_lut, _gmap, _fg, _wx, _wy, _mpos
    global _srise, _sset, _salt, _mphase, _mrise, _mset
    global _cached_min, _cached_doy
    global _fps_frames, _fps_last_ms

    _rtc = machine.RTC()
    _fg  = bytearray(1024)
    _wx  = [0] * 32
    _wy  = [0] * 32

    _sin_lut = [int(127 * math.sin(2 * math.pi * i / 256)) for i in range(256)]

    _gmap = bytearray(1024)
    for y in range(32):
        for x in range(32):
            d = math.sqrt((x-16)**2 + (y-16)**2)
            f = max(0.0, 1.0 - d / 16.0)
            _gmap[y*32+x] = 3 if f > 0.70 else 2 if f > 0.45 else 1 if f > 0.20 else 0

    mpos = []
    for i in range(12):
        a = i * math.pi / 6
        mx = int(16 + 14 * math.sin(a) + 0.5)
        my = int(16 - 14 * math.cos(a) + 0.5)
        pv = _CARD if i % 3 == 0 else _MRK
        if 0 <= mx < 32 and 0 <= my < 32:
            mpos.append((mx, my, pv))
        for ddx, ddy in ((-1,0),(1,0),(0,-1),(0,1)):
            px = mx+ddx; py = my+ddy
            if 0 <= px < 32 and 0 <= py < 32:
                mpos.append((px, py, pv))
    _mpos = mpos

    t = _rtc.datetime()
    doy = _doy(t[0], t[1], t[2])
    _srise, _sset, _salt = _solar(doy)
    _mphase = _moon(t[0], doy)
    mt = (12 + _mphase * 24) % 24
    _mrise = (mt - 6 + 24) % 24
    _mset  = (mt + 6) % 24
    _cached_doy = doy

    fh = t[4] + t[5] / 60.0
    _palette(fh)
    _cached_min  = t[4] * 60 + t[5]

    _fps_frames  = 0
    _fps_last_ms = time.ticks_ms()

    print("[cosmic_clock] init OK")
    print("  RTC:   {:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
        t[0],t[1],t[2],t[4],t[5],t[6]))
    print("  Solar: rise={:.2f}  set={:.2f}  maxAlt={:.1f}".format(_srise,_sset,_salt))
    print("  Moon:  phase={:.3f}  rise={:.2f}  set={:.2f}".format(_mphase,_mrise,_mset))

# ====================================================================
# draw()
# ====================================================================

@micropython.native  # noqa: F821
def draw():
    global _cached_min, _cached_doy
    global _srise, _sset, _salt, _mphase, _mrise, _mset
    global _fps_frames, _fps_last_ms

    t = _rtc.datetime()
    yr=t[0]; mo=t[1]; dy_=t[2]; hr=t[4]; mn=t[5]; sc=t[6]
    cur_min = hr * 60 + mn
    fh = hr + mn / 60.0

    # ---- Daily update ----
    cur_doy = _doy(yr, mo, dy_)
    if cur_doy != _cached_doy:
        _srise, _sset, _salt = _solar(cur_doy)
        _mphase = _moon(yr, cur_doy)
        mt = (12 + _mphase * 24) % 24
        _mrise = (mt - 6 + 24) % 24
        _mset  = (mt + 6) % 24
        _cached_doy = cur_doy

    # ---- Per-minute palette update ----
    if cur_min != _cached_min:
        _palette(fh)
        _cached_min = cur_min

    # ---- Sun ----
    sun_up = _srise <= fh <= _sset
    sx = 0; sy = 99
    if sun_up:
        span = _sset - _srise
        prog = (fh - _srise) / span if span > 0 else 0.0
        sx, sy = _sky_pos(prog, _salt)

    # ---- Moon ----
    mr = _mrise; ms_m = _mset
    moon_up = (fh >= mr or fh <= ms_m) if mr >= ms_m else (mr <= fh <= ms_m)
    mx_b = 0; my_b = 99
    if moon_up:
        span = (24 - mr + ms_m) if mr >= ms_m else (ms_m - mr)
        el   = (fh - mr) if fh >= mr else (fh + 24 - mr)
        prog = el / span if span > 0 else 0.0
        mx_b, my_b = _sky_pos(prog, _salt * 0.85)

    # ---- Eclipse ----
    ecl  = 256
    fire = 0
    if sun_up and moon_up:
        sun_area = math.pi * _SUN_R * _SUN_R
        ov  = _ecl_overlap(sx, sy, _SUN_R, mx_b, my_b, _MOON_R)
        cov = ov / sun_area if sun_area > 0 else 0.0
        ecl  = int((1.0 - cov * 0.65) * 256)
        fire = min(256, int(cov * 3.0 * 256))

    # ---- Blood moon ----
    blood = 0
    bd = abs(_mphase - 0.5)
    if bd < 0.03:
        blood = int((1.0 - bd / 0.03) * 256)

    illum = 1.0 - abs(2.0 * _mphase - 1.0)

    # ---- FG buffer ----
    fg = _fg
    fg[:] = _fgz

    if sun_up:
        sr = _SUN_R; sr1 = sr + 1
        sr2 = sr*sr; sr12 = sr1*sr1
        for ddy in range(-sr1, sr1+1):
            for ddx in range(-sr1, sr1+1):
                d2 = ddx*ddx + ddy*ddy
                if sr2 < d2 <= sr12:
                    px=sx+ddx; py=sy+ddy
                    if 0 <= px < 32 and 0 <= py < 32:
                        fg[py*32+px] = _SGLO
        _disc(sx, sy, sr, fg, _SCOR)

    if moon_up:
        mr2 = _MOON_R * _MOON_R
        mr_f = float(_MOON_R)
        for ddy in range(-_MOON_R, _MOON_R+1):
            for ddx in range(-_MOON_R, _MOON_R+1):
                if ddx*ddx + ddy*ddy <= mr2:
                    ndx = ddx / mr_f
                    term = 1.0 - 2.0 * illum
                    lit = (ndx > term) if _mphase <= 0.5 else (ndx < -term)
                    px=mx_b+ddx; py=my_b+ddy
                    if 0 <= px < 32 and 0 <= py < 32:
                        fg[py*32+px] = _MLIT if lit else _MDRK

    sa = _sa
    if sa > 0:
        for stx, sty in _STARS:
            if fg[sty*32+stx] == 0:
                fg[sty*32+stx] = _STAR

    for mpx, mpy, mpv in _mpos:
        fg[mpy*32+mpx] = mpv

    pi = math.pi
    ma  = mn * pi / 30 + sc * pi / 1800
    mex = int(16 + _MIN_LEN * math.sin(ma) + 0.5)
    mey = int(16 - _MIN_LEN * math.cos(ma) + 0.5)
    ha  = (hr % 12) * pi / 6 + mn * pi / 360
    hx  = int(16 + _HR_LEN * math.sin(ha) + 0.5)
    hy  = int(16 - _HR_LEN * math.cos(ha) + 0.5)

    _thick(16,16, mex,mey, fg, _GLOW)
    _thick(16,16, hx,hy,   fg, _GLOW)
    _bres(15,15,  hx-1,hy-1, fg, _GLOW)
    _bres(17,17,  hx+1,hy+1, fg, _GLOW)
    _bres(16,16, mex,mey, fg, _HAND)
    _thick(16,16, hx,hy,  fg, _HAND)
    _disc(16,16, 1, fg, _CTR)

    # ---- Wave ----
    t_ms = time.ticks_ms()
    tpx = (t_ms // _WTX) & 255
    tpy = (t_ms // _WTY) & 255
    lut = _sin_lut
    wx  = _wx; wy = _wy
    for x in range(32): wx[x] = lut[(x * _WX + tpx) & 255]
    for y in range(32): wy[y] = lut[(-y * _WY - tpy) & 255]

    # ---- Eclipse-adjusted colors ----
    scr = 255
    scg = 245 - ((245-140) * fire >> 8)
    scb = 180 - ((180- 30) * fire >> 8)
    sgr = 255
    sgg = 220 - ((220-160) * fire >> 8)
    sgb = 140 - ((140- 20) * fire >> 8)
    mlr = 220 + ((180-220) * blood >> 8)
    mlg = 225 + (( 25-225) * blood >> 8)
    mlb = 240 + (( 25-240) * blood >> 8)

    gfx   = graphics
    gmap  = _gmap
    grad  = _grad
    tm    = _TM
    hr_c  = _hr;  hg_c = _hg;  hb_c = _hb
    glo_r = _gr;  glo_g = _gg; glo_b = _gb

    # ---- Pixel loop ----
    for y in range(32):
        wy_v = wy[y]
        row  = y * 32
        for x in range(32):
            wave = wx[x] + (wy_v >> 1)
            gi = y + (wave * _WAM >> 8)
            if gi < 0:  gi = 0
            elif gi > 31: gi = 31
            base = grad[gi]
            cm = (tm[gmap[row+x]] * ecl) >> 8
            rv = (base[0] * cm) >> 8
            gv = (base[1] * cm) >> 8
            bv = (base[2] * cm) >> 8
            if rv > 255: rv = 255
            if gv > 255: gv = 255
            if bv > 255: bv = 255

            fv = fg[row+x]
            if fv == _HAND or fv == _CTR:
                rv = hr_c; gv = hg_c; bv = hb_c
            elif fv == _MRK or fv == _CARD:
                rv = hr_c; gv = hg_c; bv = hb_c
            elif fv == _GLOW:
                rv = glo_r; gv = glo_g; bv = glo_b
            elif fv == _STAR:
                rv = rv + (((_STAR_RV - rv) * sa) >> 8)
                gv = gv + (((_STAR_GV - gv) * sa) >> 8)
                bv = bv + (((_STAR_BV - bv) * sa) >> 8)
            elif fv == _SCOR:
                rv = scr; gv = scg; bv = scb
            elif fv == _SGLO:
                rv = rv + ((sgr - rv) * 115 >> 8)
                gv = gv + ((sgg - gv) * 115 >> 8)
                bv = bv + ((sgb - bv) * 115 >> 8)
            elif fv == _MLIT:
                rv = mlr; gv = mlg; bv = mlb
            elif fv == _MDRK:
                rv = rv >> 2; gv = gv >> 2; bv = bv >> 2

            gfx.set_pen(gfx.create_pen(rv, gv, bv))
            gfx.pixel(x, y)

    # ---- FPS + diagnostics: print once per second ----
    _fps_frames += 1
    now_ms  = time.ticks_ms()
    elapsed = time.ticks_diff(now_ms, _fps_last_ms)
    if elapsed >= 1000:
        fps = _fps_frames * 1000 // elapsed
        period = ("morning","day","evening","night")[
            3 if fh>=_PH[3] else 2 if fh>=_PH[2] else 1 if fh>=_PH[1] else 0
        ]
        sun_str  = "sun({},{})".format(sx,sy)        if sun_up  else "sun:down"
        moon_str = "moon({},{})".format(mx_b,my_b)   if moon_up else "moon:down"
        ecl_str  = " ECL={:.0f}%".format((1-ecl/256)*100) if ecl < 250 else ""
        print("[clock] {:02d}:{:02d}:{:02d} {} | {}fps | wave:{} | {} {} | phase:{:.2f}{}".format(
            hr, mn, sc, period, fps,
            (t_ms // _WTX) & 255,
            sun_str, moon_str, _mphase, ecl_str))
        _fps_frames  = 0
        _fps_last_ms = now_ms


def deinit():
    global _sin_lut, _gmap, _fg, _wx, _wy, _mpos, _grad
    _sin_lut = None; _gmap = None; _fg = None
    _wx = None; _wy = None; _mpos = None; _grad = None