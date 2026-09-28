"""Who sits where: the seat lists per room, seating and unseating a member, and the settings block
fit.
"""
import os
import re
from . import common, matchmaking, peers, reservation, tablerow, vscom


#: Tables whose owner has pressed Confirm -- `{(chan, index)}`.
#:
#: WARNING: A MATCH WAS BEING ANNOUNCED WHILE THE OWNER WAS STILL IN THE RULES DIALOG.
#: Reported live, 2026-08-20: "Player 2 was able to join before I finished setting the
#: table rules, and immediately started loading into the game while I was still
#: setting the rules... now the host is just sitting in the lobby with the table
#: at setting up while player 2 is at the black screen waiting to start."
#: The wire shows exactly that, and the host's refusal is the client's, not a
#: choice:
#:
#:     12:43:04  m6 -> @GameEA=/EN=2                 guest joins, 2 seated
#:     12:43:08  m6 -> @GameStart=                   wake-up to the guest
#:     12:43:12  m3 <- @Save=/Tbl=0/Game=1@Tet=…     host STILL configuring
#:     12:43:18  m3 -> @GameStart=                   wake-up to the host, mid-dialog
#:     12:43:23  m6 <- @GameOK=                      guest accepts -> black screen
#:     12:43:26  m3 <- @GameNG=                      HOST DECLINES
#:     12:43:30  m3 <- (code 20) @Tet=/@Tab=         …and only NOW confirms
#:
#: `_announce_match` fired on SEAT COUNT alone. `@GameEN=` is sent when the host
#: STARTS reserving, before the dialog, so two seats can be reached while the
#: owner has chosen nothing -- and a wake-up delivered into an open dialog is
#: answered `@GameNG=`, which strands the guest on a game nobody will start.
#:
#: WARNING: 0x24 (`@Save=`) is NOT confirmation. It is the preset write and the client
#: sends it repeatedly while the dialog is open -- twice in the trace above.
#: Only code 0x14, the bare pair sent on Confirm, means the owner is done.
_TABLE_CONFIRMED = set()

#: Who is sitting where: `{room: {table index: [member_id, ...]}}`. In this
#: process only -- it is derived state, rebuilt by the next reservation, and the
#: thing that MUST be shared (the published row) already goes through `tmroom`.
_SEATED = {}

#: Rooms whose seat list we have already read back off disk this process.
_SEATED_ADOPTED = set()


def _seats_of(chan):
    """`_SEATED[chan]`, seeded ONCE from the published file if this process is new.

    WARNING: WITHOUT THIS A RESTART LEAVES A RESERVATION NOBODY OWNS. The table ROW is
    persisted by `tmroom` and this list was not, so a fresh `authsess` came up
    serving `state 1, seated 1, <occupancy id>` with an empty seat list behind
    it -- reported 2026-08-20T19:25 as a table still showing reserved with "no
    one has done anything with the table since the clients restarted", and it can
    never clear, because every path that would free it iterates THIS dict. See
    `tmroom.note_seats`.

    `setdefault`-shaped on purpose: anything this process has already learned is
    newer than the file, exactly as `tmroom._adopt` argues.
    """
    rooms = _SEATED.setdefault(chan, {})
    if chan not in _SEATED_ADOPTED:
        _SEATED_ADOPTED.add(chan)
        try:
            import tmroom
            for idx, seats in (tmroom.seats(chan) or {}).items():
                if idx not in rooms:
                    rooms[idx] = list(seats)
            if rooms:
                common._say("tm: adopted %s's seat list from the roster file -- %s"
                     % (chan, ", ".join("table %d: %d seated" % (i, len(v))
                                        for i, v in sorted(rooms.items()))))
        except Exception as exc:
            common._say("tm:   seat list not adopted for %s (%r) -- a reservation made "
                 "before this process started cannot be released" % (chan, exc))
    return rooms


def _save_seats(chan):
    """Write `chan`'s seat list back, so it outlives this process."""
    try:
        import tmroom
        tmroom.note_seats(chan, _SEATED.get(chan) or {})
    except Exception as exc:
        common._say("tm:   seat list not persisted for %s (%r) -- it will not survive a "
             "restart" % (chan, exc))


def _seat_at_table(member_id, peer_nick, seated, cmd=b""):
    """Add/remove this member at the table `peer_nick` names; publish the count.

    Returns the new seat count, or None if we could not place the message --
    which is not a failure, it is "this did not come from a table peer".
    """
    if member_id is None or os.environ.get("POL_TM_SEATS", "1") != "1":
        return None
    try:
        import tmroom
        chan = tmroom.room_of(member_id)
        if not chan:
            # WARNING: THIS USED TO RETURN SILENTLY, and that is the whole reason two
            # live reservation faults could not be told apart. Measured
            # 2026-08-20T17:48:15Z: member 3's `@GameEN=` produced NO publish and
            # NO line, while member 9's on the same table seconds earlier
            # published fine -- and the difference is invisible without this.
            # The client meanwhile believes it HAS a reservation (its own local
            # state), so the tile keeps its authored "open" label with a
            # highlight under it, and the next player to join is offered the
            # settings dialog as though the table were free.
            common._say("tm: WARNING: member %s tried to take a seat and we cannot place "
                 "them -- %s. The seat is NOT recorded, so no other client will "
                 "see this reservation and the next joiner will be offered a "
                 "fresh one."
                 % (member_id, tmroom.why_no_room(member_id)))
            return None
        index, why = peers.table_index_for_peer(peer_nick, tmroom.room_peer(chan),
                                          chan=chan)
        if index is None:
            common._say("tm:   seat NOT placed [%s]" % why)
            return None
        # THE ID THE CLIENT COMPARES AGAINST IS ITS OWN `@GameEN=/NN=`, AND IT IS
        # IN THIS VERY MESSAGE. `tmroom.guid_of` is the wrong source and says so
        # -- it refuses a 64-bit TM id outright, because the member record's
        # +0x00 is the LOBBY-band guid and the two must not be conflated.
        m = re.search(rb"/NN=([0-9A-Fa-f]{1,16})", cmd or b"")
        ident = int(m.group(1), 16) if m else 0
        # WARNING: ONE PLAYER, ONE SEAT, AND ONLY WHILE THEY ARE IN THE ROOM.
        # Reported live, 2026-08-20: "if I have a game reserved and I back out and go
        # back in it's still reserved". The client sends **no `@GameExit=` at
        # all** -- checked across a whole session's `TM0 <- code=` lines: two
        # `@GameEN=`, two `@Tet=`, one `@GameQT=`, and not one exit. So a seat
        # can never be released by a message and has to be released by STATE:
        # taking this member off every other table here, and dropping anybody
        # the room roster no longer holds. Otherwise the first reservation of a
        # session pins that table for ever, which is worse than not showing it.
        rooms = _seats_of(chan)
        # ONE PLAYER, ONE SEAT -- and since 2026-08-20 pm, one seat GLOBALLY,
        # because the client has exactly one "my table" slot: a new reservation
        # anywhere replaces whatever it held. The presence half of the old sweep
        # ("drop anybody the room roster no longer holds") is GONE -- that was
        # release-on-room-exit by the back door, the same policy the PART site
        # just retired. Departed members are released by the reconcile's
        # no-live-session rule, not by whoever happens to sit down next.
        for other in [c for c in list(_SEATED) if c != chan]:
            other_rooms = _seats_of(other)
            other_touched = []
            for oidx, oseats in other_rooms.items():
                obefore = list(oseats)
                oseats[:] = [(m, i) for (m, i) in oseats if m != member_id]
                if oseats != obefore:
                    other_touched.append((oidx, obefore))
            for oidx, obefore in other_touched:
                tablerow._republish_table(tmroom, other, oidx, other_rooms.get(oidx) or [])
                gone2 = tablerow._owner_changed(obefore, other_rooms.get(oidx) or [])
                if gone2 is not None:
                    tablerow._push_owner_change(tmroom, other, oidx, gone2,
                                       list(other_rooms.get(oidx) or []))
                common._say("tm:   %s table %d: seat moved -- member %s reserved in %s "
                     "(one seat per player, the client has one my-table slot)"
                     % (other, oidx, member_id, chan))
            if other_touched:
                _save_seats(other)
        touched = set()
        vacated = {}
        for idx, seats in rooms.items():
            before = vacated[idx] = list(seats)
            seats[:] = [(mid, i) for (mid, i) in seats if mid != member_id]
            if seats != before:
                touched.add(idx)
        who = rooms.setdefault(index, [])
        if seated:
            who.append((member_id, ident))
        touched.discard(index)
        for idx in sorted(touched):
            tablerow._republish_table(tmroom, chan, idx, rooms.get(idx) or [])
            # THE SAME DEPARTURE, ONE TABLE OVER. This loop vacates seats for two
            # reasons -- this member sat down somewhere else, or somebody it
            # holds is no longer in the room roster -- and either can take an
            # owner off a table that still has people at it. `release_seats` is
            # the PART path only, so without this the owner walking to the next
            # table leaves their guest behind with a table nobody is told they
            # now own. Same helper, so the two cannot drift.
            gone = tablerow._owner_changed(vacated.get(idx) or [], rooms.get(idx) or [])
            if gone is not None:
                tablerow._push_owner_change(tmroom, chan, idx, gone,
                                   list(rooms.get(idx) or []))
        row, authored = tablerow._table_rows(tmroom, chan, index)
        if row is None:
            return len(who)
        # THE ROW's view of who is there, which a VS. COM game joins without a
        # seat (`_COM_AT`). `who` itself is NOT reassigned: it is the seat
        # list, and this function's return value is the `/EN=` seat count.
        pub = who or vscom._com_who(chan, index)
        cap = int(row[4] or 0)
        # `0x76338` refuses a table whose seated >= 8, so the count is bounded by
        # the capacity the row itself declares rather than by a literal.
        row[5] = str(min(len(pub), cap) if cap else len(pub))
        # VERIFIED: AND THE FIELD THE SCREEN ACTUALLY BELIEVES. block[14..29] is one
        # 64-bit id, most-significant nibble first, and the tile loop's state-1
        # arm opens with `or ebp, edx; je` on it: a ZERO id short-circuits to
        # display code 2, the empty tile. Every table this server has served has
        # been all-'A' = 0, which is why a reservation changed nothing.
        row[6] = tablerow._occupancy_block(row[6], pub)
        # The AUTHORED state is what an empty table goes back to -- taking it
        # from `row` would leave the table stuck on the last occupant's state.
        # Recruiting (state 1) until the owner confirms -> ready (state 2).
        # inplay: a seat change during a RUNNING match (the start sequence's
        # own PART churn, a mid-game release) must not demote "Playing" to
        # recruiting -- see _match_running.
        row[3] = tablerow._occupancy_state(pub, (authored or row)[3],
                                  confirmed=tablerow._confirmed_flag(tmroom, chan, index),
                                  inplay=True if matchmaking._match_running(chan, index)
                                  else None)
        if tmroom.note_table(chan, row):
            common._say("tm:   %s table %d -> %s seated, sequence %d -- pushed to every "
                 "client in the room" % (chan, index, row[5],
                                         tmroom.sequence(chan)))
        _save_seats(chan)
        # STATE 3 ON THE SEATED MEMBER, NOW. The owner's "Start Game" gate
        # (0x5C0D0) reads each reserved player's status block; re-marking here
        # emits the PD that carries block[8]='D' without a re-fetch. See
        # tmroom.remark_seated / _mark_status.
        try:
            if member_id is not None:
                tmroom.remark_seated(member_id)
        except Exception as exc:
            common._say("tm:   seated status not re-marked (%r)" % (exc,))
        # FILL THE RESERVATION SLOT so the owner's tile unlocks and Start Game
        # can evaluate -- pushed on every seat change, both directions, since a
        # departure also changes who is reserved. See _push_reservation_list.
        reservation._push_reservation_list(tmroom, chan, index, who)
        # CAPTURE THE MATCH ROSTER on every seat change, independent of
        # _announce_match (which POL_TM_MATCH_AT=0 disables). The guest wake-up on
        # the host's @GameReady= needs to know who was seated with the host AFTER
        # the host PARTs the room, and this seat-change is the last moment both are
        # in `who`. _remember_match only resets pre-match state (accepts/hands/
        # turn), which is empty before a game starts, so calling it here is safe.
        if who:
            matchmaking._remember_match(chan, index, who)
        if seated:
            matchmaking._announce_match(tmroom, chan, index, who)
        return len(who)
    except Exception as exc:
        common._say("tm:   seat not published (%r)" % (exc,))
        return None


def _block_fit(current, need):
    """The block, long enough to write index `need-1`, and NEVER LONGER.

    WARNING: THIS IS THE CRASH. Both writers used to do `ljust(64, 'A')`, which
    filled the whole 64-byte field and destroyed the NUL that has to follow the
    letters: `tools/tmptl.py` records `T_SET_END = 49` as "block[49] -- and NUL
    from here (0x86074)", and the record is 104 bytes with +0x28 running to the
    very end, so a 64-character block leaves the client's `mgStrCopyLim(rec+0x28,
    src, 0x40)` an UNTERMINATED string. Both players' clients died the instant the
    first such row was published (live testing, 2026-08-20).

    WARNING: It was DORMANT until the commit that fixed the `tools/` import: while
    those writers were silently no-oping in the container, nothing ever padded.
    Fixing one bug is what armed the other.
    """
    cur = current or ""
    return cur.ljust(max(len(cur), need), "A")
