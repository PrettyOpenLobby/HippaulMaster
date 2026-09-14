#!/usr/bin/env python3
"""Build Tetra Master's ranking lists -- the WEEKLY TALLY JOB.

SE rebuilt these once a week ("Next update: 01/02/2011 10:00 PST" on the period
screenshot) and so do we. The format is documented in
`services/tmrank.py`; this is only the job that gathers players, sorts them into
the five lists and publishes the files.

    python tools/tmrank.py --publish                 build from live member state
    python tools/tmrank.py --publish --dry-run       ...and show, don't write
    python tools/tmrank.py --fixtures                rewrite the shipped fallbacks
    python tools/tmrank.py --dump <file.bin>         read a list or header back

WHY IT IS A JOB AND NOT A REQUEST HANDLER: the `<LN>` row count is answered by
the `authsess` container and the bytes are served seconds later by `login`.
A rebuild landing between those two would tell the client one length and
hand it another, which is POL-5135's shape. Once a week, out of band, cannot.

WHERE THE NUMBERS COME FROM. A member's ranking stats live in the `rank` block
of `<member>.tm_collection.json`, beside their cards:

    {"cards": [...], "money": 4321,
     "rank": {"rating": 296, "rating_last": 250,
              "prize_total": 125000, "prize_week": 4000,
              "rookie": true, "hide_name": false,
              "last_rank": {"0": 3, "4": 1}}}

`rating` is x100 (296 renders as 2.96, the same encoding as the save's VS.
Rating at +0x128) and the prize figures are plain money.

WHAT THIS JOB CAN DERIVE, AND WHAT IT CANNOT. Three of the four inputs a row
needs are real today:

  * the NAME, from the live room roster (`tmroom.name_of`) -- a member with no
    name is skipped rather than published as a blank row;
  * the IDENTITY, from the member's Tetra Master Content ID, which is the value
    the client itself hands back in `<CR>` (`tmrank.cid_for`) and is what makes
    `Your Rank: <r>/<N> players` resolve instead of `Did not rank`;
  * ROOKIE eligibility, from `member.created_at` -- a stand-in for a rule SE's
    server owned, and stated as one.

WARNING: **THE FOURTH USED TO BE "ZERO, BECAUSE NOTHING HERE CAN KNOW IT". THAT IS NO
LONGER TRUE AND THIS BANNER SAID SO FOR TWO DAYS AFTER IT STOPPED BEING TRUE.**
`tetramaster._bump_result_stats` and `_bump_prize` have scored every finished
match since 2026-08-22: the server holds the board (`_board_scores`), so it
knows who won, and it writes `games`, `score_total`, `rating`, `prize_total` and
`prize_week` into the `rank` block this job reads. Average rank (`avg_rank`,
`place_total`) joined them on 2026-08-24. A run today publishes real numbers.

What is still ours-by-construction rather than SE's, and stays flagged as such:
the RATING FORMULA (average score x100 -- it matches the career screen's own
division at `0x10F6A5` and lands on the scale of the one measured example,
"2.96", but SE's is unrecoverable) and the definition of AVERAGE RANK (mean
finishing position; see `tetramaster._bump_result_stats` for the three
independent things that put it on that scale and for what is NOT measured).

WARNING: And these numbers reach the RANKING SCREENS through this job only. The Player
Data -> Status screen reads them out of the player's own save instead, which is
`tetramaster._save_sync_stats`'s job, not this one -- two different consumers of
one ledger, and a stat can be live on one screen and stale on the other.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "services"))
import tmrank                                                    # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "services", "tmdata")


def _names():
    """{member id: display name}, from the roster this server already keeps."""
    try:
        import tmroom
    except ImportError:
        return {}
    try:
        return {str(k): str(v) for k, v in (tmroom._live_names() or {}).items()}
    except Exception:
        return {}


def _from_db(stamp, rookie_days):
    """{member: Content ID} and {member: is a rookie}, from accounts.db.

    THE CONTENT ID IS THE ROW'S IDENTITY (`tmrank.cid_for`) -- the value the
    client hands back to us in `<CR>`, which is what makes `Your Rank:` resolve
    instead of `Did not rank`. The DB's own copy wins over the mint formula so
    an issued id is used where one exists.

    AND `member.created_at` IS THE ONLY ROOKIE RULE WE CAN DERIVE. Ranking.BIN
    says "last week's top 10 rookies" and nothing in the client says what a
    rookie is -- that was SE's server's business -- so account age is a stand-in,
    stated as one. `POL_TM_RANK_ROOKIE_DAYS` moves the line.
    """
    try:
        import accounts
    except ImportError:
        return {}, {}
    cids, rookie = {}, {}
    try:
        conn = accounts.connect()
        for row in conn.execute("SELECT id, created_at FROM member"):
            mid = str(row["id"])
            try:
                age = (stamp - time.mktime(time.strptime(
                    str(row["created_at"])[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400.0
                rookie[mid] = age <= rookie_days
            except (ValueError, TypeError):
                pass
        for row in conn.execute(
                "SELECT h.member_id AS member_id, hc.content_id AS content_id"
                " FROM handle_content hc JOIN handle h ON h.id = hc.handle_id"
                " WHERE hc.content_code = ? AND hc.content_id IS NOT NULL",
                (tmrank.POOL_CONTENT_CODE,)):
            cids[str(row["member_id"])] = row["content_id"]
    except Exception as exc:
        print("accounts.db unreadable (%r) -- falling back to the mint formula "
              "for Content IDs and to each record's own rookie flag" % (exc,))
    return cids, rookie


def _players(args, stamp):
    if args.players:
        with open(args.players) as f:
            data = json.load(f)
        return data["players"] if isinstance(data, dict) else data
    cids, rookie = _from_db(stamp, args.rookie_days)
    return tmrank.load_players(resource_dir=args.resource_dir, names=_names(),
                               content_ids=cids, rookies=rookie)


def _build(players, stamp):
    """{resource path: bytes} for every list plus the header.

    `tmrank.tally_start(stamp)` is the first second of the week this publish
    tallies; Top 30 and Best Rookies only rank members who played since then."""
    files = {}
    counts = {}
    since = tmrank.tally_start(stamp)
    for rank_id, (menu, label, key, cap) in sorted(tmrank.LISTS.items()):
        blob = tmrank.build_list(rank_id, players, since)
        files[tmrank.path_for(rank_id)] = blob
        counts[menu] = len(blob) // tmrank.REC
    # Menu 3 (Prize Center) shares menu 4's file and draws no rows; its player
    # count slot is never read, so it is simply absent from `counts`.
    files[tmrank.RKDATA] = tmrank.build_rkdata(counts, stamp)
    return files, counts


def _report(files, counts, players):
    print("%d player(s) tallied" % len(players))
    for rank_id, (menu, label, key, cap) in sorted(tmrank.LISTS.items()):
        blob = files[tmrank.path_for(rank_id)]
        rows = len(blob) // tmrank.REC
        print("  RankID %d  menu %d  %-13s %3d row(s)  sorted by %s"
              % (rank_id, menu, label, rows, key))
        for i in range(min(rows, 5)):
            r = tmrank.decode_row(blob[i * tmrank.REC:(i + 1) * tmrank.REC])
            v = r[key]
            shown = ("%d.%02d" % (v // 100, v % 100)) if key.startswith("rating") \
                else "{:,}".format(v)
            print("      %2d. %-15s %-11s cid %d" % (i + 1, r["name"], shown, r["cid"]))
        if rows > 5:
            print("      ... %d more" % (rows - 5))
    d = tmrank.decode_rkdata(files[tmrank.RKDATA])
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t)) if t else "(none)"
    print("  %s  next update %s, tally period %s -> %s"
          % (tmrank.RKDATA, fmt(d["stamp"]), fmt(d["tally_from"]), fmt(d["tally_to"])))


def _fixtures():
    """The SHIPPED FALLBACKS, one zero row per list.

    They exist so the screen still opens on a server that has never run the job
    -- `<LN>` 0 closes the rankings scene with no error (rva 0x1750AE), so one
    row is the floor. A zero row is a BLANK LINE, not a player: it must stay
    that way, because this file answers every member and a name baked in here
    would be a phantom in everybody's rankings. See the same rule for
    `b/g/PTL` in `responders._tm_template_blob`.
    """
    out = {}
    for rank_id in sorted(tmrank.LISTS):
        out[tmrank.path_for(rank_id)] = bytes(tmrank.REC)
    out[tmrank.BASE_PATH] = bytes(tmrank.REC)        # the pre-2026-08-20 name
    out[tmrank.RKDATA] = tmrank.build_rkdata({}, 0)  # no counts, no period
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--publish", action="store_true",
                    help="build the live lists and write them to the store")
    ap.add_argument("--fixtures", action="store_true",
                    help="rewrite the shipped zero-row fallbacks in services/tmdata")
    ap.add_argument("--dump", metavar="FILE", help="decode a list file or header")
    ap.add_argument("--players", metavar="JSON",
                    help="take players from this file instead of live state")
    ap.add_argument("--resource-dir", metavar="DIR",
                    help="where the *.tm_collection.json live")
    ap.add_argument("--stamp", type=int, default=None,
                    help="the next-update UNIX time (default: next Sunday)")
    ap.add_argument("--rookie-days", type=int,
                    default=int(os.environ.get("POL_TM_RANK_ROOKIE_DAYS", "30")),
                    help="an account younger than this counts as a rookie")
    ap.add_argument("--no-rollover", action="store_true",
                    help="publish without the weekly write-back (prize_week "
                         "reset, last_rank, champion, ranking prizes)")
    ap.add_argument("--correct", action="store_true",
                    help="RE-publish a wrong tally for the SAME week: rebuild "
                         "the lists, restamp last_rank and the champion, and "
                         "replace UNPAID ranking grants -- but do NOT reset "
                         "prize_week (the new week has already begun)")
    ap.add_argument("--min-games", type=int, default=None,
                    help="games before a VS. Rating list ranks a member "
                         "(default POL_TM_RANK_MIN_GAMES, else %d)"
                         % tmrank.min_games())
    ap.add_argument("--dry-run", action="store_true", help="do not write")
    a = ap.parse_args()
    if a.min_games is not None:
        os.environ["POL_TM_RANK_MIN_GAMES"] = str(max(0, a.min_games))

    if a.dump:
        with open(a.dump, "rb") as f:
            blob = f.read()
        if len(blob) == tmrank.RKDATA_LEN:
            print(json.dumps(tmrank.decode_rkdata(blob), indent=2))
        elif blob and len(blob) % tmrank.REC == 0:
            for i in range(len(blob) // tmrank.REC):
                row = tmrank.decode_row(blob[i * tmrank.REC:(i + 1) * tmrank.REC])
                print("%3d %s" % (i + 1, json.dumps(row)))
        else:
            print("%d bytes is neither %d (the header) nor a whole number of %d"
                  % (len(blob), tmrank.RKDATA_LEN, tmrank.REC))
            return 2
        return 0

    if a.fixtures:
        files = _fixtures()
        for path, blob in sorted(files.items()):
            dest = os.path.join(FIXTURE_DIR, path.replace("/", "_") + ".bin")
            if a.dry_run:
                print("would write %s (%dB)" % (dest, len(blob)))
                continue
            with open(dest, "wb") as f:
                f.write(blob)
            print("wrote %s (%dB)" % (dest, len(blob)))
        return 0

    if not a.publish:
        ap.print_help()
        return 1

    stamp = a.stamp if a.stamp is not None else tmrank.next_update()
    if a.correct and a.stamp is None:
        # Correcting the publish that went out at the START of this week: that
        # one's stamp is the next update, which is what next_update() still
        # returns until next Sunday. Said out loud, because a --correct run a
        # week late would re-rank the wrong week.
        print("--correct: re-publishing the week %s tallied"
              % time.strftime("%Y-%m-%d", time.gmtime(tmrank.tally_start(stamp))))
    players = _players(a, stamp)
    files, counts = _build(players, stamp)
    _report(files, counts, players)
    if a.dry_run:
        print("dry run -- nothing written (store would be %s)" % tmrank.store_dir())
        return 0
    if not players:
        print("REFUSING to publish an empty tally: every list would be 0 rows, "
              "and `<LN>` 0 closes the rankings screen with no error. The "
              "shipped one-row fixtures already say 'nothing here' safely.")
        return 3
    for dest in tmrank.write_store(files):
        print("wrote %s" % dest)
    if a.correct:
        _rollover(players, a.resource_dir, stamp, reset_week=False,
                  replace_grants=True)
    elif not a.no_rollover:
        _rollover(players, a.resource_dir, stamp)
    return 0


def _rollover(players, resource_dir, stamp=None, reset_week=True,
              replace_grants=False):
    """The weekly write-back, run only after a REAL publish.

    The lists just published ARE last week's standings from this moment on:
    `prize_week <- 0` (the Weekly Total restarts), and each member's per-menu
    `last_rank` gets the position they just published at (the client draws the
    moved-up/-down marker by comparing it against this week's row position).

    WARNING: `rating_last` IS NO LONGER WRITTEN (2026-09-13). It was the copy the
    NEXT publish sorted Top 30 / Best Rookies on -- a week stale by design,
    and across the 09-07 rescale it crowned a 2-game player on an old-scale
    9.50. Those lists now sort on the rating at publish (tmrank LISTS banner).

    `reset_week=False, replace_grants=True` is `--correct`: the same stamping
    for a re-published week, without zeroing the Weekly Total a new week has
    already started accumulating, and with UNPAID grants replaced.
    """
    root = resource_dir or os.path.dirname(tmrank.store_dir())
    since = tmrank.tally_start(stamp) if stamp else None
    # The published position per member per menu: tmrank.ranked IS build_list's
    # sort, so these cannot disagree with the files just written.
    ranks = {}
    for rank_id, (menu, _label, _key, _cap) in tmrank.LISTS.items():
        for i, p in enumerate(tmrank.ranked(rank_id, players, since)):
            ranks.setdefault(str(p.get("member_id")), {})[menu] = i + 1
    rolled = 0
    for p in players:
        mid = str(p.get("member_id"))
        fn = os.path.join(root, "%s.tm_collection.json" % mid)
        try:
            with open(fn) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        block = data.setdefault("rank", {})
        if reset_week:
            block["prize_week"] = 0
        block["last_rank"] = {str(m): r
                              for m, r in (ranks.get(mid) or {}).items()}
        tmp = fn + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, fn)
            rolled += 1
        except OSError as exc:
            print("rollover: could not write %s (%r)" % (fn, exc))
    print("rolled over %d member(s): %slast_rank stamped"
          % (rolled, "prize_week <- 0, " if reset_week else ""))
    _write_champion(players, ranks)
    # THE RANKING PRIZES (wired 2026-09-04; priced since 08-20 and never paid).
    # `RankingPrize.BIN` pays "last week's top 30 by VS. Rating" and "last
    # week's top 10 rookies" -- the Top 30 (menu 1) and Best Rookies (menu 2)
    # lists just published. Each placed member gets a GRANT in their prize
    # record; the Prize Center pays it on their next open, riding the /PP=
    # slots the client adds itself (tmprize.announce). The publish id is the
    # week the lists went out, so a re-run of the same publish grants nothing
    # twice. `POL_TM_RANK_PRIZE=0` disables the grant.
    if os.environ.get("POL_TM_RANK_PRIZE", "1") == "0":
        print("ranking prizes: POL_TM_RANK_PRIZE=0 -- not granted")
        return
    try:
        import tmprize
    except ImportError as exc:                                # pragma: no cover
        print("ranking prizes NOT granted: tmprize did not import (%r)" % (exc,))
        return
    saved_dir = os.environ.get("POL_RESOURCE_DIR")
    os.environ["POL_RESOURCE_DIR"] = root
    granted = 0
    try:
        publish_id = tmprize.week_id()
        # A correction visits EVERY member, not just the placed ones: a member
        # the wrong publish placed and the right one does not still holds an
        # unpaid grant, and only a visit can withdraw it.
        mids = sorted(set(ranks) | ({str(p.get("member_id")) for p in players}
                                    if replace_grants else set()))
        for mid in mids:
            by_menu = ranks.get(mid) or {}
            got = tmprize.grant_ranking(mid, publish_id,
                                        top30_rank=by_menu.get(1),
                                        rookie_rank=by_menu.get(2),
                                        say=print, replace=replace_grants)
            if got:
                granted += 1
    finally:
        if saved_dir is None:
            os.environ.pop("POL_RESOURCE_DIR", None)
        else:
            os.environ["POL_RESOURCE_DIR"] = saved_dir
    print("ranking prizes: %d member(s) granted for publish %s (paid at each "
          "member's next Prize Center open)" % (granted, publish_id))


def _write_champion(players, ranks, data_dir=None):
    """Name last week's #1 for the card shop's rank_1 Pack (wired 2026-09-04).

    Read off TM.dll: the shop opener's `/SN=` becomes "<X>'s Pack" -- the label
    of PackPrm record 5, `rank_1 Pack` (kind 3, 100,000 gold, three cards from
    levels 9..11) -- and the save header's 17-byte string at +0x104 seeds the
    same label. This is the Tetra Master "event" the wiki documents: the top
    player's pack in the ordinary Card Shop. The champion is rank 1 of the
    Top 30 list just published (menu 1, last week's rating). Written to
    `<POL_DATA_DIR>/tm-champion.json`, read by `tetramaster._champion`.
    `POL_TM_CHAMPION=0` disables."""
    if os.environ.get("POL_TM_CHAMPION", "1") == "0":
        print("champion: POL_TM_CHAMPION=0 -- not named")
        return None
    top = [mid for mid, by_menu in ranks.items() if by_menu.get(1) == 1]
    if not top:
        print("champion: nobody placed 1st on the Top 30 list -- rank_1 Pack "
              "keeps its current label")
        return None
    mid = top[0]
    name = next((str(p.get("name") or "") for p in players
                 if str(p.get("member_id")) == mid), "")
    if not name.strip():
        print("champion: member %s placed 1st but has no display name -- "
              "not named" % mid)
        return None
    root = data_dir or os.environ.get("POL_DATA_DIR") \
        or os.path.dirname(os.path.dirname(tmrank.store_dir()))
    path = os.path.join(root, "tm-champion.json")
    try:
        import tmprize
        week = tmprize.week_id()
    except Exception:
        week = 0
    blob = {"member_id": mid, "name": name.strip()[:16], "week": week}
    try:
        os.makedirs(root, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(blob, f)
        os.replace(tmp, path)
    except OSError as exc:
        print("champion: could not write %s (%r)" % (path, exc))
        return None
    print("champion: member %s %r is #1 -- the Card Shop now sells \"%s's "
          "Pack\" (rank_1 Pack) and every save rewrite carries the name at "
          "+0x104 (%s)" % (mid, blob["name"], blob["name"], path))
    return blob


if __name__ == "__main__":
    sys.exit(main())
