# CosmicClock
Cosmic Unicorn Clock and Visual Effects

## Notes

### The `*` directory in `lib/` on the device

The device filesystem has an empty directory literally named `*` at `lib/*`.
It is ignored — nothing imports from it and it holds no files.

It is not a MicroPython quirk. It is a shell-quoting artifact: mpremote does
not expand wildcards on the device side, so a remote path containing `*` is
taken literally. A command like `mpremote fs mkdir ':lib/*'` — or any `cp`
whose device-side argument was a glob that the local shell left unexpanded —
creates a directory whose name is the single character `*`.

To remove it:

    mpremote connect /dev/cu.usbmodem114301 fs rmdir ':lib/*'

Quote or escape the path, otherwise zsh may try to expand it locally first.
