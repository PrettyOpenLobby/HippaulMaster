#!/usr/bin/env python3
"""Run every Tetra Master selftest, and exit non-zero if any of them fails.

    python tools/tm_run_all.py              # everything
    python tools/tm_run_all.py -k roster    # only suites whose name contains
    python tools/tm_run_all.py -v           # stream each suite's own output

The list is explicit, not globbed: a suite that is not registered here does
not exist. Suites marked `core` exercise the seam with the OpenLobby core and
need it beside this tree (see tm_testenv.py); they are skipped, loudly, when
it is not found. The last entry runs the core's own resource suite WITH this
title loaded, so its Tetra Master section stops skipping.
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
SERVICES = os.path.join(ROOT, "services")
sys.path.insert(0, HERE)
import tm_testenv                                                  # noqa: E402

CORE = tm_testenv.core_path()
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")   # suites print non-ASCII markers
PY = sys.executable

#: (name, argv, cwd, needs the core)
SUITES = [
    # --- the game itself: cards, dice, board, battle -------------------------
    ("tmroll",        [PY, "tmroll.py"],                       SERVICES, False),
    ("tmbattle",      [PY, "tmbattle.py"],                     SERVICES, False),
    ("tetramaster",   [PY, "tetramaster.py", "--selftest"],    SERVICES, False),
    ("tm_title",      [PY, "tm_title.py", "--selftest"],       SERVICES, False),
    ("tmsave",        [PY, "tmsave.py", "--selftest"],         SERVICES, False),
    ("tmrank",        [PY, "tmrank.py", "--selftest"],         SERVICES, False),
    # the lobby lists and the room roster (zone list, room list, PTL)
    ("tmroom",        [PY, "tmroom.py", "--selftest"],         SERVICES, False),
    ("tm_backfill",   [PY, "tm_stats_backfill.py", "--selftest"], HERE,  False),
    ("tm_auction_mint", [PY, "tm_auction_mint_test.py"],       HERE,     True),
    ("tm_watch",      [PY, "tm_watch_test.py"],                HERE,     False),
    ("tm_board",      [PY, "tm_board_test.py"],                HERE,     False),
    # --- the seam with the core: the roster, the counts, the save defaults --
    ("tm_lobby_counts", [PY, "tm_lobby_counts_test.py"],       HERE,     True),
    ("tm_roster_delta_base", [PY, "tm_roster_delta_base_test.py"], HERE, True),
    ("tm_roster_retire", [PY, "tm_roster_retire_test.py"],     HERE,     True),
    ("tm_save_defaults", [PY, "tm_save_defaults_test.py"],     HERE,     True),
    # the core's own resource suite, with this title loaded
    ("core_resource", [PY, os.path.join(CORE or "", os.pardir, "tools",
                                        "resource_test.py")],
                      os.path.join(CORE or "", os.pardir, "tools"),    True),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", action="append", default=[])
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()
    todo = [s for s in SUITES if not args.k or any(k in s[0] for k in args.k)]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in (SERVICES, CORE, env.get("PYTHONPATH", "")) if p])
    env["POL_TITLES"] = "tmtitle"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    failed, skipped = [], []
    print(f"running {len(todo)} suite(s); core: {CORE or 'NOT FOUND'}")
    for name, cmd, cwd, needs_core in todo:
        if needs_core and CORE is None:
            print("  %-22s ... SKIP  (no OpenLobby core beside this tree)" % name)
            skipped.append(name)
            continue
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=cwd, env=env, timeout=600,
                               capture_output=not args.v, text=True,
                               encoding="utf-8", errors="replace")
            ok = r.returncode == 0
        except subprocess.TimeoutExpired:
            ok, r = False, None
        print("  %-22s ... %s %6.1fs" % (name, "ok  " if ok else "FAIL",
                                         time.time() - t0), flush=True)
        if not ok:
            failed.append(name)
            if r is not None and not args.v:
                print("=" * 72)
                print((r.stdout or "")[-3000:])
                print((r.stderr or "")[-2000:])
                print("=" * 72)
    n = len(todo) - len(skipped)
    if failed:
        print(f"{n - len(failed)}/{n} suites passed, {len(failed)} FAILED: "
              f"{', '.join(failed)}")
        sys.exit(1)
    print(f"{n}/{n} suites passed" + (f" ({len(skipped)} skipped)" if skipped else ""))


if __name__ == "__main__":
    main()
