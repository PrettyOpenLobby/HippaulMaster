"""Selftests: packs, seating, the heartbeat, the table audit and reconcile, @GameML, peers and chat.
"""
import uuid
import json
import tm_cardprm
import time
from . import (
    cardshop, cardtables, common, dispatch, matchmaking, peers, protocol, pushqueue,
    reservation, roomchat, seating, tableaudit, tablerow, vscom,
)


def _selftest_blocks():
    """The block writers must never grow the field past its authored length.

    This is the assertion the crash of 2026-08-20 did not have. A 64-character
    block fills `+0x28` to the end of the 104-byte record and leaves the client
    an unterminated string; both players' clients died on the first one.
    """
    ok = True
    authored = "BAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFBAABIGJPAAAAA"      # 49
    for name, got in (
            ("occupancy", tablerow._occupancy_block(authored, [(1, 0xAB12CD56EB0F5932)])),
            ("occupancy/empty", tablerow._occupancy_block(authored, [])),
            ("settings", tablerow._settings_block({"ll": 10, "lu": 50}, authored))):
        if len(got) > len(authored):
            common._say("FAIL: %s grew the block %d -> %d; +0x28 runs to the end of "
                  "the record and an unterminated block CRASHES THE CLIENT"
                  % (name, len(authored), len(got)))
            ok = False
    # ...and the id must be readable back exactly as the client reads it.
    import tmroom
    blk = tablerow._occupancy_block(authored, [(1, 0xAB12CD56EB0F5932)])
    if tmroom.client_reads_id(blk[14:30]) != 0xAB12CD56EB0F5932:
        common._say("FAIL: the occupancy id does not survive the block write")
        ok = False
    if tmroom.client_reads_id(tablerow._occupancy_block(authored, [])[14:30]) != 0:
        common._say("FAIL: an empty table must publish id 0 -- non-zero is what the "
              "state-1 arm treats as 'somebody is here'")
        ok = False
    return ok


def _selftest_packs():
    """THE PACK CONTENTS, against `PackPrm.BIN` itself.

    This is the guard the uniform-draw bug did not have: a Beginner's Pack that
    yields a level-19 card is what live testing reported on 2026-08-20, and the
    only reason it went unnoticed is that nothing asserted a pack's cards come
    from the pack's own draw table. See the banner above `_draw_pack`.
    """
    ok = True
    table = cardtables._packprm()
    # 78 packs in the PC build's table, 77 in the PS2's -- the count is the
    # table's own header word, so either is a whole decode.
    if len(table) not in (77, 78):
        common._say("FAIL: PackPrm.BIN must decode to 77 or 78 records -- got %d"
             % len(table))
        return False

    # The four fields the shop actually reads, on a record a tester bought.
    beg = table[1]
    if (beg["price"], beg["cards"], beg["kind"]) != (1000, 10, 1):
        common._say("FAIL: /No=1 must be price 1000, 10 cards, kind 1 -- got %r"
             % ((beg["price"], beg["cards"], beg["kind"]),))
        ok = False
    if [r[0] for r in beg["draw"]] != [1, 2, 3, 5]:
        common._say("FAIL: /No=1 draws levels 1/2/3/5 -- got %r"
             % ([r[0] for r in beg["draw"]],))
        ok = False

    # EVERY ROW BUT ONE RESOLVES TO A REAL POOL. This is what makes the
    # (level, group, category) reading a decode rather than a shape that fits:
    # 308 rows over 77 records, and 307 of them select real cards. The one
    # exception is SE's own data -- record 27, Quina's COM deck, kind 0, never
    # sold -- and it is pinned here so that a wider break cannot hide behind it.
    empty = []
    for i, rec in enumerate(table):
        prev = -1
        for level, group, cat, cum in rec["draw"]:
            if cum <= prev:          # unreachable filler after the table ends
                continue
            prev = cum
            if not cardshop._pack_pool(level, group, cat):
                empty.append((i, rec["kind"], (level, group, cat)))
    # With the PC build's tables every row resolves; the PS2 build's has one
    # exception, record 27 (Quina's COM deck, kind 0, never sold).
    if empty not in ([], [(27, 0, (1, 3, 5))]):
        common._say("FAIL: every draw row must select real cards (the PS2 table's one "
             "exception is record 27, level 1 / group 3 / category 5) -- got %r"
             % (empty,))
        ok = False

    # And the draw obeys it. Levels first -- the reported symptom.
    lv = set()
    for _ in range(120):
        for card in cardshop._draw_pack(1):
            lv.add(tm_cardprm.row(card[0])[5])
    if not lv <= {1, 2, 3, 5}:
        common._say("FAIL: a Beginner's Pack yielded levels %r; the file says 1/2/3/5"
             % (sorted(lv),))
        ok = False

    # Then the themed packs, whose names state their own answer.
    for no, cat, what in ((21, 2, "Summoner's"), (22, 3, "Warrior's"),
                          (23, 4, "Voyager's"), (24, 7, "FFIX Celebrity")):
        got = set()
        for _ in range(60):
            for card in cardshop._draw_pack(no):
                got.add(tm_cardprm.row(card[0])[7])
        if got != {cat}:
            common._say("FAIL: the %s Pack must draw category %d only -- got %r"
                 % (what, cat, sorted(got)))
            ok = False

    # The Prize Center card id, off the same record at +0x20.
    if table[74]["card"] != 152 or table[74]["price"] != 10000:
        common._say("FAIL: the dearest Prize Center record is card 152 at 10000 -- "
             "got %r at %r" % (table[74]["card"], table[74]["price"]))
        ok = False

    # A COM opponent's deck is FIVE VARIED cards from a real deck record, never
    # a single Prize Center card five times over (reported live 2026-09-02: /Com=23
    # -> record 47 = Maechen x5, because the base+offset mapping walked past
    # the deck ladder into the kind-5 records). Every character index -- high
    # ones included -- must resolve to a real deck.
    ladder = vscom._com_deck_ladder()
    if len(ladder) < 2 or not all(vscom._is_com_deck(cardtables._pack_rec(n)) for n in ladder):
        common._say("FAIL: the COM deck ladder must be real deck records, got %r"
             % (ladder,))
        ok = False
    for ci in (1, 6, 11, 21, 22, 23, 40):
        rec_no = vscom._com_deck_record(ci)
        if not vscom._is_com_deck(cardtables._pack_rec(rec_no)):
            common._say("FAIL: /Com=%d resolved to record %d, which is NOT a deck "
                 "(five identical cards is the bug this guards)" % (ci, rec_no))
            ok = False
        deck = [r.split(b"|")[0] for r in vscom._com_deck_rows(ci)]
        if len(deck) != 5:
            common._say("FAIL: a COM deck is five cards, /Com=%d gave %d"
                 % (ci, len(deck)))
            ok = False
        # A real deck record's pool is many cards, so its draws are not all
        # one id -- the degenerate single-card deck the tester saw. One deal
        # of a weighted pool can come up five of a kind by chance (record 27
        # did, in one of twelve runs of this suite), so look at several deals:
        # a single-card record gives one id however often it is drawn.
        ids = set(deck)
        for _deal in range(7):
            ids.update(r.split(b"|")[0] for r in vscom._com_deck_rows(ci))
        if len(ids) == 1:
            common._say("FAIL: /Com=%d dealt five identical cards (id %r) from record "
                 "%d -- the single-card Prize Center bug" % (ci, deck[0], rec_no))
            ok = False

    if ok:
        common._say("packs: 77 records, 307 of 308 draw rows select real cards, "
             "/No=1 yields only levels 1/2/3/5, and every COM deck is a real "
             "%d-record ladder (no single-card decks)" % len(ladder))
    return ok


def _selftest_seating():
    """A settings publish must NOT un-seat anybody.

    Measured live 2026-08-20: `@GameEN=` seated the player and the other client
    correctly drew "Setting Up", then the `@Tet=`/`@Tab=` pair rebuilt the row
    from the AUTHORED fixture and published state 7 / id 0 -- "once it's done it
    goes back to showing as empty for the other person". This replays that exact
    order.

    WARNING: Sandboxed like `tmroom.selftest`: `note_table` publishes, and a test that
    can reach the snapshot two containers share has already cost this project one
    prod incident.
    """
    import os as _os, tempfile, tmroom
    ok = True
    saved = (tmroom._KEY, tmroom._OWNER[0],
             dict(tmroom._TABLES), dict(tmroom._PEERS),
             dict(tmroom._RECORDS), dict(tmroom._ROOMS_SEQ))
    saved_seated = dict(seating._SEATED)
    try:
        tmroom._KEY = "tm:selftest:tm-seat:" + uuid.uuid4().hex
        tmroom._OWNER[0] = True
        tmroom._TABLES.clear(); tmroom._PEERS.clear()
        tmroom._RECORDS.clear(); tmroom._ROOMS_SEQ.clear(); seating._SEATED.clear()
        mid, ident = 0x860FB3E2A2, 0xAB12CD56EB0F5932
        tmroom.note_member(mid, ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                                 "0x0000002000000001", "T"])
        tmroom.note_room_peer("#TM0R001", peers.peer_guid("UKXDBA266"))

        def row():
            live = dict(tmroom.tables("#TM0R001")).get("#TM0T001")
            return live and (live[3], tmroom.client_reads_id(live[6][14:30]))

        dispatch.handle_line(b"41000100@GameEN=/NN=%016X/L=1" % ident,
                    peer_nick="UE7QN1N9G", member_id=mid)
        if row() != ("1", ident):
            common._say("FAIL: @GameEN= must seat and publish the id, got %r" % (row(),))
            ok = False
        dispatch.handle_line(b"14000000@Tet=/tl=0@Tab=/in=0/lu=99999/ll=0/au=300/al=100",
                    peer_nick="UE7QN1N9G", member_id=mid)
        # 0x14 is the CONFIRM. Since 2026-08-22 confirm KEEPS state 1 (default
        # POL_TM_TABLE_STATE_CONFIRMED=1): +0x108 now fills (proven live), so the
        # state-1 owner tile unlocks from the reservation slot WITHOUT the
        # state-2 flip that closed joins and locked out guests. It must NOT
        # un-seat, and the id must survive. (<3 seated, so inplay is False.)
        if row() != ("1", ident):
            common._say("FAIL: the confirm must keep state 1 (owner tile unlocks from "
                  "+0x108; joins stay open) WITHOUT un-seating -- got %r "
                  "(want state 1, same id)." % (row(),))
            ok = False
        dispatch.handle_line(b"41000000@GameExit=", peer_nick="UE7QN1N9G", member_id=mid)
        # THE AUTHORED 7 AGAIN -- 6151fa3c's clamp is RETRACTED (measured:
        # its heal TD put "Setting up" on a free table at 22:18; state 7 is the
        # client's deterministic empty tile, display code 0 pre-stored). This
        # assertion has now flip-flopped once; it checks the ONE constant,
        # tmroom.EMPTY_TABLE_STATE, whose banner carries the whole history.
        import tmroom as _tr
        if row() != (str(_tr.EMPTY_TABLE_STATE), 0):
            common._say("FAIL: leaving must restore the AUTHORED state and clear the "
                  "id, got %r" % (row(),))
            ok = False
    except Exception as exc:
        common._say("FAIL: seating selftest raised %r" % (exc,)); ok = False
    finally:
        (tmroom._KEY, tmroom._OWNER[0]) = saved[0], saved[1]
        for d, v in ((tmroom._TABLES, saved[2]), (tmroom._PEERS, saved[3]),
                     (tmroom._RECORDS, saved[4]), (tmroom._ROOMS_SEQ, saved[5]),
                     (seating._SEATED, saved_seated)):
            d.clear(); d.update(v)
        tmroom._SHARED.forget()
    return ok


def _selftest_cancel():
    """`@GameQT=/ID=` must RELEASE the sender's seat; bare `@GameQT=` must not.

    Static read 2026-08-22: sender 0x84050 appends
    `/ID=<16-hex table id>`; the bare form (0x83FC0) is the generic
    dialog-confirm shared by rules-Confirm, rules-accept AND the tile-menu
    cancel -- byte-identical, so the bare form must stay a pure confirm. Until
    the cancel arm existed, a `/ID=` cancel was answered as a confirm and the
    freed-on-screen table came back occupied on the next TD -- the live
    2026-08-22 report.

    WARNING: Sandboxed like `_selftest_seating`: the release republishes through the
    shared registry.
    """
    import os as _os, tempfile, tmroom
    ok = True
    saved = (tmroom._KEY, tmroom._OWNER[0],
             dict(tmroom._TABLES), dict(tmroom._PEERS),
             dict(tmroom._RECORDS), dict(tmroom._ROOMS_SEQ))
    saved_seated = dict(seating._SEATED)
    try:
        tmroom._KEY = "tm:selftest:tm-cancel:" + uuid.uuid4().hex
        tmroom._OWNER[0] = True
        tmroom._TABLES.clear(); tmroom._PEERS.clear()
        tmroom._RECORDS.clear(); tmroom._ROOMS_SEQ.clear(); seating._SEATED.clear()
        mid, ident = 0x860FB3E2A2, 0xAB12CD56EB0F5932
        tmroom.note_member(mid, ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                                 "0x0000002000000001", "T"])
        tmroom.note_room_peer("#TM0R001", peers.peer_guid("UKXDBA266"))

        def row():
            live = dict(tmroom.tables("#TM0R001")).get("#TM0T001")
            return live and (live[3], tmroom.client_reads_id(live[6][14:30]))

        dispatch.handle_line(b"41000100@GameEN=/NN=%016X/L=1" % ident,
                    peer_nick="UE7QN1N9G", member_id=mid)
        if row() != ("1", ident):
            common._say("FAIL: @GameEN= must seat first, got %r" % (row(),)); ok = False
        # A BARE @GameQT= is the dialog-confirm and must NOT touch the seat.
        dispatch.handle_line(b"41000500@GameQT=", peer_nick="UE7QN1N9G", member_id=mid)
        if row() != ("1", ident):
            common._say("FAIL: a bare @GameQT= (confirm) must NOT release the seat, "
                 "got %r" % (row(),)); ok = False
        # The `/ID=` form frees the table AND still answers @GameQA -- both
        # senders wait on (0x41, 6), so a cancel that releases but does not
        # answer would hang the client exactly like the unanswered confirm did.
        wire = tmroom.canonical_table_id(0, 1)
        out = dispatch.handle_line(b"41000500@GameQT=/ID=%016X" % wire,
                          peer_nick="UE7QN1N9G", member_id=mid)
        if not out or not out.startswith(b"41000600") or b"@GameQA=" not in out:
            common._say("FAIL: the /ID= cancel must still answer @GameQA on "
                 "(0x41, 6), got %r" % (out,)); ok = False
        if row() != (str(tmroom.EMPTY_TABLE_STATE), 0):
            common._say("FAIL: @GameQT=/ID= must release the seat -- authored state "
                 "and id 0, got %r" % (row(),)); ok = False
    except Exception as exc:
        common._say("FAIL: cancel selftest raised %r" % (exc,)); ok = False
    finally:
        (tmroom._KEY, tmroom._OWNER[0]) = saved[0], saved[1]
        for d, v in ((tmroom._TABLES, saved[2]), (tmroom._PEERS, saved[3]),
                     (tmroom._RECORDS, saved[4]), (tmroom._ROOMS_SEQ, saved[5]),
                     (seating._SEATED, saved_seated)):
            d.clear(); d.update(v)
        tmroom._SHARED.forget()
    return ok


def _selftest_heartbeat():
    """The reservation heartbeat TTL, red-path first. See `_SEAT_ALIVE`.

    Measured live 2026-08-22: Cancel Reservation sends ZERO bytes -- the
    table-peer @Pong just stops -- so silence-past-TTL is the only server-side
    cancel there is. Assert: a fresh stamp survives the sweep; a silent one is
    freed and the row republishes empty; a running match's table is exempt; an
    ANCIENT match is reclaimed (the post-match leak); TTL 0 disables.

    WARNING: Sandboxed like `_selftest_seating`; the sweep walks the shared seat map.
    """
    import os as _os, tempfile, tmroom
    ok = True
    saved = (tmroom._KEY, tmroom._OWNER[0],
             dict(tmroom._TABLES), dict(tmroom._PEERS),
             dict(tmroom._RECORDS), dict(tmroom._ROOMS_SEQ))
    saved_seated = dict(seating._SEATED)
    saved_alive, saved_began = dict(matchmaking._SEAT_ALIVE), dict(matchmaking._MATCH_BEGAN)
    saved_started = dict(matchmaking._MATCH_STARTED)
    saved_sweep, saved_ttl = matchmaking._SEAT_SWEEP[0], _os.environ.get("POL_TM_SEAT_TTL_S")
    chan = "#TM0R001"
    try:
        tmroom._KEY = "tm:selftest:tm-hb:" + uuid.uuid4().hex
        tmroom._OWNER[0] = True
        for d in (tmroom._TABLES, tmroom._PEERS, tmroom._RECORDS,
                  tmroom._ROOMS_SEQ, seating._SEATED, matchmaking._SEAT_ALIVE, matchmaking._MATCH_BEGAN,
                  matchmaking._MATCH_STARTED):
            d.clear() if isinstance(d, dict) else None
        _os.environ.pop("POL_TM_SEAT_TTL_S", None)
        mid, ident = 0x860FB3E2A2, 0xAB12CD56EB0F5932
        tmroom.note_member(mid, ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                                 "0x0000002000000001", "T"])
        tmroom.note_room_peer(chan, peers.peer_guid("UKXDBA266"))

        # The peer decode that feeds the stamp: our own published table id must
        # round-trip nick -> (chan, index), and a member's nick must NOT.
        tnick = matchmaking.table_peer_nick(tmroom, chan, 1)
        if tnick and matchmaking._peer_table(tnick) != (chan, 1):
            common._say("FAIL: _peer_table(%r) = %r, want (%r, 1)"
                 % (tnick, matchmaking._peer_table(tnick), chan)); ok = False
        if matchmaking._peer_table(b"UGRAWG3GT") != (chan, 1):
            common._say("FAIL: the live-measured table-1 peer must decode, got %r"
                 % (matchmaking._peer_table(b"UGRAWG3GT"),)); ok = False

        def seated():
            return any(m == mid for m, _i in seating._seats_of(chan).get(1, []))

        def seat():
            dispatch.handle_line(b"41000100@GameEN=/NN=%016X/L=1" % ident,
                        peer_nick="UE7QN1N9G", member_id=mid)
            if not seated():
                common._say("FAIL: heartbeat selftest could not seat its member")
                return False
            return True

        def sweep(age=None):
            if age is not None:
                matchmaking._SEAT_ALIVE[matchmaking._alive_key(chan, mid, 1)] = time.time() - age
            matchmaking._SEAT_SWEEP[0] = 0.0
            return matchmaking.expire_silent_seats()

        # Fresh stamp: the sweep must not touch the seat.
        if seat():
            matchmaking._SEAT_ALIVE[matchmaking._alive_key(chan, mid, 1)] = time.time()
            sweep()
            if not seated():
                common._say("FAIL: a FRESH heartbeat's seat was freed"); ok = False
        # WARNING: AND ONE TABLE'S BEAT MUST NOT KEEP ANOTHER TABLE'S SEAT ALIVE.
        # The bug this replaces: `_SEAT_ALIVE` keyed `(chan, member)`, so a
        # player reserved at table 1 who then pongs table 2 (a VS. COM game
        # lives on the table peer for its whole length) re-stamped the table-1
        # reservation on every beat and the TTL could never reap it.
        if seated() or seat():
            matchmaking._SEAT_ALIVE[matchmaking._alive_key(chan, mid, 1)] = time.time() - 120
            matchmaking._SEAT_ALIVE[matchmaking._alive_key(chan, mid, 2)] = time.time()
            matchmaking._SEAT_SWEEP[0] = 0.0
            matchmaking.expire_silent_seats()
            if seated():
                common._say("FAIL: a table-1 seat silent 120s was kept alive by a "
                     "FRESH beat on table 2 -- _SEAT_ALIVE must key by TABLE, "
                     "or a COM game at one table pins a reservation at another")
                ok = False
            matchmaking._SEAT_ALIVE.pop(matchmaking._alive_key(chan, mid, 2), None)
        # Silent past TTL: freed.
        if seated() or seat():
            sweep(age=120)
            if seated():
                common._say("FAIL: a seat silent 120s (TTL 45) must be freed"); ok = False
        # A running match holds the table even in silence.
        if seat():
            matchmaking._MATCH_STARTED[(chan, 1)] = {pushqueue._push_key(mid)}
            matchmaking._MATCH_BEGAN[(chan, 1)] = time.time()
            sweep(age=120)
            if not seated():
                common._say("FAIL: a running match's seat was reaped by the TTL"); ok = False
            # ...but an ancient match is reclaimed (the post-match leak).
            matchmaking._MATCH_BEGAN[(chan, 1)] = time.time() - 100000
            sweep(age=120)
            if seated():
                common._say("FAIL: a match begun 100000s ago must not hold the table")
                ok = False
            matchmaking._MATCH_STARTED.pop((chan, 1), None)
            matchmaking._MATCH_BEGAN.pop((chan, 1), None)
        # TTL 0 disables the whole mechanism.
        if seat():
            _os.environ["POL_TM_SEAT_TTL_S"] = "0"
            if sweep(age=120) != 0 or not seated():
                common._say("FAIL: POL_TM_SEAT_TTL_S=0 must disable the sweep"); ok = False
    except Exception as exc:
        common._say("FAIL: heartbeat selftest raised %r" % (exc,)); ok = False
    finally:
        (tmroom._KEY, tmroom._OWNER[0]) = saved[0], saved[1]
        for d, v in ((tmroom._TABLES, saved[2]), (tmroom._PEERS, saved[3]),
                     (tmroom._RECORDS, saved[4]), (tmroom._ROOMS_SEQ, saved[5]),
                     (seating._SEATED, saved_seated), (matchmaking._SEAT_ALIVE, saved_alive),
                     (matchmaking._MATCH_BEGAN, saved_began), (matchmaking._MATCH_STARTED, saved_started)):
            d.clear(); d.update(v)
        matchmaking._SEAT_SWEEP[0] = saved_sweep
        if saved_ttl is None:
            _os.environ.pop("POL_TM_SEAT_TTL_S", None)
        else:
            _os.environ["POL_TM_SEAT_TTL_S"] = saved_ttl
        tmroom._SHARED.forget()
    return ok


def _selftest_seat_move():
    """Entering a new room must release a seat left behind in the old one.

    The orphan `reconcile_room_tables` cannot see (see
    `release_seats_elsewhere`): a member seated in room A who moves to room B
    keeps their room-A seat, because the sweep frees only seats of members with
    NO session at all. Reserve in R001, walk into R002, assert R001 is freed --
    and that the seat in R002 (where they now are) would be untouched.
    """
    import os as _os, tempfile, tmroom
    ok = True
    saved = (tmroom._KEY, tmroom._OWNER[0], dict(tmroom._TABLES),
             dict(tmroom._PEERS), dict(tmroom._RECORDS), dict(tmroom._ROOMS_SEQ),
             dict(tmroom._SEATS))
    saved_seated = dict(seating._SEATED)
    try:
        tmroom._KEY = "tm:selftest:tm-move:" + uuid.uuid4().hex
        tmroom._OWNER[0] = True
        for d in (tmroom._TABLES, tmroom._PEERS, tmroom._RECORDS,
                  tmroom._ROOMS_SEQ, tmroom._SEATS):
            d.clear()
        seating._SEATED.clear()
        mid, ident = 0x860FB3E2A2, 0xAB12CD56EB0F5932

        def seated_in(chan):
            return [m for rows in seating._seats_of(chan).values() for m, _i in rows
                    if m == mid]

        tmroom.note_member(mid, ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                                 "0x0000002000000001", "T"])
        tmroom.note_room_peer("#TM0R001", peers.peer_guid("UKXDBA266"))
        dispatch.handle_line(b"41000100@GameEN=/NN=%016X/L=1" % ident,
                    peer_nick="UE7QN1N9G", member_id=mid)
        if not seated_in("#TM0R001"):
            common._say("FAIL: @GameEN= did not seat the player in R001"); ok = False
        # The member walks into R002: the record moves first (as the <DE> handler
        # does), then the entry sweep runs. R001's seat must go; a hypothetical
        # R002 seat must be spared.
        tmroom.note_member(mid, ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                                 "0x0000002000000002", "T"])
        tableaudit.release_seats_elsewhere(mid, "#TM0R002")
        if seated_in("#TM0R001"):
            common._say("FAIL: a move to R002 left the R001 seat orphaned -- the "
                 "recurring phantom this fixes"); ok = False
    except Exception as exc:
        common._say("FAIL: seat-move selftest raised %r" % (exc,)); ok = False
    finally:
        (tmroom._KEY, tmroom._OWNER[0]) = saved[0], saved[1]
        for d, v in ((tmroom._TABLES, saved[2]), (tmroom._PEERS, saved[3]),
                     (tmroom._RECORDS, saved[4]), (tmroom._ROOMS_SEQ, saved[5]),
                     (tmroom._SEATS, saved[6]), (seating._SEATED, saved_seated)):
            d.clear(); d.update(v)
        tmroom._SHARED.forget()
    return ok


def _selftest_audit():
    """The invariant checker must pass the REAL seating path and catch planted
    defects.

    Two directions, both load-bearing:
      * a table seated through the real `@GameEN=` handler must audit CLEAN --
        if the production path itself trips the auditor, one of them is wrong
        and that disagreement is exactly what to investigate;
      * a planted phantom row (state 1, count 1, occupancy id, NO seats -- the
        tester's literal bug) must produce the four kinds that describe it.
        A checker that cannot go red is the dead-assertion class the ledger
        already records twice.
    """
    import os as _os, tempfile, tmroom
    ok = True
    saved = (tmroom._KEY, tmroom._OWNER[0],
             dict(tmroom._TABLES), dict(tmroom._PEERS),
             dict(tmroom._RECORDS), dict(tmroom._ROOMS_SEQ),
             json.loads(json.dumps(tmroom._SEATS)),
             json.loads(json.dumps(tmroom._CONFIRMED)))
    saved_seated = dict(seating._SEATED)
    try:
        tmroom._KEY = "tm:selftest:tm-audit:" + uuid.uuid4().hex
        tmroom._OWNER[0] = True
        tmroom._TABLES.clear(); tmroom._PEERS.clear()
        tmroom._RECORDS.clear(); tmroom._ROOMS_SEQ.clear()
        tmroom._SEATS.clear(); tmroom._CONFIRMED.clear(); seating._SEATED.clear()
        chan = "#TM0R001"
        mid, ident = 0x860FB3E2A2, 0xAB12CD56EB0F5932
        tmroom.note_member(mid, ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                                 "0x0000002000000001", "T"])
        tmroom.note_room_peer(chan, peers.peer_guid("UKXDBA266"))
        dispatch.handle_line(b"41000100@GameEN=/NN=%016X/L=1" % ident,
                    peer_nick="UE7QN1N9G", member_id=mid)
        bad = [v for v in tableaudit.audit_room_tables(chan, seating._seats_of(chan))
               if v["severity"] == "bad"]
        if bad:
            common._say("FAIL: the real seating path must audit CLEAN, got %r" % bad)
            ok = False
        # THE PLANTED PHANTOM -- the live report: a row claiming
        # state 1 / 1 seated / an occupancy id, with no seat behind it.
        blk = bytearray(b"A" * 49)
        blk[4:6] = tmroom.letters(1, 2)
        blk[14:30] = tmroom.letters(0xDEAD, 16)
        room = "0x%016X" % tmroom.room_id_for(chan)
        tmroom._TABLES.setdefault(room, {})["#TM0T002"] = [
            "#TM0T002", "0", "0x0021000100063063", "1", "8", "1",
            blk.decode("latin1")]
        kinds = {v["kind"] for v in tableaudit.audit_room_tables(chan, seating._seats_of(chan))
                 if v.get("table") == 2 and v["severity"] == "bad"}
        want = {"row-occupied-nobody", "seat-count-drift",
                "block-count-drift", "ghost-occupancy-id"}
        if not want <= kinds:
            common._say("FAIL: the planted phantom must trip %r, tripped only %r"
                 % (sorted(want), sorted(kinds)))
            ok = False
    except Exception as exc:
        common._say("FAIL: audit selftest raised %r" % (exc,)); ok = False
    finally:
        (tmroom._KEY, tmroom._OWNER[0]) = saved[0], saved[1]
        for d, v in ((tmroom._TABLES, saved[2]), (tmroom._PEERS, saved[3]),
                     (tmroom._RECORDS, saved[4]), (tmroom._ROOMS_SEQ, saved[5]),
                     (tmroom._SEATS, saved[6]), (tmroom._CONFIRMED, saved[7]),
                     (seating._SEATED, saved_seated)):
            d.clear(); d.update(v)
        tmroom._SHARED.forget()
    return ok


def _selftest_reconcile():
    """`reconcile_room_tables` heals what the audit flags -- the FIRST test
    this function has ever had (the coverage map found
    zero). Three legs: a planted phantom row is republished clean; a member
    whose record MOVED rooms loses the seat left behind (the new `_absent`
    branch); and the audit reads CLEAN afterwards -- checker and healer
    agreeing is the whole design.
    """
    import os as _os, tempfile, tmroom
    ok = True
    saved = (tmroom._KEY, tmroom._OWNER[0],
             dict(tmroom._TABLES), dict(tmroom._PEERS),
             dict(tmroom._RECORDS), dict(tmroom._ROOMS_SEQ),
             json.loads(json.dumps(tmroom._SEATS)),
             json.loads(json.dumps(tmroom._CONFIRMED)))
    saved_seated = dict(seating._SEATED)
    try:
        tmroom._KEY = "tm:selftest:tm-recon:" + uuid.uuid4().hex
        tmroom._OWNER[0] = True
        tmroom._TABLES.clear(); tmroom._PEERS.clear()
        tmroom._RECORDS.clear(); tmroom._ROOMS_SEQ.clear()
        tmroom._SEATS.clear(); tmroom._CONFIRMED.clear(); seating._SEATED.clear()
        chan = "#TM0R001"
        mid, ident = 0x860FB3E2A2, 0xAB12CD56EB0F5932
        tmroom.note_member(mid, ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                                 "0x0000002000000001", "T"])
        tmroom.note_room_peer(chan, peers.peer_guid("UKXDBA266"))
        dispatch.handle_line(b"41000100@GameEN=/NN=%016X/L=1" % ident,
                    peer_nick="UE7QN1N9G", member_id=mid)
        # Leg 1: the planted phantom (state 1 / count 1 / occ id, no seats).
        blk = bytearray(b"A" * 49)
        blk[4:6] = tmroom.letters(1, 2)
        blk[14:30] = tmroom.letters(0xDEAD, 16)
        room = "0x%016X" % tmroom.room_id_for(chan)
        tmroom._TABLES.setdefault(room, {})["#TM0T002"] = [
            "#TM0T002", "0", "0x0021000100063063", "1", "8", "1",
            blk.decode("latin1")]
        if tableaudit.reconcile_room_tables(chan) < 1:
            common._say("FAIL: reconcile must republish the planted phantom")
            ok = False
        row2 = dict(tmroom.tables(chan)).get("#TM0T002")
        if row2 and (row2[3] != str(tmroom.EMPTY_TABLE_STATE)
                     or tmroom._as_int(row2[5]) != 0
                     or tmroom.unletters(row2[6][14:30]) != 0):
            common._say("FAIL: the phantom row must heal to authored state/0/0, "
                 "got state=%s seated=%s occ=%X"
                 % (row2[3], row2[5], tmroom.unletters(row2[6][14:30])))
            ok = False
        # Leg 2: the record moves to R002 -> the R001 seat is absent now.
        tmroom.note_member(mid, ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                                 "0x0000002000000002", "T"])
        tableaudit.reconcile_room_tables(chan)
        if any(m == mid for s in seating._seats_of(chan).values() for m, _i in s):
            common._say("FAIL: a record that moved rooms must free the seat it "
                 "left behind")
            ok = False
        # Leg 3: checker and healer agree -- the room audits CLEAN.
        bad = [v for v in tableaudit.audit_room_tables(chan, seating._seats_of(chan))
               if v["severity"] == "bad"]
        if bad:
            common._say("FAIL: after reconcile the audit must be clean, got %r" % bad)
            ok = False
        # Leg 4: the seated letters revert on release -- prod 2026-08-21
        # 06:06:10Z: release_seats re-marked BEFORE saving the seat list, so
        # block[8] stayed 'D' on the record after every release.
        v10 = "T              LAA0AAABABAABIAB"
        tmroom.note_member(mid, ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                                 "0x0000002000000001", v10])
        dispatch.handle_line(b"41000100@GameEN=/NN=%016X/L=1" % ident,
                    peer_nick="UE7QN1N9G", member_id=mid)
        rec = tmroom._RECORDS[str(mid)][10]
        if rec[-16:][8] != "D":
            common._say("FAIL: seating must mark block[8]='D', record reads %r"
                 % rec[-16:])
            ok = False
        tableaudit.release_seats(mid, chan)
        rec = tmroom._RECORDS[str(mid)][10]
        if rec[-16:][8] != "A":
            common._say("FAIL: release must revert block[8] to 'A' -- the "
                 "remark-before-save ordering bug (record reads %r)"
                 % rec[-16:])
            ok = False
        # Leg 4b: an ownership change clears the departed owner's confirm --
        # the heir must NOT inherit a state-2 "Playing" table (measured live
        # 2026-08-21: Observe-only menus on every client after the owner left).
        mid2 = mid + 1
        tmroom.note_member(mid2, ["0", "0", "0", "0", "T2", "2", "0", "0",
                                  "0", "0x0000002000000001", "T2"])
        seating._seats_of(chan)[1] = [(mid, ident), (mid2, ident + 1)]
        seating._save_seats(chan)
        seating._TABLE_CONFIRMED.add((chan, 1))
        tmroom.note_table_confirmed(chan, 1)
        tableaudit.release_seats(mid, chan)          # the owner (seat 0) departs
        if (chan, 1) in seating._TABLE_CONFIRMED or tmroom.table_confirmed(chan, 1):
            common._say("FAIL: the departed owner's confirm must not survive the "
                 "handoff")
            ok = False
        row1 = dict(tmroom.tables(chan)).get("#TM0T001")
        if row1 and row1[3] == "2":
            common._say("FAIL: the heir's table must drop out of state 2 -- they "
                 "have not confirmed anything")
            ok = False
        # Leg 5: a stored row carrying the PRE-FOLD (room-shared) id heals to
        # the room-folded id -- the yellow-tile-in-every-room carrier,
        # measured live 2026-08-21 (my-reservation == every room's table 1).
        shared = tmroom.canonical_table_id(2, 0)          # table 3, pre-fold
        tmroom._TABLES.setdefault(room, {})["#TM0T003"] = [
            "#TM0T003", "0", "0x%016X" % shared, "7", "8", "0",
            ("A" * 49)]
        kinds = {v["kind"] for v in tableaudit.audit_room_tables(chan, seating._seats_of(chan))
                 if v.get("table") == 3 and v["severity"] == "bad"}
        if "room-shared-id" not in kinds:
            common._say("FAIL: the pre-fold id must be flagged room-shared-id, "
                 "got %r" % sorted(kinds))
            ok = False
        tableaudit.reconcile_room_tables(chan)
        row3 = dict(tmroom.tables(chan)).get("#TM0T003")
        want = tmroom.canonical_table_id(2, tmroom._room_no(chan))
        if not row3 or tmroom._as_int(row3[2]) != want:
            common._say("FAIL: reconcile must fold the room into the id -- want "
                 "0x%016X, row holds %r" % (want, row3 and row3[2]))
            ok = False
    except Exception as exc:
        common._say("FAIL: reconcile selftest raised %r" % (exc,)); ok = False
    finally:
        (tmroom._KEY, tmroom._OWNER[0]) = saved[0], saved[1]
        for d, v in ((tmroom._TABLES, saved[2]), (tmroom._PEERS, saved[3]),
                     (tmroom._RECORDS, saved[4]), (tmroom._ROOMS_SEQ, saved[5]),
                     (tmroom._SEATS, saved[6]), (tmroom._CONFIRMED, saved[7]),
                     (seating._SEATED, saved_seated)):
            d.clear(); d.update(v)
        tmroom._SHARED.forget()
    return ok


def _selftest_gameml_format():
    """`@GameML` must be COLUMNAR, pipe-delimited, with HEX-encoded names.

    The client's row extractors (0xAAD60 string / 0xAB470 u64 / 0xAB080 int) are
    columnar: each key appears ONCE and its value is a `|`-list indexed per
    member; names are read two-hex-chars-per-byte, so plaintext "Lex" decodes to
    the single byte 0xEA -- the empty-pane sentinel the dump showed. RE + minidump
    2026-08-21.
    """
    import tmroom
    ok = True
    saved = {k: dict(getattr(tmroom, k)) for k in
             ("_RECORDS", "_POLIDS", "_ROOMS_SEQ", "_GUIDS", "_NAMES",
              "_DELTAS", "_SEATS")}
    saved_owner = tmroom._OWNER[0]
    try:
        for k in saved:
            getattr(tmroom, k).clear()
        tmroom._OWNER[0] = True
        m1, m2 = 111, 222
        # index 9 = room, index 10 = 15-wide name + 16-letter status block.
        tmroom.note_member(m1, ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                                "0x0000002000000001",
                                "Lex            DAA0AABFDBBADIAB"])
        tmroom.note_member(m2, ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                                "0x0000002000000001",
                                "LaptopTest2    LAA0AAALDBBACIAB"])
        tmroom.note_pol_id(m1, "AB12CD31DC3F63B6")
        tmroom.note_pol_id(m2, "AB12CD56EB0F5932")
        # TABLE-SCOPED (the 13:38Z pseudo-add): only SEATED members may appear.
        # Both are seated at table 1 here; the room-roster shortcut is the bug.
        tmroom._SEATS["0x%016X" % tmroom.room_id_for("#TM0R001")] = {
            "1": [[m1, 1], [m2, 2]]}
        body = reservation._gameml_body(b"@GameM=/ID=AB12CD31DC3F63B6", member_id=m1)
        if not body:
            common._say("FAIL: @GameML produced nothing"); return False
        # ...and an asker who is NOT seated must not conjure themselves in.
        m3_unseated = 333
        tmroom.note_member(m3_unseated,
                           ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                            "0x0000002000000001",
                            "Bystander      LAA0AAALDBBACIAB"])
        tmroom.note_pol_id(m3_unseated, "AB12CE9A544769E8")
        body3 = reservation._gameml_body(b"@GameM=/ID=AB12CE9A544769E8",
                             member_id=m3_unseated)
        if body3 is None or b"/Num=0" not in body3 or b"/MLID=/" not in body3:
            common._say("FAIL: an unseated room-joiner must get /Num=0 and must NOT "
                 "appear in the table list (the 13:38Z pseudo-add), got %r"
                 % body3)
            ok = False
        # THE RESERVATION-SLOT COPY rides msgid 0xC, same body, distinct header.
        # code 0x41 msgid 4 -> "41000400"; msgid 0xC -> "41000C00". The gate's
        # +0x108 only fills from the msgid-0xC channel (parser 0x83B80).
        if protocol.encode_code(protocol.MSG_GAMEML) != b"41000400":
            common._say("FAIL: MSG_GAMEML header changed: %r" % protocol.encode_code(protocol.MSG_GAMEML))
            ok = False
        if protocol.encode_code(protocol.MSG_GAMEML_RESV) != b"41000C00":
            common._say("FAIL: the reservation reply must be code 0x41 msgid 0xC "
                 "(41000C00), got %r" % protocol.encode_code(protocol.MSG_GAMEML_RESV)); ok = False
        # COLUMNAR: exactly one of each key (a grouped/repeated key is the bug).
        for key in (b"/MLID=", b"/MLNN=", b"/Po=", b"/Lv=", b"/Rk=", b"/HN="):
            if body.count(key) != 1:
                common._say("FAIL: @GameML must carry ONE %r (columnar), got %d in %r"
                     % (key, body.count(key), body)); ok = False
        if b"/Num=2" not in body:
            common._say("FAIL: @GameML /Num=2 missing: %r" % body); ok = False
        # PIPE-joined hex columns, member order.
        if b"/MLID=AB12CD31DC3F63B6|AB12CD56EB0F5932" not in body:
            common._say("FAIL: /MLID= must be pipe-joined hex POL-IDs: %r" % body); ok = False
        # hex of the two fixture names: 4C6578 and 4C6170746F705465737432.
        if b"/MLNN=4C6578|4C6170746F705465737432" not in body:
            common._say("FAIL: /MLNN= must be pipe-joined UPPERCASE hex names: %r"
                 % body); ok = False
        if b"/HN=4C6578|4C6170746F705465737432" not in body:
            common._say("FAIL: /HN= must match /MLNN= hex encoding: %r" % body); ok = False
        # NO plaintext name may leak -- 0xAAD60 would hex-decode it to garbage.
        if b"Lex" in body or b"LaptopTest2" in body:
            common._say("FAIL: @GameML leaked a plaintext name (would decode to 0xEA): "
                 "%r" % body); ok = False
        # values must never contain a raw delimiter.
        for col in body.split(b"/")[1:]:
            v = col.split(b"=", 1)[1] if b"=" in col else b""
            if b"@" in v:
                common._say("FAIL: @GameML value carries a stray '@': %r" % body); ok = False
    except Exception as exc:
        common._say("FAIL: gameml-format selftest raised %r" % (exc,)); ok = False
    finally:
        for k, v in saved.items():
            getattr(tmroom, k).clear(); getattr(tmroom, k).update(v)
        tmroom._OWNER[0] = saved_owner
        tmroom._SHARED.forget()
    return ok


def _selftest_peer_index():
    """Table indexing MUST survive an authsess restart.

    The peer-block base drifts by 0x1000 on every client re-allocation, and a
    restart forces one (the client resumes on a new allocation while the server
    keeps the old room peer). The computed table index must not depend on WHICH
    drift era it is. This resolves one table peer against a room peer taken from
    k=1, 2 and 3 blocks up and asserts the SAME index every time -- the property
    that was broken 2026-08-21 (room 3 table 2 dropped as "out of range").
    """
    ok = True
    nick = "UKXDBA266"
    guid = peers.peer_guid(nick)
    if guid is None:
        common._say("FAIL: peer-index selftest could not fold the test nick"); return False
    tlow = guid & 0xFFFFFFFF
    room_cls = (peers.peer_class(guid) - 1) & 0xFF   # so the class gate sees room+1
    for room_no in (1, 3):
        for idx in (1, 2, 4):
            got = {}
            for k in (1, 2, 3):                # three allocation eras / restarts
                room1_low = (tlow - (idx - 1) + k * 0x1000) & 0xFFFFFFFF
                room_low = (room1_low + (room_no - 1)) & 0xFFFFFFFF
                room_peer = (room_cls << 32) | room_low
                res, _why = peers.table_index_for_peer(
                    nick, room_peer, chan=("#TM0R%03d" % room_no).encode())
                got[k] = res
            if set(got.values()) != {idx}:
                common._say("FAIL: table index not restart-stable for room %d table %d "
                     "-- got %r across drift eras k=1,2,3 (all must be %d)"
                     % (room_no, idx, got, idx)); ok = False
    return ok


def _selftest_value0():
    """The roster key id: snapshot AND delta stream serve the POL-ID
    (POL_TM_VALUE0=polid, the Start-Game-gate/members-pane unlock), and a
    <PC> removal still rewrites after the record is popped."""
    import os as _os, tempfile, tmroom
    ok = True
    saved = (tmroom._KEY, tmroom._OWNER[0], dict(tmroom._RECORDS),
             dict(tmroom._ROOMS_SEQ), dict(tmroom._GUIDS),
             dict(tmroom._POLIDS),
             json.loads(json.dumps(tmroom._DELTAS)))
    try:
        tmroom._KEY = "tm:selftest:tm-v0:" + uuid.uuid4().hex
        tmroom._OWNER[0] = True
        tmroom._RECORDS.clear(); tmroom._ROOMS_SEQ.clear()
        tmroom._GUIDS.clear(); tmroom._POLIDS.clear(); tmroom._DELTAS.clear()
        chan, mid, guid = "#TM0R001", 6, 0x860FB3E2A2
        polid = "AB12CD56EB0F5932"
        tmroom.note_guid(mid, guid)
        tmroom.note_pol_id(mid, polid)
        tmroom.note_member(mid, ["0x%016X" % guid, "0", "0", "0", "T", "2",
                                 "0", "0", "0", "0x0000002000000001",
                                 "T              LAA0AAABABAABIAB"])
        blob = tmroom.build_ptl(chan, b"")
        row0 = tmroom.decode_member(
            blob[tmroom.MEMBER_OFF:tmroom.MEMBER_OFF + tmroom.MEMBER_REC])
        if tmroom._as_int(row0[0]) != int(polid, 16):
            common._say("FAIL: the snapshot must key the row by the POL-ID, got %r"
                 % row0[0])
            ok = False
        tmroom.forget_member(mid)
        deltas = tmroom.rewrite_value0_deltas(tmroom.deltas_after(chan, 0))
        pc = [d for d in deltas if d[1] == "PC"]
        if not pc or tmroom._as_int(pc[-1][2][0]) != int(polid, 16):
            common._say("FAIL: a <PC> after the record is popped must still carry "
                 "the POL-ID (client removes by id), got %r"
                 % (pc and pc[-1][2]))
            ok = False
        pd = [d for d in deltas if d[1] in ("PD", "DE")]
        if pd and tmroom._as_int(pd[-1][2][0]) != int(polid, 16):
            common._say("FAIL: the delta stream must agree with the snapshot, got %r"
                 % pd[-1][2][0])
            ok = False
    except Exception as exc:
        common._say("FAIL: value0 selftest raised %r" % (exc,)); ok = False
    finally:
        (tmroom._KEY, tmroom._OWNER[0]) = saved[0], saved[1]
        for d, v in ((tmroom._RECORDS, saved[2]), (tmroom._ROOMS_SEQ, saved[3]),
                     (tmroom._GUIDS, saved[4]), (tmroom._POLIDS, saved[5]),
                     (tmroom._DELTAS, saved[6])):
            d.clear(); d.update(v)
        tmroom._SHARED.forget()
    return ok


def _selftest_peer_class():
    """The table-class gate must accept every era measured live and refuse the
    room family -- each constant here cost a night of dropped reservations.
    The rule is: class LOW NIBBLE == room number (era-invariant high nibble)."""
    ok = True
    for cls, room_cls, room_no, want, name in (
            (0xD0, None, None, True, "legacy absolute"),
            (0xF1, 0xF0, 1, True, "2026-08-20 room1 era F"),
            (0xE1, 0xF0, 1, True, "room1 one drift step"),
            (0xD1, 0xF0, 1, True, "2026-08-21T12:33 room1 two steps"),
            (0xD3, 0xF0, 3, True, "2026-08-21T16:24 ROOM 3 live reject (0xD3)"),
            (0xF3, None, 3, True, "room 3 era F"),
            (0xD1, None, None, True, "drifted, no room known: family-bit x1"),
            (0xF0, 0xF0, 1, False, "a ROOM (low 0) is not a table"),
            (0xD1, 0xF0, 3, False, "room-1 class in a room-3 lookup"),
            (0xA2, 0xF0, 2, False, "foreign high nibble"),
            (None, 0xF0, 1, False, "no class")):
        if peers._is_table_class(cls, room_cls, room_no) != want:
            common._say("FAIL: _is_table_class(%r, %r, room=%r) must be %s (%s)"
                 % (cls, room_cls, room_no, want, name))
            ok = False
    return ok


def _pipe_vals(body, key):
    """The `|`-separated values under one key -- the shape 0xAB080 indexes.

    `key` includes its `/` and `=`. The value ends at the next `/` or `@`,
    which is the find-first-of set at 0x51B3950.
    """
    i = body.find(key)
    if i < 0:
        return []
    v = body[i + len(key):]
    end = len(v)
    for ch in (b"/", b"@"):
        j = v.find(ch)
        if 0 <= j < end:
            end = j
    return v[:end].split(b"|")


def _selftest_chat():
    """The `@Chat=` roster: the two `/Num=` numbers, the shop-byte gate, and the
    ids going back exactly as they came in.

    Asserted against the PARSER -- nothing here has been on a wire yet -- so
    every check names the instruction that would reject the message.

    WARNING: Sandboxed like `_selftest_ingame`: this writes members and POL-IDs into
    `tmroom`, and a test that can reach the snapshot two containers share has
    already cost this project one incident.
    """
    import os as _os, tempfile, tmroom
    ok = True
    saved = (tmroom._KEY, tmroom._OWNER[0])
    saved_maps = [(d, dict(d)) for d in (tmroom._RECORDS, tmroom._ROOMS_SEQ,
                                         tmroom._POLIDS, tmroom._NAMES,
                                         tmroom._GUIDS, roomchat._CHAT_SERIAL)]
    saved_deltas = dict(tmroom._DELTAS)
    # THE DEFAULT IS OFF AND MUST STAY OFF until arm A's gate is proven -- see
    # the banner on `_chat_roster_enabled`. Assert that here, so flipping it back
    # trips a test rather than a live client's chat indicator, then force it on
    # for this run: the BODY still has to be correct whenever it is enabled.
    _chat_env = _os.environ.pop("POL_TM_CHAT_ROSTER", None)
    if roomchat._chat_roster_enabled():
        common._say("FAIL: POL_TM_CHAT_ROSTER must default OFF -- an un-erased msgid-4 "
             "parks in the store and blocks every later code-0x42 message, which "
             "is a measured, live-confirmed loss of the chat indicator")
        ok = False
    _os.environ["POL_TM_CHAT_ROSTER"] = "1"
    # THE LIVE TOGGLE. Point it at a path of our own first: a real
    # /data/tm_chat_roster.txt on the box running the test would otherwise decide
    # these assertions, and a test that reads production state is not a test.
    _chat_file_keep = roomchat._CHAT_CONTROL_FILE
    roomchat._CHAT_CONTROL_FILE = _os.path.join(tempfile.mkdtemp(prefix="tm-toggle-"),
                                       "tm_chat_roster.txt")
    try:
        tmroom._KEY = "tm:selftest:tm-chat:" + uuid.uuid4().hex
        tmroom._OWNER[0] = True
        for d, _ in saved_maps:
            d.clear()
        tmroom._DELTAS.clear()

        chan = "#TM0R001"
        # Two players in one room. `A` has told us its POL-ID; `C` has not, and
        # must therefore be LEFT OUT rather than given a made-up one.
        for mid, name in ((41, "Lex"), (42, "Framework"), (43, "Quiet")):
            tmroom.note_member(mid, tmroom.synth_member(chan, mid, name), chan)
        tmroom.note_pol_id(41, b"AB12CD56EB0F5932")
        tmroom.note_pol_id(42, b"AB12CEb20d9067c4")           # case is normalised

        # --- what the client actually sends, off the 2026-08-20 capture -------
        req = (protocol.encode_code(roomchat.MSG_CHAT | (roomchat.CHAT_MSGID_WHO << 16))
               + b"@Chat=/NN=0000000000000000/CN=/HID=0/Dm=2/Vol=0")
        reply = dispatch._handle_line(req, member_id=41)
        common._say("chat roster reply:", reply)
        if not reply:
            common._say("FAIL: the msgid-1 request must be answered"); return False

        head, body = reply[:8], reply[8:]
        # code 0x42, msgid 4 -- the arm at 0x07E42F. `0x07DFD0(0x42, 4)` is what
        # the screen pump polls for; any other msgid is never looked at.
        if head != b"42000400":
            common._say("FAIL: header %r -- the roster is code 0x42 msgid 4" % head)
            ok = False
        rcode, rcmd = protocol.decode_ebody(reply)
        if (rcode & 0xFF) != roomchat.MSG_CHAT or ((rcode >> 16) & 0xFF) != roomchat.CHAT_MSGID_ROSTER:
            common._say("FAIL: decoded header %08X" % rcode); ok = False
        # The `=` straight after the name: 0xAA920 is a lookup in a map keyed on
        # `name=`, so `@Chat` without it becomes the key `@Chat/Num` and every
        # field read misses. Same byte, same lesson as @TeachDVAns.
        if not rcmd.startswith(b"@Chat="):
            common._say("FAIL: the PC key delimiter `=` is missing after @Chat"); ok = False

        # --- `/Num=` is TWO numbers under one key ---------------------------
        nums = _pipe_vals(body, b"/Num=")
        if nums != [b"0", b"2"]:
            common._say("FAIL: /Num= must be <last part>|<entries>, got %r" % (nums,))
            ok = False
        # 0x07E4B0: `cmp a9780(6), /Num=[0]` and `jne` bails without touching the
        # list. The shop byte IS the part index, so a one-message roster needs
        # both to be 0 -- this is the gate the whole reply hangs on.
        if (rcode >> 24) & 0xFF != int(nums[0] or b"0"):
            common._say("FAIL: the header's shop byte (%d) must equal /Num= occurrence "
                 "0 (%s) or 0x07E4B0 drops the message"
                 % ((rcode >> 24) & 0xFF, nums[0])); ok = False

        # --- the rows, in the same order under both keys ---------------------
        ids, names = _pipe_vals(body, b"/NN="), _pipe_vals(body, b"/CN=")
        if ids != [b"AB12CD56EB0F5932", b"AB12CEB20D9067C4"]:
            common._say("FAIL: /NN= must echo @Init=/NN= verbatim (upper-cased), got %r"
                 % (ids,)); ok = False
        if names != [b"Lex", b"Framework"]:
            common._say("FAIL: /CN= must be the names in the same order, got %r"
                 % (names,)); ok = False
        if len(ids) != int(nums[1] or b"0") or len(names) != len(ids):
            common._say("FAIL: the entry loop at 0x07E6A1 runs /Num=[1] times and reads "
                 "one of each key per pass -- the three counts must agree")
            ok = False
        if b"|" in b"".join(names):
            common._say("FAIL: a `|` inside a name would split one row into two"); ok = False
        # Member 43 has no POL-ID. A row for it would be a row its owner does not
        # recognise as itself (0x0AFE3) and nobody else can resolve.
        if b"Quiet" in body:
            common._say("FAIL: a member with no POL-ID must be omitted, not invented")
            ok = False

        # --- `/C=` identifies the snapshot and only ever rises ---------------
        first = _pipe_vals(body, b"/C=")
        again = _pipe_vals(dispatch._handle_line(req, member_id=41)[8:], b"/C=")
        if not first or not again or int(again[0]) <= int(first[0]):
            common._say("FAIL: /C= must rise per snapshot (0x07E5C4 erases a lesser one "
                 "unread), got %r then %r" % (first, again)); ok = False

        # --- the other two arms are not ours to answer ----------------------
        line = (protocol.encode_code(roomchat.MSG_CHAT | (roomchat.CHAT_MSGID_LINE << 16))
                + b"@Chat=/La=1/Dt=#CHAT#\thello")
        if dispatch._handle_line(line, member_id=41) is not None:
            common._say("FAIL: msgid 8 is a chat LINE and is relayed, not answered here")
            ok = False

        # --- and the store refuses an id of the wrong kind -------------------
        if tmroom.note_pol_id(44, b"0000000000000000"):
            common._say("FAIL: all-zeros is the client's `I do not know`, not an id")
            ok = False
        if tmroom.note_pol_id(44, b"not-hex"):
            common._say("FAIL: a non-hex POL-ID must be refused"); ok = False
        if tmroom.pol_id_of(44) != "":
            common._say("FAIL: an unknown member's POL-ID must be empty, not a guess")
            ok = False
        # ...and it takes the OTHER source's wire form too. Class-L `<PC>` comes
        # through polpro as `(0xAB12CD56EB0F5932)`, parentheses and all, and
        # `responders._roster_note` hands that value straight over.
        import polpro as _polpro
        _pc = _polpro.parse(b"<PC>(0xAB12CD56EB0F5932)")[0][1][0]
        if not tmroom.note_pol_id(45, _pc) or                 tmroom.pol_id_of(45) != "AB12CD56EB0F5932":
            common._say("FAIL: <PC>'s %r must normalise to the same 16 hex digits, "
                 "got %r" % (_pc, tmroom.pol_id_of(45))); ok = False

        # --- the control file, which is what aborts a live test -------------
        # WARNING: ABSENT MUST MEAN "NO OPINION", NEVER "ON". This file is the abort
        # handle for a reply carrying a measured hazard; if a missing or
        # unreadable file could enable it, the abort would be the thing that
        # armed it.
        if roomchat._chat_control_says() is not None:
            common._say("FAIL: a missing control file must have no opinion, got %r"
                 % (roomchat._chat_control_says(),)); ok = False
        _os.environ["POL_TM_CHAT_ROSTER"] = "0"
        for text, want, why in (("1", True, "1 enables"),
                                ("0", False, "0 disables"),
                                ("off", False, "a word disables"),
                                ("", None, "empty = no opinion"),
                                (" \n ", None, "whitespace = no opinion")):
            with open(roomchat._CHAT_CONTROL_FILE, "w") as _f:
                _f.write(text)
            if roomchat._chat_control_says() is not want:
                common._say("FAIL: control file %r -> %r, expected %r (%s)"
                     % (text, roomchat._chat_control_says(), want, why)); ok = False
            # and the file must actually GOVERN, not just parse
            if want is not None and roomchat._chat_roster_enabled() is not want:
                common._say("FAIL: control file %r did not override the env (%s)"
                     % (text, why)); ok = False
        _os.unlink(roomchat._CHAT_CONTROL_FILE)
        if roomchat._chat_roster_enabled():
            common._say("FAIL: with the file gone, POL_TM_CHAT_ROSTER=0 must govern")
            ok = False
        _os.environ["POL_TM_CHAT_ROSTER"] = "1"    # restore for any later check
    finally:
        tmroom._KEY, tmroom._OWNER[0] = saved
        for d, was in saved_maps:
            d.clear()
            d.update(was)
        tmroom._DELTAS.clear()
        tmroom._DELTAS.update(saved_deltas)
        roomchat._CHAT_CONTROL_FILE = _chat_file_keep
        if _chat_env is None:
            _os.environ.pop("POL_TM_CHAT_ROSTER", None)
        else:
            _os.environ["POL_TM_CHAT_ROSTER"] = _chat_env
    return ok
