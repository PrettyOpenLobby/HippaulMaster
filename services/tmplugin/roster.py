"""The room roster on the auth band: update sequence, deltas, member records, departures and
retirement.
"""
import os
import time
import tetramaster
import tmroom
from . import corenames


# ---------------------------------------------------------------------------
# the roster, the auction, the rankings, the profile pool, chat
# ---------------------------------------------------------------------------
def _roster_sequence():
    """The UPDATE SEQUENCE of the room this connection's member is standing in, or None.

    WARNING: THIS IS WHAT `$SERIAL` MUST RESOLVE TO, and it is the second half of the
    live-roster change. `tmroom.build_ptl` stamps `b/g/PTL` `+0x40` with the room's
    sequence, `cp__002fb560` reads that field as the sequence the snapshot
    represents, and the client reports it straight back on every `<DR>`. So the
    number we answer `<DO>` with has to be THE SAME NUMBER -- and until this it
    was not: `polpro._file_serial` globbed the resource directory for the newest
    stored `*.b_g_PTL.bin` and returned ITS `+0x40`.

    Measured 2026-08-18, with the roster already live and one room in play:

        tm-roster.json   #TM0R001 seq 2     <- stamped into the blob we serve
        stored file      +0x40 = 23         <- what `<DO>` was answering

    A client that read 2 and is told 23 is 21 deltas behind, which is inside
    `cp__002fae98`'s window, so it queues nothing, applies nothing and
    re-requests -- and that is the `b/g/PTL` re-fetch storm in `lobby.log`, 623
    fetches at a flat 3 s cadence. The snapshot half landing without this made
    the mismatch WORSE, not better, because before it both numbers came from the
    same stale file and at least agreed with each other.

    Returns None -- meaning "fall back to the old glob" -- for a session we
    cannot place in a room, which is every non-Tetra-Master caller on this
    channel and every client that has not sent its `<DE>` yet.
    """
    if tmroom is None or os.environ.get("POL_TM_ROSTER", "1") != "1":
        return None
    try:
        _m = corenames._session_get("member_id")
        # Record-first -- same fix, same measurement as `_roster_delta_reply`.
        chan = ((tmroom.room_of(_m) if _m else None) or corenames._room_of_member(_m))
        if not chan:
            return None
        # WARNING: MIRROR `_ptl_with_live_roster`'S CONDITION, DO NOT APPROXIMATE IT.
        # This used to ask `tmroom.members(chan)` -- "do we hold a record?" --
        # which was right until the roster began taking WHO IS PRESENT from the
        # room registry (a333f757). A player who has not sent `<DE>` yet is now
        # built into the blob from a synthesised record, so the blob IS stamped
        # with the room's sequence while `members(chan)` is still empty -- and
        # the old test would have fallen back to the directory glob for exactly
        # that player, re-opening the mismatch this function exists to close.
        # The rule is simply: if a blob gets BUILT, its sequence is the answer.
        who = ((corenames._live_rooms() or {}).get(chan) or {}).get("who") or []
        if not any(row.get("member_id") for row in who)                 and not tmroom.members(chan):
            return None                    # build_ptl returns `base` unchanged
        return tmroom.sequence(chan)
    except Exception:                      # a roster miss must not cost a reply
        return None


def _roster_sync_presence(state):
    """Give a room-registry arrival a member record, so the DELTAS carry it too.

    `tmroom.build_ptl` already synthesises a row for somebody the IRC registry
    knows and we hold no `<DE>` for -- but only at FETCH time, into the snapshot.
    A client that already has the snapshot never fetches again (that is the point
    of the delta stream), so without this it would never learn about them at all:
    the roster's sequence only moves when a RECORD changes, and a synthesised row
    is not a record.

    Storing the synthesised row through `note_member` fixes both halves at once --
    it bumps the room's sequence, emits the `PD`, and is idempotent, because
    `note_member` returns False when the values are unchanged. So this can run on
    every registry mutation, which is where it is called from.

    WARNING: ONE DIRECTION ONLY -- IT ADDS, IT NEVER REMOVES. Absence from the registry
    is NOT a departure: an auth session that blips drops its rows for a moment,
    and keying presence to the socket is precisely the bug that made people blink
    out of each other's lists (see `tmroom.note_member` and `build_ptl`'s "A
    UNION, NOT A FILTER"). A player leaves when their own record says so
    (`stat = 0`) or when the member list is rebuilt from a fetch.
    """
    if tmroom is None or os.environ.get("POL_TM_ROSTER", "1") != "1":
        return
    try:
        for chan, entry in (state or {}).items():
            if not str(chan).startswith("#TM0R"):
                continue                    # Tetra Master rooms only
            for row in (entry or {}).get("who") or []:
                mid = row.get("member_id")
                if not mid or tmroom.room_of(mid):
                    continue                # unbound session, or already placed
                name = row.get("name") or row.get("nick") or ""
                if tmroom.note_member(mid, tmroom.synth_member(chan, mid, name)):
                    corenames.log("authserv",
                        f"  roster {chan}: member {mid} ({name!r}) is in the "
                        f"room with no <DE> yet -- synthesised, sequence "
                        f"{tmroom.sequence(chan)}")
    except Exception as exc:
        corenames.log("authserv", f"  roster: presence sync failed ({exc!r}) -- the "
                        f"snapshot still synthesises the row on fetch")


def _roster_delta_reply(payload):
    """The `<DD>` answer to a class-L `<DR>`, or None to use the template.

    WARNING: THIS IS THE LAST STEP OF THE LIVE-ROSTER CHANGE, AND IT IS WHY THE `b/g/PTL` RE-FETCH STORM EXISTS.
    `<DR>(N)` is the client reporting its own update sequence every 2 s
    (`cp__002fb218`, gated on `LobbyState == 15`). We have always answered it with
    `<DO>`, and `<DO>` is not a version reply -- `cp__002fb7d0` ignores its value
    entirely and, if `LobbyState == 15`, just calls the snapshot reload
    (`cp__002fc608`, a 0xC050 = 49232-byte fetch, which is `tmroom.TOTAL` exactly).
    So every poll was ordering a full re-read of `b/g/PTL`, which is the flat 3 s
    623-fetch cadence in `lobby.log` -- not a client chasing a serial, a client
    doing what we told it to.

    What SE sends instead is the deltas: `<DD>` carrying (sequence, payload) GROUP
    pairs that `cp__002fae98` walks and `cp__002fb290` applies. Three answers,
    and the difference between them is the whole point:

        deltas_after -> [..]   send them, and the client's list changes with no
                               fetch at all
        deltas_after -> []     the client is CURRENT: say NOTHING. Silence is the
                               correct answer to a poll with no news, and it is
                               what stops the storm.
        deltas_after -> None   we cannot serve it from the log (too far behind,
                               or claiming a sequence we never issued -- what a
                               client holding a blob from a previous process
                               looks like). Fall through to the template, i.e.
                               `<DO>`, i.e. reload. That arm is now RARE instead
                               of universal.

    Returns `(payload_bytes_or_None, handled)`. `handled` False means "not a
    `<DR>` I can speak to" and the caller does exactly what it did before.
    """
    if tmroom is None or os.environ.get("POL_TM_DELTAS", "1") != "1":
        return None, False
    try:
        groups = corenames.polpro.parse(payload)
        if not groups or groups[0][0] != "DR":
            return None, False
        have = (groups[0][1] or ["0"])[0]
        member = corenames._session_get("member_id")
        # TRADE RE-SERVE CLOCK (2026-08-24): the room-band @Pong is a ~60s
        # keepalive (measured Cnt=21 -> Cnt=22 one minute apart), useless
        # against the client's one-shot @TrAns purge; THIS <DR> roster
        # heartbeat is the ~2s clock. The tick queues via _queue_push and the
        # copies ride the normal unprompted flusher within seconds.
        try:
            tetramaster._trade_reserve_tick(member)
        except Exception:
            pass
        try:
            tetramaster.event_tick(member)
        except Exception:
            pass
        # RECORD-FIRST, exactly as `_ptl_with_live_roster` does and for a
        # measured reason: Fox's re-joined session carried `member_id 0` in the
        # registry row (rooms-live.json `who=[.., (0, None)]`), so the
        # registry-only lookup placed him nowhere and every `<DR>` he sent --
        # including `<DR>(93)` at sequence 93, CURRENT -- was answered `<DO>`,
        # a reload, forever (measured 22:10:19). His `<DE>` had been recorded
        # six minutes earlier; the RECORD knew exactly where he was.
        chan = ((tmroom.room_of(member) if member else None)
                or corenames._room_of_member(member))
        if not chan:
            return None, False           # cannot place them: template as before
        # WARNING: THE "IS THIS BASE LIVE?" GATE THAT STOOD HERE IS RETRACTED -- see
        # the banner above `_PTL_TEMPLATE_SERIAL`. `have` IS the test: a client
        # reports the `+0x40` of the blob it holds, and `deltas_after` already
        # refuses a sequence we never issued or cannot chain to. Nothing this
        # process holds in memory can second-guess that, because the blob was
        # served by a DIFFERENT CONTAINER.
        deltas = tmroom.deltas_after(chan, have)
        if deltas is None:
            corenames.log("authserv", f"  roster {chan}: <DR>({have}) from member "
                            f"{member} is outside the delta log (we are at "
                            f"{tmroom.sequence(chan)}) -- answering with the "
                            f"template, i.e. a reload")
            return None, False
        if not deltas:
            corenames.log("authserv", f"  roster {chan}: <DR>({have}) from member "
                            f"{member} is CURRENT -- silent, which is what stops "
                            f"the b/g/PTL re-fetch storm")
            return None, True
        # ONE `<DD>` IS ONE IRC NOTICE, SO DO NOT STUFF IT. A client rejoining at
        # 0 can be owed the whole `DELTA_WINDOW` (64), and 64 `PD` groups is
        # ~13 KB on a band whose largest measured line is nothing like that.
        #
        # WARNING: BUT A PREFIX DOES NOT ADVANCE THE CLIENT. MEASURED 2026-08-20, and
        # it is the correction to this block's own previous comment, which said
        # "the client applies it, advances, and the next poll 2 s later carries
        # the rest -- convergent". It is not convergent. It is a hard hang:
        #
        #     19:08:19  <DR>(0) is owed 39 deltas -- sending the first 16
        #     19:08:22  <DR>(0) is owed 39 deltas -- sending the first 16
        #     ...every ~1.5 s, both clients, for as long as anyone watched
        #
        # In live testing it showed up as Tetra Master hanging on "Retrieving
        # table info..." when reserving a table. The prefix path had NEVER RUN before
        # that afternoon -- a backlog over 16 was needed to reach it -- and in
        # all 52 executions since, not one client advanced off the sequence it
        # came in on. There is no instance in any log of a chunked batch
        # converging. Whatever the client does with a batch that does not bring
        # it current, applying it and re-reporting the new sequence is not it.
        #
        # SO WHEN WE CANNOT SEND THE WHOLE CHAIN, SEND NONE OF IT and let the
        # client reload the snapshot instead. That is the same answer
        # `deltas_after` already gives a client past `RELOAD_BEHIND`, it is a
        # path with years of evidence behind it, and `build_ptl` stamps the blob
        # with the current sequence -- so the client comes back CURRENT and the
        # next poll is the silent one. Convergent, without having to be right
        # about a size limit we have not measured.
        #
        # `POL_TM_DELTA_CHUNK=0` sends the lot in one notice (the old escape
        # hatch, still there). `POL_TM_DELTA_PREFIX=1` restores the truncating
        # behaviour for whoever wants to measure WHY the client refuses it --
        # that is a real open question, just not one to hang the room on.
        #
        # WARNING: 16 IS MEASURED NOT TO APPLY -- the default came down to 4 on
        # 2026-08-20T22:33. A client at <DR>(0) owed a COMPLETE 16-delta chain
        # (len == chunk, so the reload guard above never fired) was handed the
        # identical <DD> every 3 s, ten times and counting, never moving off 0
        # -- observed live as the client TIMING OUT ON ROOM EXIT. Same
        # client, same evening: 1 delta applies, 4 deltas apply ("<DR>(0) -> 4
        # delta(s) ... then CURRENT", 00:20), 16 does not, twice (as a 16-of-39
        # prefix at 19:07 and as this complete 16-of-16). The ceiling is
        # somewhere in 5..15 and UNMEASURED; 4 is the largest count ever seen
        # to work. Anything longer is a reload, which is measured to converge.
        chunk = int(os.environ.get("POL_TM_DELTA_CHUNK", "4") or 0)
        if chunk and len(deltas) > chunk:
            if os.environ.get("POL_TM_DELTA_PREFIX", "0") == "1":
                corenames.log("authserv", f"  roster {chan}: <DR>({have}) from member "
                                f"{member} is owed "
                                f"{len(deltas)} deltas -- POL_TM_DELTA_PREFIX=1, "
                                f"sending the first {chunk}. THIS HAS NEVER "
                                f"BEEN SEEN TO CONVERGE; expect a re-poll at "
                                f"{have}")
                deltas = deltas[:chunk]
            else:
                corenames.log("authserv", f"  roster {chan}: <DR>({have}) from member "
                                f"{member} is owed "
                                f"{len(deltas)} deltas, more than the {chunk} one "
                                f"notice carries -- answering with the template, "
                                f"i.e. a RELOAD. A prefix does not advance the "
                                f"client and re-polls for ever.")
                return None, False
        # THE RECIPIENT'S OWN ROW USES THE MEMBER-NUMBER SELF-ID -- same rule as
        # `build_ptl`, applied here so a periodic self `<PD>` cannot re-stamp the
        # row with a guid and re-break the "can't select yourself" guard. Only
        # the fetcher's own rows change; every other member's delta is untouched.
        # Value-0 first (the roster key id, POL_TM_VALUE0 -- must agree with
        # build_ptl's snapshot), then the per-recipient self-row override.
        deltas = tmroom.rewrite_value0_deltas(deltas)
        deltas = tmroom.rewrite_self_deltas(deltas, member)
        out = corenames.polpro.build(tmroom.dd_groups(deltas))
        corenames.log("authserv", f"  roster {chan}: <DR>({have}) from member {member} -> "
                        f"{len(deltas)} delta(s) {[d[1] for d in deltas]} up to "
                        f"sequence {deltas[-1][0]}")
        return out, True
    except Exception as exc:
        corenames.log("authserv", f"  roster: delta reply failed ({exc!r}) -- the template "
                        f"still stands")
        return None, False


def _roster_note_guid(member_id):
    """Record the LOBBY-BAND self id for the member record's `+0x00`.

    `_capture_self_guid` already learns this from `u/account`'s fetch subject and
    stores it as `handle.client_guid`; this just carries it into the roster. It
    is the value the working placeholder carried (0x860FB3E2A2) and it has the
    right shape -- 44-bit guid plus a 6-bit slot -- where the 64-bit
    `@Init=/NN=` id does not.
    """
    if tmroom is None or corenames.accounts is None or not member_id:
        return
    # THE NAME THE ROW DRAWS, AND IT IS NOT BEHIND THE HANDLE-ID GATE. The
    # client sends value 4 empty and cannot do better -- it does not know other
    # players' handles -- and `+0x28` is what the room screen draws,
    # so without this the row appears with NO NAME ON IT. It needs only the
    # member, so a session whose handle we cannot resolve still gets a name;
    # putting it below the `hid` check would have silently skipped exactly the
    # sessions most likely to need it.
    try:
        nm = corenames._member_display_name(member_id)
        if nm and tmroom.note_name(member_id, nm):
            corenames.log("authserv", f"  roster: member {member_id} draws as {nm!r}")
    except Exception as exc:
        corenames.log("authserv", f"  roster: cannot name member {member_id} ({exc!r})")
    try:
        db = corenames.accounts.connect(os.environ.get("POL_ACCOUNTS_DB", corenames.accounts.DEFAULT_DB))
        try:
            hid = corenames._session_handle_id(db)
            if not hid:
                return
            row = db.execute("SELECT client_guid FROM handle WHERE id = ?",
                             (int(hid),)).fetchone()
            g = int((row["client_guid"] or 0)) if row is not None else 0
            if g and tmroom.note_guid(member_id, g):
                corenames.log("authserv", f"  roster: member {member_id} (handle {hid}) "
                                f"is {g:#x} on the lobby band -- recorded")
            # WARNING: SAY IT OUT LOUD WHEN WE ARE GUESSING. With no `client_guid` the
            # record's `+0x00` falls back to our own row id: correctly SHAPED,
            # resolvable by nobody, and the row goes quiet with nothing to show
            # for it. `client_guid` is learned from the `u/account` fetch
            # subject, so it arrives only after that client has fetched it once.
            elif tmroom.guid_is_placeholder(member_id)                     and tmroom.warn_once(member_id):
                corenames.log("authserv",
                    f"  roster: member {member_id} (handle {hid}) has NO "
                    f"client_guid yet, so its member record carries our row id "
                    f"-- the row may not resolve on any client until that "
                    f"handle fetches u/account")
        finally:
            db.close()
    except ValueError as exc:
        corenames.log("authserv", f"  roster: REFUSED a lobby-band id -- {exc}")
    except Exception as exc:
        corenames.log("authserv", f"  roster: cannot read the lobby-band id ({exc!r})")


def _roster_note_departure(chan):
    """Retire this connection's member record when they PART a Tetra Master room."""
    if tmroom is None or os.environ.get("POL_TM_ROSTER", "1") != "1":
        return
    try:
        if not bytes(chan).startswith(b"#TM0R"):
            return
        member = corenames._session_get("member_id")
        # WARNING: A PART NO LONGER RELEASES SEATS (2026-08-20, POL_TM_SEAT_PERSISTS).
        # The client keeps its reservation across a room exit -- it sends no
        # exit message on leaving and offers an explicit "Cancel reservation"
        # in the tile menu -- so releasing here manufactured a live divergence:
        # the owner's client showed the yellow reserved tile while the server
        # had freed the table, and the second player was offered it as free and
        # took it out from under him (measured 23:32:26). The seat now dies
        # with the SESSION (retire-on-close, retire-stale, the reconcile's
        # no-live-session sweep) or by the client's own cancel (@GameExit=).
        # The ROOM RECORD still retires below: presence in the room and a claim
        # on a table are different facts with different lifetimes now.
        if member and tetramaster is not None \
                and os.environ.get("POL_TM_SEAT_PERSISTS", "1") != "1":
            tetramaster.release_seats(member)
        if member and tmroom.forget_member(member):
            corenames.log("authserv", f"  roster {chan.decode('latin1', 'replace')}: "
                            f"member {member} PARTED -- record retired, PC "
                            f"delta queued for everyone still in the room")
    except Exception as exc:
        corenames.log("authserv", f"  roster: departure not recorded ({exc!r})")


def _roster_retire_stale(chan):
    """Retire records whose member has NO session anywhere and an OLD record.

    WARNING: THE MEMBER-RECORD EDITION OF THE TABLE-STATE INVARIANT, reported twice in
    one evening (2026-08-20: "it says all three users are in that room" and then
    "LT2 shows PCTest being in the room... it hasn't been online in like two
    hours"). `_roster_retire_on_close` covers a close THIS process sees; a close
    that lands while the process is down -- five deploys ran that evening --
    lands on nobody, and the record is then adopted from the file forever.

    TWO conditions, BOTH required, because each alone is the bug its own banner
    warns about:

      * `PRESENCE.sessions_for(mid)` is empty -- no live connection anywhere on
        the service. A DEPLOY BOUNCE survivor is protected by the second test,
        not this one: their client pongs within ~2 s of the relay re-attaching,
        so they hold a session again long before anyone's `<DE>` sweeps a room.
      * the record's own timestamp (value 6, epoch seconds -- the field the
        client stamps on every re-send) is older than POL_TM_RECORD_STALE_S
        (default 600). This is what keeps a moment of session churn from
        blinking somebody out -- the fault `tmroom.note_member`'s banner records
        the last socket-keyed presence attempt causing.

    Retiring emits the `PC` delta through `forget_member`, so every client in
    the room watches them leave rather than holding the ghost -- and a mistaken
    retire heals on the member's next event, exactly like a missed one does.
    """
    if tmroom is None or os.environ.get("POL_TM_ROSTER", "1") != "1":
        return
    if os.environ.get("POL_TM_RETIRE_STALE", "1") != "1":
        return
    try:
        grace = float(os.environ.get("POL_TM_RECORD_STALE_S", "600") or 600)
    except ValueError:
        grace = 600.0
    now = time.time()
    for mid, values in list(tmroom.members(chan)):
        if corenames.PRESENCE.sessions_for(mid):
            continue                    # connected somewhere: not ours to judge
        try:
            stamped = float(values[6])
        except (TypeError, ValueError, IndexError):
            stamped = 0.0               # an unstamped record cannot prove youth
        if stamped and now - stamped < grace:
            continue                    # recent: could be a bounce in progress
        if tetramaster is not None:
            tetramaster.release_seats(mid, chan)   # seats first, as ever
        if tmroom.forget_member(mid):
            corenames.log("authserv",
                f"  roster {chan}: member {mid} has NO live session and a "
                f"record {int(now - stamped) if stamped else '?'}s old -- "
                f"retired (the close that should have done this landed while "
                f"no process was listening)")


def _roster_retire_on_close(member_id):
    """The member's connection died without a PART. Retire their room record.

    VERIFIED: THIS IS HOW THE ORIGINAL DID IT, and that is a measurement, not a
    preference. `<PD>` is **event-driven, not periodic** -- measured 2026-08-20
    across a live session, the gaps between one member's own record re-sends run
    2 s, 5 s, three minutes, ninety minutes -- so there is no heartbeat on this
    channel for an expiry to work against, and SE cannot have aged records out
    either. `PART` and class-L `<PC>` are the CLEAN exits; the connection itself
    is the only thing that can cover the dirty ones.

    WARNING: **NOT the reverted socket-presence filter.** That one judged presence from
    current IRC membership on EVERY READ, so a moment's churn blinked people out
    of each other's lists -- `tmroom.note_member`'s banner is right that presence
    belongs to the record. This retires ONCE, on an actual close, which is the
    same shape as the `PART` this module already trusts.

    WARNING: **THE SERVER-RESTART CASE IS EXCLUDED BY CONSTRUCTION -- do not "fix" it
    with a shutdown flag.** This runs from a channel handler's `finally`, and the
    service installs no SIGTERM handler, so a container restart kills the process
    without ever running it. That is the same fact the deliberate-close bit above
    relies on ("it stays up only on the path that never runs a `finally`: being
    killed"). A deploy bounce therefore leaves every record standing, which is
    what we want: the client is still there, we are the ones who went away.
    WARNING: IF A SIGNAL HANDLER IS EVER ADDED TO THIS SERVICE THIS COMMENT BECOMES
    WRONG, and a player sitting still in a room will blank out of everyone's
    list after every deploy.
    """
    if tmroom is None or os.environ.get("POL_TM_ROSTER", "1") != "1":
        return
    if os.environ.get("POL_TM_RETIRE_ON_CLOSE", "1") != "1":
        return
    try:
        # SEATS FIRST, WHILE `room_of` CAN STILL PLACE THEM. Retiring the record
        # first orphans the seat -- that is `_roster_note_departure`'s own
        # hard-won ordering, and the reason it carries a comment about it.
        chan = tmroom.room_of(member_id)
        if not chan:
            return                      # not in a TM room -- nothing to retire
        if tetramaster is not None:
            tetramaster.release_seats(member_id)
        if tmroom.forget_member(member_id):
            corenames.log("authserv", f"  roster {chan}: member {member_id} DROPPED "
                            f"(connection closed without a PART) -- record "
                            f"retired, PC delta queued for everyone still in "
                            f"the room")
    except Exception as exc:
        corenames.log("authserv", f"  roster: close-retire failed ({exc!r}) -- ignored")


def _roster_note_room_peer(target):
    """Record the room peer guid behind a class-L target nick. Never raises."""
    if tmroom is None or tetramaster is None:
        return
    try:
        guid = tetramaster.peer_guid(target)
        if guid is None or tetramaster.peer_class(guid) != tetramaster.ROOM_PEER_CLASS:
            return
        chan = corenames._room_of_member(corenames._session_get("member_id"))
        if chan and tmroom.note_room_peer(chan, guid):
            corenames.log("authserv", f"  roster {chan}: room peer is {guid:#x} (nick "
                            f"{target!r}) -- table peers index against it")
    except Exception as exc:
        corenames.log("authserv", f"  roster: room peer not recorded ({exc!r})")


def _roster_note(payload, tag):
    """Keep a class-L `<DE>`/`<PD>` member record. Never raises -- a roster miss
    must not cost the client its reply."""
    if tmroom is None or os.environ.get("POL_TM_ROSTER", "1") != "1":
        return
    try:
        groups = corenames.polpro.parse(payload)
        if not groups:
            return
        cmd, values = groups[0]
        member_id = corenames._session_get("member_id")
        # WARNING: `<PC>` AND `@Init=/NN=` ARE THE WRONG ID -- retracted 2026-08-18.
        # They carry a 64-BIT value (0xAB12CD56EB0F5932) and the member record
        # wants the LOBBY-BAND self id (0x860FB3E2A2), which is what the working
        # placeholder carried and what `u/account`'s fetch subject is. The shape
        # says why: an id here is a 44-bit guid plus a 6-bit SLOT,
        # so `>> 44` must be a real slot -- 0 for the
        # working value, garbage for the 64-bit one, and the row vanishes.
        if cmd == "PC":
            # ...BUT IT IS STILL THE DEPARTURE, AND DROPPING IT LEFT A GHOST.
            # Measured 2026-08-20, live, two members in #TM0R001: one walked out,
            # the other kept seeing them in the member list for as long as the
            # room stayed open. The frame arrives exactly as it should --
            #     00:25:54  polpro L <- <PC>(0xAB12CD56EB0F5932)
            # -- and `tmroom.forget_member` has been sitting here able to answer
            # it since it was written; its ONLY caller in the tree was its own
            # selftest.
            #
            # The retraction above is about the PAYLOAD's id, and it stands: that
            # 64-bit value cannot build a record. It does not follow that the
            # message is useless, because we do not need it to say WHO -- `<PC>`
            # arrives on the leaver's OWN session, so the session's member_id is
            # the answer, and it is the same id `<DE>` was recorded under.
            #
            # Entering a room sends `<PC>` for the previous one (measured: PC at
            # 00:25:54, `<DE>` for the new room at 00:26:22), so a removal that
            # is immediately followed by a re-add is normal and self-correcting.
            # READ THE ROOM FIRST -- forget_member drops the record, and
            # `room_of` then has nothing to answer from. `members("?")` is worse
            # than useless here: room_id_for falls back to room 1, so the count
            # would silently be some other room's.
            # VERIFIED: AND ITS PAYLOAD IS THE **POL-ID**, WHICH IS WORTH KEEPING.
            # The retraction above says this 64-bit value cannot build a member
            # record, and that stands. It is still a real id with a real reader:
            # Tetra Master's chat roster matches `/NN=` against exactly this
            # number (TM.dll 0x0AFE3, fed from `@Init=/NN=`), so `<PC>` is a
            # second source for it on a session where we missed the `@Init=`.
            # It goes to `note_pol_id`, NOT to `note_guid` -- those are two
            # different ids for the same person and the latter refuses this one.
            if member_id and values:
                try:
                    # polpro hands the value back with its parentheses on --
                    # `<PC>(0xAB12CD56EB0F5932)` parses to `'(0x...)'` -- and
                    # `note_pol_id` takes the wire form as it comes.
                    tmroom.note_pol_id(member_id, values[0])
                except Exception:
                    pass
            chan = tmroom.room_of(member_id) if member_id else None
            if member_id:
                try:
                    tetramaster.event_left_room(member_id)
                except Exception:
                    pass
            if member_id and tmroom.forget_member(member_id):
                corenames.log("authserv",
                    f"  roster: <PC> from member {member_id} -- dropped from "
                    f"{chan or '?'}, "
                    f"{len(tmroom.members(chan)) if chan else 0} left")
            return
        if cmd not in ("DE", "PD") or len(values) < 11:
            return
        # NO ROOM LOOKUP. `<DE>` arrives BEFORE the IRC JOIN (measured: 07:36:25
        # against a JOIN 1.5 s later), so requiring one threw away every room
        # entry. The record carries its own room at value 9.
        _roster_note_guid(member_id)
        # The tournament matchmaker reads the event status from every
        # <DE>/<PD> (listed members only; see tetramaster.note_event_status).
        try:
            tetramaster.note_event_status(member_id, values)
        except Exception as exc:
            corenames.log("authserv", f"  roster: event status not noted ({exc!r})")
        if tmroom.note_member(member_id, values):
            chan = tmroom.room_of(member_id) or "?"
            corenames.log("authserv",
                f"  roster {chan}: {tag.decode('latin1')} <{cmd}> from member "
                f"{member_id} -- {len(tmroom.members(chan))} in the room, "
                f"sequence {tmroom.sequence(chan)}")
            # EVERY ROOM ENTRY RECONCILES THE ROOM. The <DE> precedes the PTL
            # fetch (it arrives even before the IRC JOIN -- see the banner
            # above), so the state is healed before anybody can look at it.
            # Idempotent; publishes only on change; never costs the reply.
            # RECORDS FIRST: the seat sweep keys on members(chan), so a record
            # retired here frees its seats in the same pass.
            if cmd == "DE" and chan != "?":
                try:
                    _roster_retire_stale(chan)
                except Exception as exc:
                    corenames.log("authserv",
                        f"  roster {chan}: stale-record sweep failed ({exc!r})")
                if tetramaster is not None:
                    # ENTERING A ROOM RETIRES A SEAT LEFT IN ANOTHER ONE. A seat
                    # cannot outlive the presence that made it, and a member is
                    # present in exactly one room -- so their <DE> here is proof
                    # any seat they hold elsewhere is stale. reconcile can't see
                    # it (its holder still has a session), so it is swept from the
                    # room CHANGE. See tetramaster.release_seats_elsewhere.
                    try:
                        tetramaster.release_seats_elsewhere(member_id, chan)
                    except Exception as exc:
                        corenames.log("authserv",
                            f"  roster {chan}: seat-elsewhere sweep failed ({exc!r})")
                    try:
                        tetramaster.reconcile_room_tables(chan)
                    except Exception as exc:
                        corenames.log("authserv",
                            f"  roster {chan}: reconcile failed ({exc!r})")
    except Exception as exc:
        corenames.log("authserv", f"  roster: {exc!r} -- ignored, the reply still stands")
