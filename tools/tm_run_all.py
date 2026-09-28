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

Every suite imports OpenLobby's polcore (tmstore.py), so the core has to be
found for any of them. Suites listed in NEEDS_DB each get a fresh, empty
PostgreSQL database (tmpg.py, over OpenLobby's tools/pgtest.py), dropped when
the suite ends; with no server they SKIP, or FAIL under POL_TEST_REQUIRE_DB=1.
No suite ever sees a POL_DATABASE_URL or POL_VALKEY_URL from the environment
it was started in: live state is each suite's own in-memory store unless the
suite starts a Valkey of its own.
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
    # the facade over services/tmgame/ forwards every rebinding the tools make
    ("tm_facade",     [PY, "facade_rebind_check.py"],          HERE,     False),
    ("tm_title",      [PY, "tm_title.py", "--selftest"],       SERVICES, False),
    ("tmsave",        [PY, "tmsave.py", "--selftest"],         SERVICES, False),
    ("tmrank",        [PY, "tmrank.py", "--selftest"],         SERVICES, False),
    # the lobby lists and the room roster (zone list, room list, PTL)
    ("tmroom",        [PY, "tmroom.py", "--selftest"],         SERVICES, False),
    ("tm_backfill",   [PY, "tm_stats_backfill.py", "--selftest"], HERE,  False),
    ("tm_auction_mint", [PY, "tm_auction_mint_test.py"],       HERE,     True),
    ("tm_watch",      [PY, "tm_watch_test.py"],                HERE,     False),
    ("tm_board",      [PY, "tm_board_test.py"],                HERE,     False),
    ("tm_faces",      [PY, "tm_faces_test.py"],                HERE,     False),
    ("tm_com_deck",   [PY, "tm_com_deck_test.py"],             HERE,     False),
    ("tm_event_release", [PY, "tm_event_release_test.py"],     HERE,     False),
    ("tm_gameea_refuse", [PY, "tm_gameea_refuse_test.py"],     HERE,     False),
    # --- the seam with the core: the roster, the counts, the save defaults --
    ("tm_lobby_counts", [PY, "tm_lobby_counts_test.py"],       HERE,     True),
    ("tm_roster_delta_base", [PY, "tm_roster_delta_base_test.py"], HERE, True),
    ("tm_roster_retire", [PY, "tm_roster_retire_test.py"],     HERE,     True),
    ("tm_save_defaults", [PY, "tm_save_defaults_test.py"],     HERE,     True),
    # --- the state outside the process: PostgreSQL tables and Valkey keys ----
    ("tm_store",      [PY, "tm_store_test.py"],                HERE,     True),
    # the core's own resource suite, with this title loaded
    ("core_resource", [PY, os.path.join(CORE or "", os.pardir, "tools",
                                        "resource_test.py")],
                      os.path.join(CORE or "", os.pardir, "tools"),    True),
]


#: suites that get a fresh PostgreSQL database of their own (see the docstring):
#: this repository's store suite, and the core's resource suite, whose
#: accounts live in PostgreSQL too
NEEDS_DB = {"tm_store", "core_resource"}


def _fresh_database():
    """(url, drop) for a new empty database, or (None, why)."""
    try:
        import tmpg
        if not tmpg.server_available():
            return None, "no PostgreSQL server (Docker, or POL_TEST_DATABASE_URL)"
        url = tmpg.pgtest.create_database()
        return url, lambda: tmpg.pgtest.drop_database(url)
    except Exception as exc:                                  # noqa: BLE001
        return None, "no test database (%s)" % exc


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
    for k in ("POL_DATABASE_URL", "POL_VALKEY_URL", "TM_TEST_DATABASE"):
        env.pop(k, None)
    failed, skipped = [], []
    print(f"running {len(todo)} suite(s); core: {CORE or 'NOT FOUND'}")
    for name, cmd, cwd, needs_core in todo:
        if needs_core and CORE is None:
            print("  %-22s ... SKIP  (no OpenLobby core beside this tree)" % name)
            skipped.append(name)
            continue
        suite_env, drop = env, None
        if name in NEEDS_DB:
            url, drop = _fresh_database()
            if url is None:
                if os.environ.get("POL_TEST_REQUIRE_DB") == "1":
                    print("  %-22s ... FAIL  (%s, POL_TEST_REQUIRE_DB=1)" % (name, drop))
                    failed.append(name)
                else:
                    print("  %-22s ... SKIP  (%s)" % (name, drop))
                    skipped.append(name)
                continue
            suite_env = dict(env, POL_DATABASE_URL=url, TM_TEST_DATABASE="1")
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=cwd, env=suite_env, timeout=600,
                               capture_output=not args.v, text=True,
                               encoding="utf-8", errors="replace")
            ok = r.returncode == 0
        except subprocess.TimeoutExpired:
            ok, r = False, None
        finally:
            if drop is not None:
                try:
                    drop()
                except Exception:                             # noqa: BLE001
                    pass
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
