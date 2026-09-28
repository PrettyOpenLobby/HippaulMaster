"""Career statistics: streaks, Elo, the per-match counters, card growth and syncing them into the
save.
"""
import os
import random
import time
from .deps import tmprize, tmroll, tmsave
from . import boardrules, collection, common, matchend, pushqueue, savefile, vscom


def _com_elo(char_index):
    """A COM opponent's fixed Elo: the rung of the deck this server deals it
    (`_com_deck_record`; the ladder's level bands rise), spread evenly from
    `POL_TM_ELO_COM_LO` to `POL_TM_ELO_COM_HI` (VS. Rating x100, default 150
    and 275). A COM with no `/Com=` index sits at the middle."""
    import tmrank as _tr
    lo = _tr._env_float("POL_TM_ELO_COM_LO", 150)
    hi = _tr._env_float("POL_TM_ELO_COM_HI", 275)
    frac = 0.5
    try:
        ladder = vscom._com_deck_ladder()
        if char_index is not None and len(ladder) > 1:
            frac = ladder.index(vscom._com_deck_record(int(char_index))) / (len(ladder) - 1)
    except (TypeError, ValueError):
        pass
    return _tr.elo_of_display(lo + (hi - lo) * frac)


def _match_elos(roster, scores, n, n_solo=None, now=None):
    """{roster index: new Elo} for one finished match, or {} when the match
    does not rate: Elo off, a result from before `tmrank.elo_from()`, no
    board scores, or the same member in two seats.

    Seats are `scores`' order: in PvP the roster IS the seats; in a VS. COM
    game the one human is seat 0 and seat s >= 1 is the COM picked at `/Com=`
    position s - 1 (`_COM_GAME`, as `_watch_seats` reads it)."""
    import tmrank as _tr
    now = int(time.time() if now is None else now)
    if (not _tr.elo_enabled() or now < _tr.elo_from() or not scores
            or not common._env_int("POL_TM_RANK_RATING", 1) or not roster):
        return {}
    mids = [m for m, _v in roster]
    if len(set(mids)) != len(mids):
        return {}
    ratings, ks = [], []
    blocks = {}
    for i, mid in enumerate(mids):
        blk = (collection._collection_load(mid).get("rank") or {})
        blocks[i] = blk
        ratings.append(_tr.elo_of(blk))
        ks.append(_tr.elo_k(blk.get("elo_games")))
    if n_solo:
        coms = (vscom._COM_GAME.get(pushqueue._push_key(mids[0])) or {}).get("coms") or []
        for s in range(1, len(scores)):
            ratings.append(_com_elo(coms[s - 1] if s - 1 < len(coms) else None))
            ks.append(None)
    if len(ratings) != len(scores):
        common._say("tm:   ...Elo skipped: %d seat(s) rated, %d scores" % (len(ratings), len(scores)))
        return {}
    new = _tr.elo_match(ratings, scores, ks)
    common._say("tm:   ...Elo: %s" % ", ".join(
        "%s %.0f->%.0f" % ("seat %d" % i, ratings[i], new[i]) for i in range(len(new))))
    return {i: new[i] for i in range(len(mids))}


def _bump_result_stats(member_id, score, place=None, opponents=None,
                       combo=0, elo=None):
    """One finished match for `member_id`: advance the career counters and
    return `(games, score_total)`.

    The first two are the numbers `@ResultData`'s own-section `/D=` occ 5/6
    carry, and occ 5 is a DIVISOR (see `_resultdata_body`) -- this function
    never returns a games count below 1. Persisted in the collection's `rank`
    block, beside the ranking stats `tools/tmrank.py` reads, because they are
    the same kind of thing: numbers only the server can know.

    `place` is this member's finishing POSITION in the match (1 = won), from
    `_placements`. It feeds `avg_rank`, and passing None simply leaves the
    average where it was -- a caller that cannot say where somebody finished
    must not be able to move it.

    WARNING: **THE SAVE IS NOT REWRITTEN FROM HERE** (`sync_save=False`, which exists
    to avoid rebuilding the card block for a header change). `_save_sync_stats`
    delivers these into the save's own stat fields instead, and the caller runs
    it once per member per match -- see the banner there for why a stat that
    only reaches the JSON is a stat the player never sees.
    """
    score = max(0, int(score or 0))
    if not common._env_int("POL_TM_RESULT_STATS", 1):
        return 1, score
    try:
        data = collection._collection_load(member_id)
        block = data.setdefault("rank", {})
        import tmrank as _tr
        # Read BEFORE `games` moves: a block from before `tiled_games` existed
        # derives it from the counts it already holds (tmrank.tiled_games_of).
        tiled_before = _tr.tiled_games_of(block)
        games = max(1, int(block.get("games") or 0) + 1)
        total = max(0, int(block.get("score_total") or 0) + score)
        block["games"], block["score_total"] = games, total
        # WHEN they last played: Top 30 and Best Rookies are "last week's"
        # lists and only rank members who played in the tallied week
        # (tmrank._eligible). Every result counts, COM games included.
        block["last_played"] = int(time.time())
        # ...and HOW MANY this week, for the board's "This Week So Far"
        # (tmrank.week_games_of): a stamp from an older week starts at 0.
        wk = _tr.week_start(block["last_played"])
        block["week_games"] = (1 + max(0, int(block.get("week_games") or 0))
                               if block.get("week_of") == wk else 1)
        block["week_of"] = wk
        # ...the Elo `_match_elos` worked out for this seat (None = this
        # result does not rate), which `rating_of` below shows
        if elo is not None:
            block["elo"] = round(float(elo), 2)
            block["elo_games"] = max(0, int(block.get("elo_games") or 0)) + 1
        # ...AND THE VS. RATING, which is what the ranking lists sort on
        # (tmrank.LISTS: menus 0/1/2 read it, x100 fixed point). SE's formula
        # is unrecoverable; the average score x100 is ours by construction
        # (the csid precedent), matches the career screen's own average
        # (score_total / games -- the same division the client runs at
        # 0x10F6A5), and lands on the scale of the one measured example
        # ("2.96"). POL_TM_RANK_RATING=0 stops writing it.
        if common._env_int("POL_TM_RANK_RATING", 1):
            # WARNING: RESCALED 2026-09-07 (live testing): the
            # client's own Technical Rating (0xCC72F) is written for ratings
            # well UNDER 4.00 -- it multiplies (400 - avg opponent rating) --
            # and average tiles x100 (300..800) printed 0.00 on every result
            # screen. `tmrank.rating_of` is the ONE formula: 1.00 + 3.00 x the
            # share of the board held, over the career (`tiles_total`).
            block["tiles_total"] = (max(0, int(block.get("tiles_total") or 0))
                                    + boardrules._board_tiles(1 + max(0, int(opponents or 0))))
            # ...and HOW MANY games those tiles cover, so rating_of can count
            # the rest at 16 each instead of dividing a career's score by a
            # few games' tiles (one tester's flat 4.00, 2026-09-13).
            block["tiled_games"] = tiled_before + 1
            block["rating"] = max(0, int(_tr.rating_of(block)))
        # ...AND THE AVERAGE RANK, the mean finishing position x100. This is
        # the stat `CoPrm.BIN` gates every TITLE on and the one the Player Data
        # screen calls 平均順位 / `Average Rank`, and it has never had a
        # producer -- which is why the save's +0x30 has always been 0 and why
        # the client's own title has always been computed against a free pass
        # (`tm_title.py`, and `<=` against zero matches every threshold).
        #
        # VERIFIED: **AND THE DEFINITION IS NOW MEASURED, NOT INFERRED** (2026-08-24).
        # It was reasoned from `CoPrm.BIN`'s 300..100 thresholds, `PlPrm.BIN`'s
        # COM opponents and the guidebook's prize formula; then the client's own
        # recomputation at `0xCC814` was read, and it is
        # `mean((ring[i] + 2) * 50)` over a 32-slot result ring -- i.e. **mean
        # finishing place x100**, exactly the scale that was guessed, with
        # draws on the half-steps. See `_placements`.
        #
        # WARNING: **100 IS THE FLOOR, NOT 0.** The client's arithmetic cannot produce
        # below 1.00, so a 0 here is out of domain -- which is what an unwritten
        # save `+0x30` has always been.
        #
        # `place_total` is kept alongside (already x100) so the average stays
        # exact across restarts; storing only the rounded average would make a
        # later correction impossible.
        if place is not None and common._env_int("POL_TM_AVG_RANK", 1):
            # WARNING: THE DIVISOR IS GAMES THAT PLACED, NOT GAMES PLAYED. `games`
            # counts EVERY result; `place_total` only accumulates when the
            # caller could say where the member finished (`place is not None`
            # -- the guard right above). Dividing the one by the other
            # deflates the average by however many results carried no place,
            # and on real data it went far enough under 1.00 to be pinned by
            # the `max(100, ...)` floor: member 3, 2026-08-25, place_total
            # 1150 over games 18 = 63 -> floored to 100, i.e. "a perfect 1.00
            # average" for a player whose every recorded placement was a 2nd
            # or a draw. The floor was hiding the arithmetic, so the number
            # looked plausible and was not.
            #
            # WARNING: This matters beyond the Status screen: the title picker
            # (`tm_title.py`, rva 0x110B10) tests `avg_rank <= threshold`, so
            # a too-low average clears every row and INFLATES the title.
            places = max(0, int(block.get("place_total") or 0)) + max(100, int(place))
            placed = int(block.get("placed") or 0)
            if not placed and block.get("place_total"):
                # MIGRATION, once, for a block written before `placed` existed.
                # Derived from what is already stored -- `place_total` and the
                # average we currently believe -- so it reproduces today's
                # number rather than inventing a different one. The count is
                # unrecoverable exactly (nothing recorded which results
                # carried a place); this is the closest consistent pair.
                prev_avg = max(100, int(block.get("avg_rank") or 100))
                placed = max(1, round(int(block["place_total"]) / prev_avg))
                common._say("tm:   ...member %s: seeding rank.placed = %d from the "
                     "stored place_total %d / avg_rank %d (one-time; the old "
                     "average divided by GAMES PLAYED, not games placed)"
                     % (member_id, placed, block["place_total"], prev_avg))
            placed += 1
            block["place_total"], block["placed"] = places, placed
            block["avg_rank"] = max(100, places // placed)
            # ...AND THE STREAK PAIR, off the same result byte the client uses.
            # `consec_wins` is SIGNED: negative is a losing run, which is how one
            # field serves both Playdat labels. `streak` is its running max, and
            # the client only reconsiders the max on a non-losing result
            # (0xCC673/0xCC67E fall into 0xCC6AA; the loss arm jumps past it).
            res = matchend._result_code(place)
            consec = matchend._streak_next(block.get("consec_wins"), res)
            block["consec_wins"] = max(-32768, min(32767, consec))
            if res < 2:
                block["streak"] = max(int(block.get("streak") or 0),
                                      block["consec_wins"])
        # ...AND OPPONENTS FACED, which is NOT the games count. `0xCC7AC` adds
        # `N - 1` per match and `0xCC7E4` divides Grand Total by it, so Average
        # Prize is per OPPONENT. Authoring it also keeps that division away from
        # zero -- it is the result-screen divide-by-zero's divisor.
        if opponents is not None and common._env_int("POL_TM_AVG_RANK", 1):
            block["opponents"] = (max(0, int(block.get("opponents") or 0))
                                  + max(0, int(opponents)))
        # MOST COMBOS -- a career MAX, exactly as `0xD029B` keeps it. Tallied
        # unconditionally so the history exists; only the SAVE write is gated
        # (see `_note_combo` for what is inferred and what is measured).
        if combo:
            block["most_combos"] = max(int(block.get("most_combos") or 0),
                                       int(combo))
        collection._collection_store(member_id, data, sync_save=False)
        return games, total
    except Exception as e:
        common._say("tm: result stats NOT recorded for member %s (%s) -- serving the "
             "one-game floor" % (member_id, e))
        return 1, score


def _grow_used_cards(member_id, used_rows):
    """Grow the cards `member_id` USED this match toward their CardPrm ceilings,
    and persist -- "the cards you use will grow too" (SE guidebook).

    Growth is server-authored: BOTH clients only store/render the absolute stats
    we send (via the save / `@Card`) and compute no growth themselves (PC
    0xB2CF0-only, PS2 0x2b1c80-only). So the server raises the stat and the save
    rewrite (inside `_collection_store`) carries it to the client on its next
    reload. The AMOUNT is `tmroll`'s tunable policy; the mechanism (grow toward
    the ceiling, never past) is measured.

    `used_rows` are the member's selected-hand wire rows (b"id|atk|...", up to 8
    pipe values). Each is matched to a COLLECTION instance by (id + the four
    combat stats) -- the erase/sell identity -- preferring an exact match so a
    duplicate id grows the right copy; a used card we hold no record of is
    skipped (drift), never invented. Returns the number of cards that grew.
    `POL_TM_CARD_GROW=0` disables growth.
    """
    if member_id is None or tmroll is None or not common._env_int("POL_TM_CARD_GROW", 1):
        return 0
    used = []
    for r in used_rows or []:
        try:
            c = [int(x) for x in bytes(r).split(b"|") if x != b""]
        except (ValueError, TypeError):
            continue
        if len(c) >= 5:
            used.append(c)
    if not used:
        return 0
    try:
        data = collection._collection_load(member_id)
        have = data.get("cards")
        have = [list(c) for c in have] if isinstance(have, list) else []
        rnd = random.Random()        # per-instance growth; no reproducibility needed
        consumed, grown = set(), []
        for c in used:
            cid, stats = c[0], c[1:5]
            idx = next((i for i, row in enumerate(have)
                        if i not in consumed and row and row[0] == cid
                        and list(row[1:5]) == stats), None)
            if idx is None:          # fall back to any same-id instance
                idx = next((i for i, row in enumerate(have)
                            if i not in consumed and row and row[0] == cid), None)
            if idx is None:
                continue             # a used card we have no record of
            row = have[idx]
            while len(row) < 8:
                row.append(0)
            new_row, deltas = tmroll.grow_card(rnd, row)
            consumed.add(idx)
            if deltas:
                have[idx] = new_row
                grown.append((cid, deltas))
        if grown:
            data["cards"] = have
            collection._collection_store(member_id, data)      # rewrites the save
            common._say("tm: member %s -- %d used card(s) GREW toward their ceiling: %s"
                 % (member_id, len(grown),
                    "; ".join("#%d %s" % (c, d) for c, d in grown)))
        return len(grown)
    except Exception as e:                                   # never fail a match
        common._say("tm: card growth NOT applied for member %s (%s)" % (member_id, e))
        return 0


def _save_sync_stats(member_id):
    """Deliver the career stats we hold into the SAVE the client reads.

    WARNING: **THE GAP THIS CLOSES: WE HAVE BEEN COMPUTING STATISTICS AND NEVER
    HANDING THEM OVER.** `_bump_result_stats` and `_bump_prize` maintain games,
    score, VS. Rating, average rank and the prize-money tallies in the
    collection's `rank` block; `tmprize` maintains the prize-point balance.
    `tools/tmrank.py` reads the first set once a week for the ranking screens --
    and NOTHING read either of them for `U/g/TM0DataFile`, which is the only
    place Player Data -> Status gets its numbers from. So the screen showed
    zeros for figures this server had on disk, which is a delivery bug, not a
    missing feature.

    Offsets are `tmsave.STAT_FIELDS`, measured by the self-locating save
    probe. Writes go through `_save_patch_fields`, so this is
    the same atomic header patch `@Opt=` and the guild write already use -- the
    card block is untouched and no rebuild happens.

    WARNING: **ONLY FIELDS WE ACTUALLY PRODUCE ARE WRITTEN.** `Biggest Prize`,
    `Average Prize` and `Consecutive Wins` have offsets but no producer, and a
    zero written into them is indistinguishable from the zero already there --
    so they are named in `tmsave.STAT_FIELDS` and deliberately absent below.
    Average rank is written only once `games > 0`: a player who has never
    played has no average, and writing one anyway would be inventing the single
    number every title in the game is gated on.

    `POL_TM_SAVE_STATS=0` disables the whole write-through.

    KEY: **AND THE TWO 17-BYTE NAME SLOTS, WHICH ARE OURS TO FILL AND WHICH WE
    HAVE ALWAYS SHIPPED AS ZEROS.** Reported live, 2026-08-25: "'Memorable Win'
    just shows my own name". It is not coming from the save: both
    slots read ZERO in every member's file on prod.

    Measured in `TM.dll.unpacked` the same night: `0x5224CD4` (save +0x104) and
    `0x5224CE5` (save +0x115) have **exactly one reference each image-wide**,
    `0x101508` and `0x10151E`, and both are a `push` feeding the `0x1E2060`
    memcpy that copies **save -> object** (`[esi+0x104]`, `[esi+0x116]`, 0x11
    bytes each). Nothing in the client ever writes them back. So they are
    SERVER-AUTHORED strings, exactly like the stat dwords above -- the same
    delivery gap this function exists to close, one tier along.

    WARNING: WHICH SLOT IS "Memorable Win" IS NOT SETTLED. `tmsave.STRING_FIELDS`
    calls it a *candidate* for +0x104 and names no owner for +0x115, and after
    the `/Rule=` occ-0 retraction this file is not going to guess a field from
    position again. `POL_TM_SAVE_STR_104` / `POL_TM_SAVE_STR_115` write a
    literal into each slot so ONE login settles it: set them to two
    distinguishable strings, open Player Data -> Status, and read which label
    shows which. Unset (the default) writes nothing and leaves today's zeros.
    Once the mapping is measured, the producer replaces the knob.
    """
    if member_id is None or tmsave is None:
        return {}
    _str_probe = {}
    for _off, _env in ((0x104, "POL_TM_SAVE_STR_104"),
                       (0x115, "POL_TM_SAVE_STR_115")):
        _val = os.environ.get(_env)
        if _val:
            # 16 characters + the NUL the 17-byte slot holds. latin1 because
            # the encoding of these slots is unmeasured too -- a probe should
            # put ASCII in and read ASCII out, not add a variable.
            _str_probe[_off] = (17, _val.encode("latin1", "replace")[:16]
                                + b"\x00")
    if not common._env_int("POL_TM_SAVE_STATS", 1):
        return {}
    try:
        block = (collection._collection_load(member_id) or {}).get("rank") or {}

        def _n(key):
            try:
                return max(0, int(block.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        def _sn(key):
            """...and the SIGNED reader, for the one field that runs negative."""
            try:
                return max(-32768, min(32767, int(block.get(key) or 0)))
            except (TypeError, ValueError):
                return 0

        games = _n("games")
        prize_total = _n("prize_total")
        opponents = _n("opponents")
        rating = _n("rating")
        if games:
            # A block written before 2026-08-22 carries games/score_total and no
            # stored rating. ONE derivation, and it is `tmrank.stats_of`'s --
            # the ranking lists and this save must not disagree about a player's
            # rating, and they would the moment two copies of this formula
            # drifted. See that function's own note.
            try:
                import tmrank as _tr
                rating = max(0, int(_tr.stats_of({"rank": block})["rating"]))
            except Exception:
                rating = 0
        # WARNING: WIDTHS ARE THE PARSER'S, and the word fields are written AS WORDS
        # (`write_field` grew width 2 for exactly this): a dword at +0x78 would
        # destroy Winning Streak at +0x7A. `tmsave.STAT_FIELDS_WORD`.
        changes = {
            tmsave.RATING_OFF: (4, rating),
            tmsave.PRIZE_TOTAL_OFF: (4, prize_total),
            tmsave.VS_GAMES_OFF: (2, min(games, 0xFFFF)),
        }
        if games and _n("avg_rank"):
            changes[tmsave.AVG_RANK_OFF] = (4, _n("avg_rank"))
        if _n("biggest_prize"):
            changes[tmsave.BIGGEST_PRIZE_OFF] = (4, _n("biggest_prize"))
        if opponents:
            # AVERAGE PRIZE is the client's own division -- Grand Total over
            # OPPONENTS FACED, not over matches (0xCC7E4). Writing the divisor
            # as well as the quotient keeps the result screen's own recompute
            # (0x10F6A5) off a zero, which is the result-screen crash.
            changes[tmsave.OPPONENTS_OFF] = (2, min(opponents, 0xFFFF))
            changes[tmsave.AVERAGE_PRIZE_OFF] = (4, prize_total // opponents)
        # THE STREAK PAIR. `consec_wins` is SIGNED -- `write_field` masks, so a
        # losing run stores as two's complement exactly as `0xCC68A` writes it.
        # Both are written whenever either is non-zero, because 0 is a real
        # value for this pair (a reset) and skipping it would strand the old one.
        consec, streak = _sn("consec_wins"), _n("streak")
        if consec or streak:
            changes[tmsave.CONSEC_WINS_OFF] = (2, consec)
            changes[tmsave.STREAK_OFF] = (2, min(streak, 0xFFFF))
        # MOST COMBOS -- OFF BY DEFAULT. The field is measured, the UNIT is not
        # (`_note_combo`). `POL_TM_MOST_COMBOS=1` puts it on the screen, which
        # is how the unit gets settled: play a match with a known chain and read
        # the number back.
        if _n("most_combos") and common._env_int("POL_TM_MOST_COMBOS", 0):
            changes[tmsave.MOST_COMBOS_OFF] = (1, min(_n("most_combos"), 0xFF))
        # The prize-point BALANCE is the closest thing we hold to `Prize Points
        # Acquired`, and it is not the same number -- ours drifts UP relative to
        # the client's the moment a player spends, because the Prize Center's
        # debit is client-side and no message reports it (`tmprize`'s own
        # banner). Written anyway, because a balance that is right until the
        # first purchase beats a zero that is never right, and named as the
        # approximation it is rather than quietly labelled "acquired".
        if tmprize is not None and common._env_int("POL_TM_SAVE_PRIZE_POINTS", 1):
            try:
                pts = int((tmprize.load(member_id) or {}).get("points") or 0)
                changes[tmsave.PRIZE_POINTS_OFF] = (4, max(0, pts))
            except Exception:
                pass
        # THE NAME-SLOT PROBE, if it is armed. See this function's banner:
        # +0x104 and +0x115 are server-authored and we have only ever written
        # zeros, which is why "Memorable Win" has nothing of ours to show.
        # Which slot carries which label is UNMEASURED -- set the two env vars
        # to different strings and one login settles it.
        # THE DECK SET NAMES ride every sync too, so a stale stamp in a
        # save (the 2026-09-06 champion write) is cleared on the next login
        # without waiting for a collection change. An armed probe wins.
        for _off, _v in collection._deck_name_fields(member_id).items():
            changes.setdefault(_off, _v)
        changes.update(_str_probe)
        if _str_probe:
            common._say("tm: member %s save STRING PROBE armed -> %s. Open Player "
                 "Data -> Status and read which label shows which; that is the "
                 "measurement (tmsave.STRING_FIELDS calls +0x104 a CANDIDATE "
                 "for Memorable Win and names no owner for +0x115)."
                 % (member_id, ", ".join(
                     "+0x%03X=%r" % (o, v[1]) for o, v in
                     sorted(_str_probe.items()))))
        moved = savefile._save_patch_fields(member_id, changes)
        if moved:
            def _shown(v):
                return (repr(bytes(v).rstrip(b"\x00").decode("latin1"))
                        if isinstance(v, (bytes, bytearray)) else v)
            common._say("tm: member %s save stats -> %s"
                 % (member_id, ", ".join(
                     "%s %s->%s" % (
                         {0x104: "deck tab 1", 0x115: "deck tab 2"}.get(
                             o, tmsave.STAT_FIELDS.get(o, hex(o))),
                         _shown(a), _shown(b))
                     for o, (a, b) in sorted(moved.items()))))
        return moved
    except Exception as e:
        common._say("tm: save stats NOT delivered for member %s (%s) -- the Player "
             "Data screen will keep showing the old numbers" % (member_id, e))
        return {}
