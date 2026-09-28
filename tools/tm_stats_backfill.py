#!/usr/bin/env python3
"""Deliver the career stats we ALREADY HOLD into every member's save.

WHY THIS EXISTS. `tetramaster._save_sync_stats` writes a member's career block
into `U/g/TM0DataFile` -- but only on the three events that MOVE one: a match
result, a prize credit, and the Prize Center opening. That is correct going
forward and useless backwards. Matches have been scored into the collection's
`rank` block since 2026-08-22 and nothing has ever carried those numbers into a
save, so **every member who played before the write-through shipped still shows
zeros on Player Data -> Status until their next match.** This is the one-shot
that fixes them, and it is the same shape as `tmsave.py --heal` for the options
block: a deliberate, per-member, logged repair of saves that predate a fix.

    python tools/tm_stats_backfill.py --dry-run     say what would move
    python tools/tm_stats_backfill.py               write it
    python tools/tm_stats_backfill.py --member 16   just one

KEY: **IT CALLS `_save_sync_stats`, IT DOES NOT REIMPLEMENT IT.** A backfill that
computed the fields itself would be a second copy of the offset table, the width
table and the "only write what we produce" rule -- and the day one of them
changed, the repair tool would quietly write a different save than the live path
does. Importing the real function is the whole design.

WARNING: **WHAT IT WILL NOT INVENT.** A `rank` block written before 2026-08-24 has no
`place_total`, because nothing recorded finishing positions then. Average Rank
is therefore UNKNOWABLE for those matches and stays unwritten -- and so does the
TITLE that is computed from it. Deriving one from `games` alone would be
fabricating the single number every title in the game is gated on. The one
derivation that IS safe is the VS. Rating, from `score_total`/`games`, and
`_save_sync_stats` does it via `tmrank.stats_of` so the ranking lists and the
save cannot disagree.

WARNING: **SAFE TO RUN TWICE.** `_save_patch_fields` returns only the fields that
actually moved and skips the write entirely when nothing did, so a second
run reports zero changes and rewrites nothing.

The collections and saves are rows of the core's blob table (tmblob.py), so it
runs where POL_DATABASE_URL reaches the stack's PostgreSQL, for example
`docker compose exec login python tools/tm_stats_backfill.py --dry-run`.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))

import tmrank                                                    # noqa: E402
import tmsave                                                    # noqa: E402


def _members():
    """Every member id with a collection record, in a stable order."""
    return tmrank.collection_members()


def _describe(block):
    """The career numbers a member holds, for the report."""
    keep = ("games", "rating", "avg_rank", "place_total", "prize_total",
            "biggest_prize", "opponents", "consec_wins", "streak")
    return {k: block[k] for k in keep if block.get(k)}


def selftest():
    """0 = pass. Builds its own fixtures in a throwaway database (tmpg.py);
    touches nothing real.

    Guards the two things a repair tool silently loses: that it still reaches
    `_save_sync_stats` at all (a signature change would otherwise break it
    unnoticed until the day somebody needed it), and that it does NOT invent the
    fields it promises not to.
    """
    import struct
    import tempfile
    sys.path.insert(0, HERE)
    import tmpg
    if tmpg.fresh_database() is None:
        return tmpg.skip_or_fail("tm_stats_backfill")
    import tmblob
    tmp = tempfile.mkdtemp(prefix="tm-backfill-")
    res = os.path.join(tmp, "resources")
    os.makedirs(res)
    os.environ["POL_RESOURCE_DIR"] = res
    os.environ["POL_DATA_DIR"] = tmp
    os.environ["POL_LOG_DIR"] = tmp
    full = {"games": 8, "score_total": 96, "prize_total": 4000,
            "place_total": 1200, "avg_rank": 150, "biggest_prize": 900,
            "opponents": 8, "consec_wins": 3, "streak": 5}
    legacy = {"games": 4, "score_total": 40, "prize_total": 250}
    for mid, blk in (("16", full), ("7", legacy)):
        tmblob.write_json(tmrank.collection_name(mid),
                          {"cards": [], "money": 51000, "rank": blk})
    bad = []

    def check(cond, what):
        if not cond:
            bad.append(what)
            print("  [FAIL] " + what)

    rc = main(["--dry-run"])
    check(rc == 0, "--dry-run exits 0")
    check(not tmblob.exists("16.U_g_TM0DataFile.bin"),
          "--dry-run must write NOTHING")
    check(main([]) == 0, "the real run exits 0")

    import tetramaster
    blob = tmblob.read(tetramaster._save_file("16"))
    w = lambda o: struct.unpack_from("<H", blob, o)[0]      # noqa: E731
    d = lambda o: struct.unpack_from("<I", blob, o)[0]      # noqa: E731
    check(d(tmsave.AVG_RANK_OFF) == 150, "a full block delivers Average Rank")
    check(d(tmsave.AVERAGE_PRIZE_OFF) == 4000 // 8,
          "Average Prize is Grand Total over OPPONENTS (500), not over games")
    check(w(tmsave.CONSEC_WINS_OFF) == 3 and w(tmsave.STREAK_OFF) == 5,
          "the streak PAIR both survive -- a dword write would zero +0x7A")
    check(d(tmsave.RATING_OFF) == tmrank.rating_of(full),
          "VS. Rating is delivered (tmrank.rating_of: 1.00 + 3.00 x board share)")

    old = tmblob.read(tetramaster._save_file("7"))
    check(struct.unpack_from("<I", old, tmsave.AVG_RANK_OFF)[0] == 0,
          "a block with NO place_total must leave Average Rank unwritten -- "
          "inventing it fabricates the number every title is gated on")
    check(struct.unpack_from("<I", old, tmsave.RATING_OFF)[0]
          == tmrank.rating_of(legacy),
          "...but its VS. Rating IS derived (tmrank.rating_of, 16 tiles/game "
          "assumed without tiles_total), the same way tmrank.stats_of derives "
          "it, so the two cannot disagree")

    # Idempotence: the second run must move nothing and rewrite nothing.
    before = tmblob.mtime(tetramaster._save_file("16"))
    check(main([]) == 0, "a second run exits 0")
    check(tmblob.mtime(tetramaster._save_file("16")) == before,
          "a second run must not rewrite the save (_save_patch_fields skips "
          "the write when nothing moved)")

    print("tm_stats_backfill: %d check(s) failed" % len(bad) if bad
          else "tm_stats_backfill: PASS")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would move; write nothing")
    ap.add_argument("--member", action="append", dest="members",
                    help="only this member id (repeatable)")
    ap.add_argument("--selftest", action="store_true",
                    help="run against throwaway fixtures and assert")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()

    # Imported LATE and after the path is set: tetramaster is a large module and
    # a missing sibling should fail here with a clear message rather than at the
    # top of a tool whose purpose is repair.
    try:
        import tetramaster
    except Exception as exc:                                 # pragma: no cover
        sys.stderr.write("tetramaster did not import (%r) -- nothing to do\n"
                         % (exc,))
        return 2

    ids = args.members or _members()
    if not ids:
        print("no collection records -- nothing to back-fill")
        return 0

    if args.dry_run:
        # Patch the writer out rather than duplicating its decisions. The real
        # function still runs, still resolves every field, and still logs what
        # it would do; only the bytes-to-disk step is neutralised.
        real = tetramaster._save_patch_fields
        preview = {}

        def _fake(member_id, changes):
            import tmblob
            cur = {}
            raw = tmblob.read(tetramaster._save_file(member_id))
            buf = bytearray(raw if raw is not None else tetramaster._SAVE_FILE_LEN)
            if len(buf) < tetramaster._SAVE_FILE_LEN:
                buf.extend(b"\x00" * (tetramaster._SAVE_FILE_LEN - len(buf)))
            for off, (width, value) in sorted(changes.items()):
                got = tmsave.write_field(buf, off, width, value)
                if got:
                    cur[off] = got
            preview[member_id] = cur
            return cur

        tetramaster._save_patch_fields = _fake

    total, touched = 0, 0
    try:
        for mid in ids:
            block = (tetramaster._collection_load(mid) or {}).get("rank") or {}
            held = _describe(block)
            moved = tetramaster._save_sync_stats(mid)
            total += 1
            if moved:
                touched += 1
            print("  %-10s %-4s %s" % (
                mid, "%dB" % len(moved) if moved else "--",
                ", ".join("%s %s->%s" % (tmsave.STAT_FIELDS.get(o, hex(o)), a, b)
                          for o, (a, b) in sorted(moved.items()))
                or ("holds %r but nothing moved" % (held,) if held
                    else "no career stats recorded")))
            # The note belongs on any member missing the placement history --
            # ESPECIALLY one whose other fields DID move, because that is the
            # case where a reader would otherwise assume the row is complete.
            if block.get("games") and not block.get("place_total"):
                print("             ^ no place_total: this block predates "
                      "2026-08-24, so Average Rank -- and the TITLE computed "
                      "from it -- stay unwritten rather than invented")
    finally:
        if args.dry_run:
            tetramaster._save_patch_fields = real

    print("\n%d member(s) examined, %d %s"
          % (total, touched,
             "would be updated" if args.dry_run else "updated"))
    if args.dry_run and touched:
        print("re-run without --dry-run to write them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
