"""Checking and healing a room's tables: the audit, reconcile, and releasing seats when members
leave.
"""
import os
from . import (
    common, matchmaking, pushqueue, rematch, seating, tablerow, turns, vscom, watchers, webwatch,
)


def audit_room_tables(chan, seats_by_index=None):
    """Every way `chan`'s published table state can disagree with the truth --
    READ-ONLY, the checker half of the invariant.

    WARNING: THIS LIST IS THE INVARIANT'S DEFINITION, IN ONE PLACE: *table state must
    not outlive the presence that created it*, extended to the DERIVED row
    fields the client actually renders from (`row[5]` seat count, `block[4..5]`
    count, `block[14..29]` occupancy id) -- the fields `reconcile_room_tables`'
    stored-row heal has never checked, and per the 0x674C4 switch the exact
    fields that draw "Setting Up" (state 0, count != 0) and Join/owned (state
    1, foreign id) on a table nobody is at. `services/tmtables.py` prints these
    verdicts from inside the container; Phase 2's reconcile is to HEAL from this
    same list. A check that lives anywhere else lets the checker and the healer
    disagree, which is how six one-producer "fixes" happened.

    Returns `[{"table": idx, "kind": slug, "severity": "bad"|"info",
    "detail": line}, ...]`; empty means CLEAN. Never raises -- an unreadable
    room returns a single `audit-failed` entry, because silence is what let
    every earlier producer hide.

    `seats_by_index` defaults to the SHARED store (`tmroom.seats`), so a
    standalone audit reads the same truth both containers share; in-process
    callers may pass `_seats_of(chan)` to audit what THIS process believes.
    Presence evidence (live sessions) is only consulted when `responders` is
    already loaded -- i.e. inside a serving band. A standalone process has an
    EMPTY presence registry, and judging seats "unbacked" against it would
    manufacture exactly the false absence the d1bd5100 bounce guard exists to
    prevent; standalone, those checks degrade to `severity: "info"`.
    """
    out = []

    def note(idx, kind, detail, severity="bad"):
        out.append({"table": idx, "kind": kind, "severity": severity,
                    "detail": detail})
    try:
        import tmroom
        if seats_by_index is None:
            seats_by_index = tmroom.seats(chan)
        seats_by_index = {int(i): [(int(m), int(x or 0)) for m, x in (s or [])]
                          for i, s in (seats_by_index or {}).items()}
        recorded = {int(m) for m, _v in tmroom.members(chan)}
        rows = dict(tmroom.tables(chan))
        try:
            import titles
            sessions_known = titles.core.bound
        except ImportError:
            sessions_known = False

        # --- seats vs presence, and seats vs the published rows -------------
        for idx, seats in sorted(seats_by_index.items()):
            if seats and ("#TM0T%03d" % idx) not in rows:
                note(idx, "seats-without-row",
                     "the server holds %d seat(s) here but publishes NO row -- "
                     "every client sees the authored free tile while the next "
                     "@GameEN= is refused" % len(seats))
            for mid, ident in seats:
                place = tmroom.room_of(mid)
                has_record = mid in recorded
                alive = None
                if sessions_known:
                    try:
                        alive = bool(titles.core.PRESENCE.sessions_for(mid))
                    except Exception:
                        alive = None
                if place and place != chan:
                    # The one release_seats_elsewhere exists for -- if it shows
                    # up here, that path missed an event.
                    note(idx, "seat-holder-elsewhere",
                         "member %s holds a seat here but their record says "
                         "they are in %s" % (mid, place))
                elif not has_record and alive is False:
                    note(idx, "seat-unbacked",
                         "member %s holds a seat with no room record AND no "
                         "live session -- the reconcile sweep's own definition "
                         "of absent" % mid)
                elif not has_record:
                    note(idx, "seat-off-room",
                         "member %s holds a seat with no record in this room; "
                         "presence %s -- legitimate if they are mid-navigation "
                         "(reservation persistence), stale otherwise"
                         % (mid, "is live" if alive else
                            "is unknown from this process"),
                         severity="info")

        # --- the member records' seated letters vs the seat list ------------
        # block[8]/block[9] of a record's status block are what the client's
        # Start-Game gate reads (+0x48/+0x4C); a 'D' with no seat behind it is
        # drift. Measured on prod 2026-08-21: release_seats re-marked BEFORE
        # saving the seat list, so every release left the letters seated.
        seated_members = {int(m) for s in seats_by_index.values() for m, _i in s}
        for mid, vals in tmroom.members(chan):
            v10 = vals[10] if len(vals) > 10 else None
            if not isinstance(v10, str):
                continue
            if tmroom._mark_status(v10, int(mid) in seated_members) != v10:
                note(None, "member-letters-drift",
                     "member %s's record letters disagree with their seat "
                     "(seated=%s) -- the Start-Game gate reads these"
                     % (mid, int(mid) in seated_members))

        # --- the published rows vs the seat list ----------------------------
        for name, row in sorted(rows.items()):
            if not name.startswith("#TM0T"):
                continue
            try:
                idx = int(name[5:])
            except ValueError:
                note(None, "unparseable-name",
                     "stored row %r cannot be indexed" % name)
                continue
            seats = seats_by_index.get(idx) or []
            authored = tmroom.fixture_table(idx)
            authored_state = (tmroom._as_int(authored[3]) if authored
                              else tmroom.EMPTY_TABLE_STATE)
            state = tmroom._as_int(row[3])
            if tmroom._as_int(row[2]) < tmroom.TABLE_ID_MIN:
                note(idx, "colliding-id",
                     "row id %s is below TABLE_ID_MIN -- collides with member "
                     "ids on the client" % row[2])
            elif (tmroom._room_no(chan)
                  and tmroom._as_int(row[2])
                  == tmroom.canonical_table_id(idx - 1, 0)):
                # Measured live 2026-08-21: the client's my-reservation global
                # matched table 1 in EVERY room because all rooms shared the
                # pre-fold id -- the yellow tile that follows a player around.
                note(idx, "room-shared-id",
                     "row id %s is the PRE-FOLD canonical -- identical in "
                     "every room, so one client's my-reservation matches this "
                     "table everywhere" % row[2])
            if not seats and state != authored_state:
                note(idx, "row-occupied-nobody",
                     "state %s with no seat behind it (authored state is %s) "
                     "-- the classic phantom" % (state, authored_state))
            if seats and state == authored_state:
                note(idx, "row-free-somebody",
                     "%d seat(s) held but the row still says the authored "
                     "state %s -- no client can see the reservation"
                     % (len(seats), authored_state))
            row_count = tmroom._as_int(row[5])
            if row_count != len(seats):
                note(idx, "seat-count-drift",
                     "row[5] says %d seated, the seat list holds %d"
                     % (row_count, len(seats)))
            blk = row[6] or ""
            if len(blk) >= 30:
                cnt = occ = None
                try:
                    cnt = tmroom.unletters(blk[4:6])
                    occ = tmroom.unletters(blk[14:30])
                except Exception:
                    note(idx, "block-undecodable",
                         "block %r does not letter-decode" % blk[:30])
                if cnt is not None and cnt != len(seats):
                    note(idx, "block-count-drift",
                         "block[4..5] says %d seated, the seat list holds %d "
                         "-- this byte pair gates EVERY arm of the tile "
                         "switch (0x671DD)" % (cnt, len(seats)))
                if occ is not None:
                    want = next((i for _m, i in seats if i), 0)
                    if not seats and occ:
                        note(idx, "ghost-occupancy-id",
                             "block[14..29] carries id 0x%X with nobody "
                             "seated -- renders as somebody's table" % occ)
                    elif seats and want and occ != (want & 0xFFFFFFFFFFFFFFFF):
                        note(idx, "occupancy-id-drift",
                             "block id 0x%X but seat 0's ident is 0x%X -- "
                             "ownership will render wrong" % (occ, want))
                    elif seats and not want and occ:
                        note(idx, "occupancy-id-unbacked",
                             "block id 0x%X but no seat carries an ident "
                             "(the /NN=-less case) -- id kept from a previous "
                             "occupant?" % occ)
            else:
                note(idx, "short-block",
                     "block is %d chars; the client reads 30+ (count at 4..5, "
                     "id at 14..29)" % len(blk))
            try:
                if tmroom.table_confirmed(chan, idx) and not seats:
                    note(idx, "confirm-without-seats",
                         "table is marked rules-confirmed with nobody seated "
                         "-- a future owner inherits a confirm they never "
                         "gave")
            except Exception:
                pass
    except Exception as exc:
        note(None, "audit-failed", repr(exc))
    return out


def reconcile_room_tables(chan):
    """Heal `chan`'s seats and table rows against the roster. Runs on every <DE>.

    WARNING: THE INVARIANT THIS ENFORCES IS THE ONE THE WHOLE DAY KEPT BREAKING:
    **table state must not outlive the presence that created it.** Every stale
    variant live testing reported -- a reservation surviving a restart, a
    "Setting up" tile in a room empty for an hour, a row carrying a state the
    client cannot render or an id from before a fix -- is this invariant broken
    somewhere, and every previous fix repaired ONE producer. This repairs the
    STATE, on an event that is guaranteed to precede anybody looking at it: the
    `<DE>` a client sends on entering the room, before it fetches `b/g/PTL`.

    Three repairs, all idempotent, all published only when something changed:

      * a seat whose member has NO RECORD in the room is released -- the seat
        was created by presence, and the record is presence's source of truth
        (`release_seats` exists for the event-driven case; this is the sweep
        for every event that never fired: dead sockets during downtime, ghost
        expiry, any release path that missed);
      * a table whose seats emptied is republished free, through the same
        `_republish_table` the event path uses, so the TD and the snapshot say
        the same thing;
      * a stored row still carrying a pre-fix defect -- a state the client has
        no arm for, an id small enough to collide -- is republished normalised.
        `note_table` already rewrites the id on the way in; the state is
        clamped here for exactly the row measured live at 22:07 ("#TM0T001",
        state 7, id 0x1, untouched since before both fixes deployed).

    Returns the number of tables republished. `POL_TM_RECONCILE=0` disables.
    """
    if os.environ.get("POL_TM_RECONCILE", "1") != "1":
        return 0
    try:
        import tmroom
        # WARNING: LIVE-SESSION-BACKED, NOT ROOM-RECORD-BACKED (2026-08-20 pm). The
        # first cut swept seats whose member had no record IN THE ROOM -- which
        # contradicts reservation persistence: the client keeps its table across
        # a room exit (it has a Cancel menu for the deliberate case), so walking
        # out retires your room record and the old sweep then freed your table
        # behind your back. A seat is now released here only when its member has
        # no session anywhere on the service -- the same evidence the stale-
        # record retire trusts. Standalone (no responders importable, i.e. the
        # selftests), nobody is judged absent.
        # WARNING: ABSENT = NO SESSION **AND** NO ROOM RECORD, a conjunction -- and the
        # second half went in ten minutes after the first shipped without it.
        # Measured 23:47:59: an authsess recreate dropped every session, the
        # first <DE> in ran this sweep before the other player's client had
        # re-ponged, and his seat was released as "absent" while he sat there.
        # His room RECORD had survived in the file the whole time. A session
        # can blip (deploy bounce, relay reconnect); a session AND record both
        # gone is somebody who actually left the service.
        recorded = {int(m) for m, _v in tmroom.members(chan)}

        def _absent(mid):
            if int(mid) in recorded:
                return False
            # A record that MOVED to another room is absence FROM THIS room:
            # `release_seats_elsewhere` frees the seat on their <DE> into the
            # new room, but that event can land on the other container or be
            # lost to a restart -- the audit's `seat-holder-elsewhere` kind.
            # Records only move on the member's own <DE>, so this cannot fire
            # on a session blip (the d1bd5100 case keeps the record HERE).
            place = tmroom.room_of(mid)
            if place and place != chan:
                return True
            try:
                import titles
                if not titles.core.bound:
                    raise RuntimeError("no core bound (standalone)")
                return not titles.core.PRESENCE.sessions_for(mid)
            except Exception as exc:
                # WARNING: LOUD ON PURPOSE (was a silent `return False`): an import
                # failure here disables the whole sweep while looking exactly
                # like "everyone is present" -- the success-only-log trap.
                common._say("tm: WARNING: reconcile %s cannot read presence (%r) -- "
                     "judging member %s PRESENT by default; the absent-seat "
                     "sweep is NOT running" % (chan, exc, mid))
                return False
        # The heartbeat sweep first, so a cancelled/silent holder's seat is
        # already gone by the time the row heal below recomputes -- and so an
        # idle room heals on the next entry even if no table @Pong ever
        # triggers the sweep again.
        matchmaking.expire_silent_seats()
        # ...and the COM tables on the same tick, for the same reason: a room
        # nobody pongs would otherwise keep a dead COM game's "Playing" tile
        # until somebody happened to beat.
        vscom.expire_com_tables()
        rooms = seating._seats_of(chan)
        touched = 0
        freed_any = False
        for idx, seats in sorted(rooms.items()):
            before = list(seats)
            seats[:] = [(m, i) for (m, i) in seats if not _absent(m)]
            if seats == before:
                continue
            gone = [m for m, _i in before if _absent(m)]
            common._say("tm: reconcile %s table %d: seat(s) held by departed member(s) "
                 "%s released -- no live session backs them"
                 % (chan, idx, ", ".join(str(m) for m in gone)))
            tablerow._republish_table(tmroom, chan, idx, seats)
            heir = tablerow._owner_changed(before, seats)
            if heir is not None:
                tablerow._push_owner_change(tmroom, chan, idx, heir, list(seats))
            touched += 1
            freed_any = True
        if freed_any:
            seating._save_seats(chan)
        # A READY TABLE ANNOUNCES ITSELF ON THE NEXT ROOM ENTRY. The launch
        # freeze means the seated clients can produce NO event to retrigger the
        # match after a hold (measured: every control is locked), so a hold that
        # resolves server-side -- a confirm flag restored from stored rules, a
        # deploy interrupting the announce -- needs a trigger that does not come
        # from them. The <DE> this sweep already rides is exactly that: anybody
        # entering the room re-runs the announce, which dedups its own pushes
        # and re-checks its own gates, so a table that already launched or is
        # still unconfirmed is a no-op.
        try:
            need = common._env_int("POL_TM_MATCH_AT", 2)
            if need > 1:
                for idx, seats in sorted(rooms.items()):
                    if len(seats) >= need:
                        matchmaking._announce_match(tmroom, chan, idx, list(seats))
        except Exception as exc:
            common._say("tm:   reconcile announce skipped (%r)" % (exc,))
        # THE STORED-ROW HEAL -- driven by `audit_room_tables`, THE definition
        # of a violation, so the checker and this healer cannot disagree
        # (the first slice of the table-state plan). This replaces the
        # old state/id-only check, which was BLIND to the derived fields the
        # client actually renders from: a free table keeping a stale seat
        # count in `block[4..5]` draws display code 2 -- "Setting Up" -- with
        # a clean state, and a stale `block[14..29]` id draws Join/owned. The
        # heal is a republish through `_republish_table`, which recomputes
        # state, row[5] and the block from the seat list (the closest thing
        # to a renderer until Phase 2b lands) -- and note_table inside it
        # rewrites a colliding id and logs the decoded result. It also matters
        # CLIENT-side: the reservation scene's own tick (0x8F6C0) clears the
        # client-local reservation global when our served row stops saying
        # "reserved by me", so healing the row is what releases a parked
        # client -- the only server-reachable clear that exists (the
        # cross-room case has none; that is the shim's job, Phase 3).
        seat_kinds = ("seat-unbacked", "seat-holder-elsewhere", "seat-off-room")
        flagged = {}
        drifted_members = []
        for v in audit_room_tables(chan, rooms):
            if v["severity"] != "bad" or v["kind"] in seat_kinds:
                continue                    # seats were repaired above
            if v["kind"] == "member-letters-drift":
                drifted_members.append(v)
                continue
            idx = v.get("table")
            if idx is None:
                continue
            flagged.setdefault(idx, []).append(v["kind"])
        # Seated-letter drift heals by re-marking the record from the (just
        # saved) seat list -- remark_seated no-ops when nothing changes.
        if drifted_members:
            seated_now = {int(m) for s in rooms.values() for m, _i in s}
            for mid, vals in tmroom.members(chan):
                v10 = vals[10] if len(vals) > 10 else None
                if (isinstance(v10, str)
                        and tmroom._mark_status(v10, int(mid) in seated_now)
                        != v10):
                    if tmroom.remark_seated(mid):
                        common._say("tm: reconcile %s: member %s's seated letters "
                             "re-marked to match their seat" % (chan, mid))
                        touched += 1
        for idx, kinds in sorted(flagged.items()):
            tablerow._republish_table(tmroom, chan, idx, rooms.get(idx) or [])
            common._say("tm: reconcile %s table %d: republished for %s"
                 % (chan, idx, ", ".join(sorted(set(kinds)))))
            touched += 1
        return touched
    except Exception as exc:
        common._say("tm:   reconcile of %s failed (%r) -- state stands as it was"
             % (chan, exc))
        return 0


def void_all_seats(member_id, why=""):
    """Drop `member_id` from EVERY table in every room, and republish.

    WARNING: A SEAT MUST NOT OUTLIVE THE SESSION THAT MADE IT -- and a crash plus a
    fast re-login slips past `_roster_retire_on_close`'s "still present" guard
    (the new session exists before the dead one is processed), so the seat
    persists into the fresh session. Measured 2026-08-21T00:39: member 6 seated
    at #TM0R001 table 1 from a crashed match, both accounts re-logged, and the
    stale reservation rendered on both screens with nobody having reserved.

    Called on `@Init=` -- the game-service login, which fires once per session
    and always before any reservation in it. Room-to-room navigation does not
    re-init, so a legitimate "reserve, leave the room, come back" keeps its
    seat; only a fresh session voids it. `POL_TM_INIT_VOIDS_SEATS=0` disables.
    """
    if member_id is None:
        return
    try:
        import tmroom
    except Exception:
        return
    touched_any = False
    for chan in list(seating._SEATED):
        rooms = seating._seats_of(chan)
        freed = []
        for idx, seats in rooms.items():
            before = list(seats)
            seats[:] = [(m, i) for (m, i) in seats if m != member_id]
            if seats != before:
                freed.append((idx, before))
        for idx, before in freed:
            tablerow._republish_table(tmroom, chan, idx, rooms.get(idx) or [])
            gone = tablerow._owner_changed(before, rooms.get(idx) or [])
            if gone is not None:
                tablerow._push_owner_change(tmroom, chan, idx, gone,
                                   list(rooms.get(idx) or []))
        if freed:
            seating._save_seats(chan)
            touched_any = True
            common._say("tm: voided member %s's seat(s) in %s%s -- table(s) %s"
                 % (member_id, chan, (" (" + why + ")") if why else "",
                    ", ".join(str(i) for i, _b in freed)))
    if touched_any:
        # This path never re-marked at all -- the voided member's record kept
        # its seated letters (block[8]='D'), which feeds the Start-Game gate.
        try:
            tmroom.remark_seated(member_id)
        except Exception:
            pass
    return touched_any


def release_seats(member_id, chan=None):
    """Take `member_id` off every table in `chan` and republish what that frees.

    WARNING: A SEAT AND A ROOM RECORD ARE DIFFERENT STATE AND THEY WERE DRIFTING.
    Reported 2026-08-20: a tester backed out of the room, came back, and the
    client told them they ALREADY HAD A RESERVATION on table 1 -- while player 2
    could not see it. That is exactly the two halves disagreeing: the PART path
    retires the member RECORD (`tmroom.forget_member`), but the seat list lives
    in this process and the published table row kept their id, so the table said
    "occupied by you" to one client and the roster said "not here" to the other.

    Called from the PART handler BEFORE the record is retired, so `room_of` can
    still place them.
    """
    if member_id is None:
        return
    try:
        import tmroom
        chan = chan or tmroom.room_of(member_id)
        if not chan:
            return
        rooms = seating._seats_of(chan)
        freed = []
        inherit = []
        for idx, seats in rooms.items():
            before = list(seats)
            seats[:] = [(m, i) for (m, i) in seats if m != member_id]
            if seats == before:
                continue
            freed.append(idx)
            gone = tablerow._owner_changed(before, seats)
            if gone is not None:
                inherit.append((idx, gone, list(seats)))
        for idx in sorted(freed):
            tablerow._republish_table(tmroom, chan, idx, rooms.get(idx) or [])
        if freed:
            common._say("tm: member %s left %s -- released table(s) %s"
                 % (member_id, chan, ", ".join(str(i) for i in freed)))
            seating._save_seats(chan)
            # WARNING: AFTER _save_seats, not before -- remark_seated recomputes from
            # the SHARED seat list, so re-marking first read the STALE list and
            # left block[8]='D' on the record. Measured on prod 2026-08-21
            # 06:06:10Z: the release ran, the member record kept its seated
            # letters (the audit's member-letters-drift kind).
            try:
                tmroom.remark_seated(member_id)   # back to state 0, emits a PD
            except Exception:
                pass
        for idx, gone, seats in inherit:
            tablerow._push_owner_change(tmroom, chan, idx, gone, seats)
        # A SEAT RELEASED UNDER A MATCH IS THE CRASH SIGNAL. Measured
        # 2026-09-06T23:30:45Z: member 18's connection closed a minute into
        # the 3-player freeze, this path freed table 1 ("CHANGED HANDS") --
        # and `_MATCH_ROSTER[18]` stood, so every later game inherited the
        # dead match (the COM-never-plays bug). See `_match_abandoned`.
        _got = matchmaking._MATCH_ROSTER.get(pushqueue._push_key(member_id))
        if freed and _got and _got[0] == chan and _got[1] in freed:
            _match_abandoned(member_id, "seat released at %s table %s"
                             % (chan, _got[1]))
    except Exception as exc:
        common._say("tm:   seats not released for %s (%r)" % (member_id, exc))


def _match_abandoned(member_id, why):
    """This member is OUT of the PvP match `_match_of` still names them in --
    a crash, a dropped connection, a seat released under them, a new game
    started elsewhere -- and no `@Break=`/`@GameExit=` is coming. Hand the
    seat to the server if the others are still playing, then FORGET the
    member's roster entry; when no human is left, wipe the table's match
    state so the next game there (and this member's next game anywhere)
    starts clean. Returns True when a roster entry was retired.

    WARNING: THE COM-NEVER-PLAYS BUG (2026-09-06T23:35Z).
    After the 3-player freeze member 18's client died; nothing polite ever
    arrived, so `_MATCH_ROSTER[18]` kept the 3-seat lineup for ever (only
    `_remember_match` writes it and nothing popped it). Their VS. COM game
    then read a 3-seat PvP roster at every handler: `@CardSelect=` saw
    through it and dealt, but the `@TurnData=` ack derived `_cn` from the
    stale seats and swallowed the COM's put -- and it persisted across a
    client restart because the state was HERE. The 08-25 case (reserve a
    table, then walk into VS. COM) is the same hole from the front door.

    Three producers call this: `release_seats` when a seat is actually freed
    under the member's match (the drop path), `@GameENC=` (a new COM game),
    and `_remember_match` when the member is seated at a DIFFERENT table.
    `POL_TM_ABANDON_CLEANUP=0` disables all three (and the COM-binding
    release in `_remember_match`).
    """
    if not common._env_int("POL_TM_ABANDON_CLEANUP", 1) or member_id is None:
        return False
    key = pushqueue._push_key(member_id)
    got = matchmaking._MATCH_ROSTER.get(key)
    if not got:
        return False
    chan, index, seats = got
    try:
        # The others are still in it: the server plays this seat (the same
        # arm `@Break=`/`@GameExit=` take). Needs `_match_of` intact, so
        # BEFORE the pop.
        turns._seat_departed(member_id, why)
    except Exception as exc:
        common._say("tm:   ...seat takeover for member %s failed (%r) -- retiring "
             "the roster entry anyway" % (member_id, exc))
    matchmaking._MATCH_ROSTER.pop(key, None)
    _run = matchmaking._MATCH_STARTED.get((chan, index))
    if _run:
        _run.discard(key)
    left = [m for m, _v in seats
            if pushqueue._push_key(m) != key
            and (matchmaking._MATCH_ROSTER.get(pushqueue._push_key(m)) or (None, None))[:2]
            == (chan, index)
            and not turns._is_bot(chan, index, m)]
    common._say("tm: 🧹 member %s is OUT of %s table %s (%s) -- roster entry retired; "
         "%s" % (member_id, chan, index, why,
                 "%d other player(s) still hold the match" % len(left) if left
                 else "no human is left, wiping the table's match state"))
    if left:
        webwatch._live_matches_write()
        return True
    for m, _v in seats:
        matchmaking._MATCH_ROSTER.pop(pushqueue._push_key(m), None)      # departed/bot entries too
    rematch._reset_for_rematch(chan, index)
    matchmaking._MATCH_STARTED.pop((chan, index), None)
    matchmaking._MATCH_BEGAN.pop((chan, index), None)
    matchmaking._MATCH_ACCEPTS.pop((chan, index), None)
    matchmaking._accepts_forget(chan, index)
    watchers._MATCH_WATCHERS.pop((chan, index), None)
    webwatch._live_matches_write()
    try:
        import tmroom
        tablerow._republish_table(tmroom, chan, index, seating._seats_of(chan).get(index) or [],
                         why="match abandoned (%s), Playing withdrawn" % why)
    except Exception as exc:
        common._say("tm:   table %s not returned from Playing (%r)" % (index, exc))
    return True


def release_seats_elsewhere(member_id, keep_chan):
    """Drop any seat this member still holds in a room they are NOT in now.

    WARNING: THE ORPHAN reconcile CANNOT SEE, and the recurring "phantom reserved
    table in a room I just walked into" (measured 2026-08-21: a tester recorded in
    #TM0R003 while still seated at #TM0R002 table 1). A seat is created by
    presence and is meant to die with it, but `reconcile_room_tables` frees a
    seat only when its holder has NO session ANYWHERE -- so a member who walks
    from room A to room B keeps their seat in A (they still have a session), and
    nobody entering A frees it either, because to that sweep the holder is merely
    "in another room", not "gone". Presence-in-the-SERVICE was standing in for
    presence-in-the-ROOM.

    A member can only be seated in the room they are in, so ENTERING `keep_chan`
    is exactly the event that retires every OTHER seat they hold. Driven from the
    `<DE>` room-entry handler AFTER the record is moved to the new room, so this
    and `reconcile_room_tables` ride the one event. Idempotent; `release_seats`
    already republishes each freed table and hands off ownership. Enumerates the
    SHARED seat map (`tmroom`), not this process's lazily-seeded `_SEATED`, so a
    seat in a room this worker has not touched yet is still caught.
    """
    if member_id is None:
        return
    try:
        import tmroom
        keep_id = tmroom.room_id_for(keep_chan) if keep_chan else None
        targets = []
        for room_hex, tbls in (tmroom._live_seats() or {}).items():
            try:
                rid = int(room_hex, 16)
            except (TypeError, ValueError):
                continue
            if keep_id is not None and rid == keep_id:
                continue
            if any(int(m) == int(member_id)
                   for rows in (tbls or {}).values()
                   for m, _i in (rows or [])):
                targets.append("#TM0R%03d" % (rid & 0xFFFFFFFF))
        for chan in targets:
            release_seats(member_id, chan)
        if targets:
            common._say("tm: member %s entered %s -- released stale seat(s) left in %s"
                 % (member_id, keep_chan, ", ".join(targets)))
    except Exception as exc:
        common._say("tm:   release_seats_elsewhere failed for %s (%r)" % (member_id, exc))
