#!/usr/bin/env python3
"""Prove that monkeypatching through the tetramaster and tmtitle facades still reaches the code.

    python tools/facade_rebind_check.py        # fail on any rebinding the facade does not forward
    python tools/facade_rebind_check.py -v     # also list every rebinding found

services/tetramaster.py is a facade over the services/tmgame package, and
services/tmtitle.py over services/tmplugin. Reads of `tetramaster.<name>` go
to the module that owns the name. That is not enough for the tests: some of
them REBIND a name to quiet or fake a helper,

    tm._say = lambda *a, **k: None          # tools/tm_gameea_refuse_test.py
    R._live_rooms = lambda: live            # tools/tm_lobby_counts_test.py

and a rebinding that landed on the facade alone would leave every caller
inside the package still calling the real function. Nothing would raise and
the test would keep passing while testing nothing.

The facade therefore forwards writes to the owning module, and for a name
that modules import by copy (`tmsave`, `tmbattle`, ...) to every module that
holds a copy. This tool checks that promise against the rebindings the tools
actually make, empirically: it sets a sentinel through the facade and reads it
back through the owning module and every package module that has the name.

The tools import a facade under several names (`import tetramaster as tm`,
`import tmtitle as R`, or plain, sometimes inside a function), so this walks
the AST for the names bound to the module rather than grepping for it.
tmtitle needs the OpenLobby core on the path (see tm_testenv.py); without it
that half is skipped, loudly.
"""
import argparse
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")
SCAN_DIRS = ("tools", "services")
#: facade module -> the package it forwards to
FACADES = {"tetramaster": "tmgame", "tmtitle": "tmplugin"}


def module_aliases(tree, facade):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == facade:
                    out[a.asname or a.name] = a.name
    return out


def rebindings(facade):
    """[(file, line, attr)] for every `<alias>.<attr> = ...` on the facade,
    and every `setattr(<alias>, "<attr>", ...)`."""
    found = []
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
                try:
                    tree = ast.parse(open(path, encoding="utf-8").read())
                except (SyntaxError, UnicodeDecodeError):
                    continue
                aliases = module_aliases(tree, facade)
                if not aliases:
                    continue
                for node in ast.walk(tree):
                    targets = []
                    if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for tgt in targets:
                        for t in ast.walk(tgt):
                            if (isinstance(t, ast.Attribute) and isinstance(t.ctx, ast.Store)
                                    and isinstance(t.value, ast.Name)
                                    and t.value.id in aliases):
                                found.append((rel, node.lineno, t.attr))
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "setattr" and len(node.args) >= 2
                            and isinstance(node.args[0], ast.Name)
                            and node.args[0].id in aliases
                            and isinstance(node.args[1], ast.Constant)):
                        found.append((rel, node.lineno, node.args[1].value))
    return found


def forwards(T, attr, facade):
    """Does a write of `attr` through the facade reach the owning module (and
    every module holding a copy)? Returns a reason string on failure."""
    owners = getattr(T, "_OWNERS", None)
    modules = getattr(T, "_MODULES", None)
    if owners is None or modules is None:
        return f"{facade}.py is not the facade (no _OWNERS/_MODULES)"
    home = owners.get(attr)
    if home is None:
        return f"not a name of the old {facade}.py; the patch lands on the facade only"
    sentinel = object()
    saved = {m: vars(mod)[attr] for m, mod in modules.items() if attr in vars(mod)}
    try:
        setattr(T, attr, sentinel)
        if getattr(modules[home], attr, None) is not sentinel:
            return f"write did not reach the owner {FACADES[facade]}.{home}"
        for m, mod in modules.items():
            if m in saved and getattr(mod, attr) is not sentinel:
                return f"{FACADES[facade]}.{m} still holds the old copy"
        if getattr(T, attr) is not sentinel:
            return "read-back through the facade returned something else"
    finally:
        for m, old in saved.items():
            setattr(modules[m], attr, old)
    return None


def check(facade, verbose):
    """0 when every rebinding of `facade` is forwarded, 1 otherwise."""
    package = FACADES[facade]
    T = __import__(facade)
    found = rebindings(facade)
    attrs = sorted({a for _f, _l, a in found})
    print(f"{facade}: {len(found)} rebinding(s) of {len(attrs)} name(s) across the tools")
    failures = []
    for attr in attrs:
        why = forwards(T, attr, facade)
        sites = [f"{f}:{l}" for f, l, a in found if a == attr]
        if why:
            failures.append((attr, why, sites))
        elif verbose:
            print(f"  [PASS] {attr:<28} -> {package}.{T._OWNERS[attr]}  ({', '.join(sites)})")
    for attr, why, sites in failures:
        print(f"  [FAIL] {attr}: {why}")
        print(f"         at {', '.join(sites)}")
    if failures:
        print(f"  {len(failures)} name(s) are patched by a tool but not forwarded by the facade")
        return 1
    # the other half of the promise: every name the old module had is still
    # readable through the facade (a map edit that dropped one would show here;
    # tmtitle's core names are None until the core binds them, which is fine)
    missing = [n for n in T._OWNERS if n not in vars(T._MODULES[T._OWNERS[n]])]
    if missing:
        print(f"  [FAIL] {len(missing)} name(s) in _OWNERS missing from their module: "
              f"{', '.join(sorted(missing)[:10])}")
        return 1
    print(f"  every rebinding is forwarded to the module that runs it; "
          f"all {len(T._OWNERS)} names of the old module resolve")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import tm_testenv
    core = tm_testenv.setup(need_core=False)
    rc = 0
    for facade in FACADES:
        if facade == "tmtitle" and core is None:
            print("tmtitle: SKIP (no OpenLobby core beside this tree; set OPENLOBBY_SERVICES)")
            continue
        rc |= check(facade, args.verbose)
    return rc


if __name__ == "__main__":
    sys.exit(main())
