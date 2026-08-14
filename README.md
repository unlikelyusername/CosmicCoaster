# CosmicCoaster

A clock and a set of visual effects for the Pimoroni Unicorn LED panels.

Plug the board in, copy the files across, and it boots into a rotating set of
animations. It picks up the time over WiFi, so the clock is right without
setting anything.

| Board | Size |
|---|---|
| Cosmic Unicorn | 32 × 32 |
| Galactic Unicorn | 53 × 11 |
| Stellar Unicorn | 16 × 16 |

One set of files runs on all three. You tell it which board you have in
`config.json`; everything else adapts.

---

## Install

You need a Unicorn board already running Pimoroni's MicroPython build, and
[`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html)
on your computer:

```sh
pip install mpremote
```

**1. Find your board.**

```sh
mpremote devs
```

Look for the line saying `MicroPython Board in FS mode` and copy its device
path — something like `/dev/cu.usbmodem114301` on macOS, or `COM3` on Windows.

**2. Make your settings file.**

```sh
cp config.example.json config.json
```

Open `config.json` and fill in your board model and WiFi details — see
[Settings](#settings) below.

**3. Copy everything to the board.**

```sh
mpremote connect /dev/cu.usbmodem114301 fs cp -r lib :/
mpremote connect /dev/cu.usbmodem114301 fs cp -r effects :/
mpremote connect /dev/cu.usbmodem114301 fs cp main.py config.json :/
```

**4. Restart the board.** Unplug and replug it, or press reset. It will start
on its own from now on, every time it gets power.

---

## Using it

| Button | Does |
|---|---|
| **A** | Next effect |
| **Brightness +** / **−** | Dim or brighten the panel |

That's the whole interface. The effect you leave it on stays until you press A
again or the board loses power — it always starts from the first effect.

---

## Settings

Everything lives in `config.json`:

```json
{
  "model": "cosmic",
  "networks": [
    { "ssid": "YourNetwork", "password": "yourpassword" }
  ],
  "timeout": 20,
  "utc_offset": -4,
  "ntp_host": "pool.ntp.org",
  "brightness": 0.5
}
```

| Setting | Meaning |
|---|---|
| `model` | `"cosmic"`, `"galactic"` or `"stellar"` — which board you have |
| `networks` | WiFi networks to try, **in order**. Add as many as you like |
| `timeout` | Seconds to wait for WiFi before giving up and carrying on |
| `utc_offset` | Hours from UTC for the clock. `-4` is US Eastern in summer |
| `ntp_host` | Time server. The default is fine |
| `brightness` | Starting brightness, `0.0` to `1.0` |

**Multiple networks** are useful if you move the board between home and
somewhere else — list both and it will use whichever it finds.

**No WiFi?** It still works. The effects all run; only the clock needs the
network, and the radio is switched off after the time is fetched either way.

> ⚠️ **`config.json` contains your WiFi password as plain text.** That is why
> the file you edit is a copy — `config.example.json` is the safe one to share.
> Don't post `config.json` in a forum, attach it to a bug report, or commit it
> to a public repository.

---

## The effects

Press **A** to move through them.

| Effect | What you see |
|---|---|
| **hyperdrive** | Flying through a starfield, banking and turning, stars leaving curved coloured trails |
| **hyperspace3d** | The older starfield — same idea, heavier look |
| **hyperspace** | A flatter, faster take on the same |
| **wireframe** | A cube and an octahedron tumbling on crossing orbits |
| **attractor** | A strange attractor drawing itself as a glowing trajectory |
| **plasma** | Classic demoscene plasma — overlapping sine waves |
| **interference** | Six drifting wave sources, rippling where they cross |
| **distortion_waves** | Rolling distortion patterns |
| **octopus** | Rotating arms sweeping out from the centre |
| **soap** | Slow, iridescent soap-film swirl |
| **waving_cell** | A cellular grid rippling in waves |
| **bz_ripple** | A Belousov–Zhabotinsky chemical reaction — self-organising spiral waves |
| **bz_paint** | The same reaction, but it paints a canvas that slowly fades |
| **glitch** | Neon colour bars with stacked glitch artifacts |
| **glitchv2** | Hardware-failure glitches — tearing, dropouts, colour corruption |
| **supercomputer** | Blinkenlights: every pixel an amber lamp on its own cycle |
| **clock_cosmic** | The clock. **Cosmic 32×32 only** |

Effects that need a particular screen shape are skipped automatically on boards
they don't suit, so you will see fewer than 17 on the Galactic and Stellar.

---

## Troubleshooting

**Nothing happens when I power it on.**
Check the files actually copied — `mpremote connect <port> fs ls :` should list
`main.py`, `lib` and `effects`. If `main.py` is missing, redo step 3.

**It's stuck on a blank or frozen screen.**
Press the physical reset button. If the board has vanished from `mpremote devs`
entirely, unplug it and plug it back in — nothing on the computer side will
bring it back.

**The clock is wrong.**
Check `utc_offset` in `config.json`. It's a whole number of hours from UTC, and
it does **not** adjust for daylight saving — you change it twice a year.

**It never connects to WiFi.**
Only 2.4GHz networks work; the radio in these boards cannot see 5GHz. Check the
SSID and password for typos, and raise `timeout` if your router is slow to
respond. Watch the boot messages with:

```sh
mpremote connect /dev/cu.usbmodem114301
```

**An effect looks wrong on my board.**
Some are designed for a square panel and are skipped on the Galactic's wide
strip. If one still looks off, it's a bug — worth reporting.

---

## Adding your own effect

Drop a `.py` file in `effects/`. It needs three functions and a line saying
what screen shape it needs:

```python
GEOMETRY = "any"     # or "square", "cosmic", "galactic", "stellar"

graphics = None      # filled in for you
W = 0                # display width, filled in for you
H = 0                # display height, filled in for you

def init():  ...     # set up
def draw():  ...     # called once per frame
def deinit(): ...    # release anything init() allocated
```

Copy it across and it appears in the rotation. `deinit()` matters — memory on
these boards is tight, and whatever you allocate must be released when the
effect is swapped out.
