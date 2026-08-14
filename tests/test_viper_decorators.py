"""Every pointer-annotated function must actually be @micropython.viper.

This exists because of a bug no other test in this suite can see. Inserting
a new function immediately above build_path() left the old decorator
attached to the new function and build_path with none. It still compiled,
still produced byte-identical output, and passed all eight host suites --
because the host harness fakes @micropython.viper as an identity decorator,
so a decorated and an undecorated function are the same thing here.

On device they are not: build_path fell back to interpreted MicroPython and
went from ~30us to 3,274us, a hundredfold, dragging the effect to about 2fps.

So this test does not run the code. It reads the SOURCE and checks the
structural invariant: a function whose parameters are typed ptr8/ptr16/ptr32
is written to be compiled, and must carry the decorator that compiles it.
It also catches the other half of that accident -- a decorator applied twice.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib")

PTR = re.compile(r":\s*ptr(?:8|16|32)\b")
DEF = re.compile(r"^def\s+(\w+)\s*\(")
DECOR = re.compile(r"^@micropython\.(viper|native)\b")

failures = []
checked = 0

for name in sorted(os.listdir(LIB)):
    if not name.endswith(".py"):
        continue
    path = os.path.join(LIB, name)
    with open(path) as f:
        lines = f.read().splitlines()

    for i, line in enumerate(lines):
        m = DEF.match(line)
        if not m:
            continue
        # gather the full signature, which may wrap over several lines
        sig = line
        j = i
        while sig.count("(") > sig.count(")") and j + 1 < len(lines):
            j += 1
            sig += lines[j]
        if not PTR.search(sig):
            continue

        checked += 1
        fn = m.group(1)

        # walk backwards over decorators and comments to find what decorates it
        decorators = []
        k = i - 1
        while k >= 0:
            prev = lines[k].strip()
            if prev.startswith("@"):
                decorators.append(prev)
                k -= 1
            elif prev.startswith("#") or prev == "":
                break
            else:
                break

        if not decorators:
            failures.append(
                "%s:%d %s() takes pointer arguments but has NO "
                "@micropython.viper decorator -- it will run interpreted"
                % (name, i + 1, fn))
        elif not any(DECOR.match(d) for d in decorators):
            failures.append(
                "%s:%d %s() is decorated with %s, not @micropython.viper"
                % (name, i + 1, fn, decorators))
        elif len(decorators) > 1:
            failures.append(
                "%s:%d %s() has %d stacked decorators %s -- a duplicated "
                "@micropython.viper usually means one was stolen from the "
                "function below it"
                % (name, i + 1, fn, len(decorators), decorators))
        else:
            print("  ok  %-22s %s()" % (name, fn))

print()
print("checked %d pointer-taking functions across lib/" % checked)
if checked < 5:
    failures.append("only found %d pointer functions -- the scan is broken"
                    % checked)

if failures:
    print()
    for f in failures:
        print("FAIL:", f)
    print("VIPER DECORATORS: %d FAILURES" % len(failures))
    sys.exit(1)
print("VIPER DECORATORS: ALL CHECKS PASSED")
