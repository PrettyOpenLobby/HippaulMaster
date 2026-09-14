#!/usr/bin/env python3
"""Take back gold this server MINTED and never should have handed out.

WHY THIS EXISTS. `POL_TM_START_MONEY` defaulted to an unmeasured 10000, so every
member who touched Tetra Master had 10000 gold materialised for them by
`tetramaster.money_of` and then pushed to their client -- see the banner above
`_start_money`. Fixing the default stops the MINTING, but it deliberately does
not touch balances already on record: `money_of` materialises once and never
re-reads the default, so a granted balance is now indistinguishable from an
earned one and only a human knows which is which.

    "fought a COM opponent, quit, lost a card, and was given 10000 gil"
                                                    -- reported in live testing, 2026-08-25

So this is the deliberate act, kept as a tool rather than a one-off shell line
because it writes to somebody's save and ought to be reviewable, repeatable and
rehearsable.

    python tools/tm_money_reset.py 5 8 9 10           # DRY RUN -- prints only
    python tools/tm_money_reset.py --apply 5 8 9 10   # actually writes
    python tools/tm_money_reset.py --apply --to 500 5 # ...to a value, not 0

WARNING: **DRY RUN IS THE DEFAULT.** Nothing is written without `--apply`.

WARNING: **RUN IT WHERE THE STATE IS.** The balances live beside the resources
(`POL_RESOURCE_DIR`, `/data/resources` on prod), so on prod this is

    docker exec pol-server-authsess-1 python3 /app/../tools/tm_money_reset.py ...

or simply run it in a checkout with `POL_RESOURCE_DIR` pointed at that
directory. It needs no restart: `_collection_load` opens the JSON per call and
the save is served from disk.

WARNING: **A LIVE CLIENT WILL NOT NOTICE UNTIL IT IS TOLD.** The running client holds
its wallet in memory at save-struct +0xC8; it re-reads the save at launch, and
`@ComGameInit=/M=` assigns it mid-session at the next match setup. Resetting
somebody mid-session therefore drops their on-screen gold the moment they start
a game, which is exactly the confusing experience this whole thread is about.
**Do it between sessions.**

It writes through `tetramaster._set_money`, which is the one accessor that keeps
the JSON record and the client-facing save in step -- doing it by hand is how
the two drift.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "services"))

import tetramaster                                              # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("members", nargs="+",
                    help="member ids to reset (as they appear in "
                         "<id>.tm_collection.json)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this is a dry run")
    ap.add_argument("--to", type=int, default=0,
                    help="the balance to set (default 0)")
    ap.add_argument("--why", default="minted by POL_TM_START_MONEY, reclaimed",
                    help="reason, recorded in the server log")
    args = ap.parse_args()

    if args.to < 0:
        print("a negative balance is not a number the client can display")
        return 2

    where = tetramaster._collection_dir()
    print("collection dir: %s" % where)
    print("mode: %s" % ("APPLY -- writing" if args.apply else "DRY RUN"))
    print()

    rc = 0
    for mid in args.members:
        path = tetramaster._collection_file(mid)
        exists = path and os.path.exists(path)
        try:
            now = tetramaster.money_of(mid)
        except Exception as exc:
            print("  member %-4s CANNOT READ (%r)" % (mid, exc))
            rc = 1
            continue
        # WARNING: `money_of` OPENS AN ACCOUNT on a member who has none, which on a
        # never-touched member is itself a write. Say so rather than hiding it:
        # it is the same materialisation this tool exists to undo.
        note = "" if exists else "  (no record on file -- reading it made one)"
        if now == args.to:
            print("  member %-4s %6d -> unchanged%s" % (mid, now, note))
            continue
        if not args.apply:
            print("  member %-4s %6d -> %d  (would write)%s"
                  % (mid, now, args.to, note))
            continue
        if tetramaster._set_money(mid, args.to, args.why):
            print("  member %-4s %6d -> %d%s" % (mid, now, args.to, note))
        else:
            print("  member %-4s %6d -> FAILED TO WRITE" % (mid, now))
            rc = 1

    print()
    if not args.apply:
        print("nothing was written -- re-run with --apply")
    else:
        print("done. The save is rewritten too, so the change is what the "
              "client reads at its next launch.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
