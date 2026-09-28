#!/usr/bin/env python3
"""tmgame.deps: a missing optional sibling (tmsave, tmprize, tmroll) is logged,
and the module still imports with that name set to None.

    python tools/tm_deps_optional_test.py

Each module is made unimportable by putting None in sys.modules under its name,
then tmgame.deps is imported fresh with the package's log function recorded.
The import used to call an undefined `_say` there and die with NameError.
"""
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.normpath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, SERVICES)

from tmgame import common  # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  --  %s" % detail if detail else ""))
    if not ok:
        FAILS.append(label)


def import_without(name):
    """Import tmgame.deps fresh with `name` unimportable.
    Returns (module or None, the exception or None, the logged lines)."""
    said = []
    real_say = common._say
    saved = {k: sys.modules.pop(k) for k in (name, "tmgame.deps")
             if k in sys.modules}
    sys.modules[name] = None                  # `import name` -> ImportError
    common._say = lambda *a, **k: said.append(" ".join(map(str, a)))
    try:
        try:
            mod, exc = importlib.import_module("tmgame.deps"), None
        except Exception as e:                 # noqa: BLE001
            mod, exc = None, e
    finally:
        common._say = real_say
        sys.modules.pop(name, None)
        sys.modules.pop("tmgame.deps", None)
        sys.modules.update(saved)
    return mod, exc, said


for name in ("tmsave", "tmprize", "tmroll"):
    print("%s cannot be imported ->" % name)
    mod, exc, said = import_without(name)
    check(exc is None, "tmgame.deps still imports", repr(exc) if exc else "")
    check(mod is not None and getattr(mod, name, "missing") is None,
          "deps.%s is None" % name)
    mine = [s for s in said if s.startswith("tm: %s unavailable" % name)]
    check(len(mine) == 1, "one 'tm: %s unavailable' line was logged" % name,
          repr(said))

print()
if FAILS:
    print("FAILED: %d" % len(FAILS))
    sys.exit(1)
print("all checks passed")
