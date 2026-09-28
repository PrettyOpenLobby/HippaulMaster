"""b/g/PTL, the room's member and table list, rebuilt from the live roster."""
import os
import struct
import tmroom
from . import corenames, events


#: *** A CLIENT'S BASE IS NAMED BY THE SEQUENCE IT ECHOES, NOT BY A DICT. ***
#:
#: WARNING: RETRACTION, 2026-08-20. What stood here was `_PTL_LIVE_BASE`, an in-process
#: `member_id -> chan` map of everyone we had served a LIVE-ROSTER `b/g/PTL` to,
#: and `_roster_delta_reply` refused to send deltas to anyone missing from it.
#: **It could never be true.** `b/g/PTL` is served by the `login` container
#: (`command: directory,lobby,world,mail`) and `<DR>` is answered by `authsess`
#: (`command: authserv`) -- two processes, two address spaces. The map was
#: written in one and read in the other, so the read side saw an empty dict for
#: every member, for ever. Measured on prod the day it shipped:
#:
#:     authserv.log   "-> N delta(s)"          0
#:                    "is CURRENT -- silent"   0
#:                    "NO live roster"        78   (throttled once per member)
#:                    `<DR>` seen         17,547
#:     lobby.log      live b/g/PTL builds    20-60 PER MINUTE, all session
#:
#: So every one of those 17,547 polls was answered `<DO>` = reload, which is the
#: b/g/PTL re-fetch storm the delta path existed to kill, running at full rate again. And
#: the diagnosis that came with it -- "the clients never re-fetch, so a member
#: whose base was built empty stays broken" -- is falsified by the same two logs:
#: they re-fetch every 3 s and the blob they get is live and current.
#:
#: THE STREAM ALREADY WORKED, and the previous night's log is the proof:
#:
#:     00:20:25  <DR>(0) -> 4 delta(s) ['PD','PD','PD','PD'] up to sequence 4
#:     00:20:29  <DR>(4) is CURRENT -- silent
#:
#: A client sitting at 0 was handed the whole chain and came back current. That
#: is a TEMPLATE HOLDER HEALING ITSELF: `tmroom.deltas_after` replays every `PD`
#: from `have + 1`, and replaying from 0 rebuilds the entire member list. There
#: is nothing to gate.
#:
#: WHAT THE REAL RACE WAS (and it is one -- the measurement was right, only the
#: cure was wrong). The shipped fixture carries `+0x40 = 1`:
#:
#:   Lex          00:44:59  <DE>            -> recorded, sequence 1
#:                00:45:04  fetch b/g/PTL   -> live, 1 member, sequence 1
#:   tester B     00:46:01  fetch b/g/PTL   -> room_of(6) EMPTY: template served,
#:                                             and the template SAYS SEQUENCE 1
#:                00:46:02  <DE>            -> recorded, sequence 2
#:
#: tester B then polled `<DR>(1)` -- a truthful echo of a blob that contains
#: nobody -- and 1 was, by coincidence, the room's real sequence at the moment
#: Lex arrived. So we served it only delta 2 (its own arrival) and called it
#: current. The collision is the bug: **an authored constant occupying a live
#: sequence number**. `_ptl_unknown_base` removes it by stamping 0 -- "I hold
#: nothing" -- on any blob we serve without placing its fetcher, which is a
#: sequence no room is ever at once anybody is in it (`_roster_sync_presence`
#: bumps on arrival), and which the replay path already handles. Fixed in the
#: DATA WE SERVE, not in a fixture, so a stale stored per-member copy is covered
#: too.
#:
#: WARNING: DO NOT REINTRODUCE A CROSS-BAND MEMORY. Anything the `<DR>` answer needs to
#: know about a fetch must go through `tmroom`'s published file, which is what
#: that module's `_OWNER`/`_publish`/`_read_file` dance is FOR. A plain dict at
#: module scope in this file is invisible to the other container -- that is the
#: mistake above, and `_room_of_member`'s docstring warned about it already.
_PTL_TEMPLATE_SERIAL = 0


def _ptl_unknown_base(data):
    """`data` with `+0x40` forced to 0 -- "this blob describes no known room".

    Serving the fixture's own `+0x40` is what let an authored constant collide
    with a live sequence; see the banner above. 0 is safe and self-healing: the
    client echoes it on `<DR>`, `deltas_after` replays the room's whole log, and
    the list rebuilds without a fetch.
    """
    if tmroom is None or len(data) < tmroom.SERIAL_OFF + 4:
        return data
    out = bytearray(data)
    struct.pack_into("<I", out, tmroom.SERIAL_OFF, _PTL_TEMPLATE_SERIAL)
    # WARNING: AND THE TABLE IDS MUST STILL ROUND-TRIP THE ID KEY. The fixture's
    # pre-fold ids (`0x0021...`) are exactly the ids a nick cannot carry, and
    # this path used to serve them raw -- which made the FIRST VS. COM attempt
    # of every launch fail ("could not play against the computer") while the
    # retry, on a room-folded refetch, worked. See `tmroom.fold_unknown_tables`.
    try:
        tmroom.fold_unknown_tables(out)
    except Exception:
        pass
    return bytes(out)


def _ptl_with_live_roster(path, data):
    """`b/g/PTL`'s member half, rebuilt from who is ACTUALLY in the room.

    The stored file stays the source of the TABLE half -- tables are genuinely
    server-defined content -- but its member block is a snapshot of a room, and a
    snapshot of a room has to be GENERATED. `+0x40` is stamped with the room's
    sequence because `cp__002fb560` reads it as exactly that, so the number
    the client echoes back on `<DR>` finally means something.

    Falls through to the stored bytes whenever we cannot say which room the
    fetcher is in -- the pre-roster behaviour, and the right answer for a client
    we hold no records for -- but with `+0x40` blanked by `_ptl_unknown_base`,
    because a blob that describes no room must not claim a live sequence. That
    one byte-field is the difference between a second player who heals on their
    next poll and one who is told they are up to date about nobody.
    """
    if tmroom is None or path != "b/g/PTL" or len(data) < tmroom.TOTAL:
        return data
    if os.environ.get("POL_TM_ROSTER", "1") != "1":
        return data
    try:
        member = corenames._session_get("member_id")
        if not member:
            # No session, no room, and NOT a failure. Without this the `int()`
            # inside `room_of` throws and the handler logs "roster build failed",
            # which reads like a bug in the roster on a path where there is
            # simply nobody to look up. A log line that cannot tell those two
            # apart is the thing this project keeps losing hours to.
            return _ptl_unknown_base(data)
        # The RECORD says which room, not the IRC registry -- a client blipping
        # its auth session used to blink everyone out of everyone else's list.
        chan = tmroom.room_of(member) or corenames._room_of_member(member)
        if not chan:
            corenames.log("lobby", f"  b/g/PTL: member {member} is in NO room we know yet "
                         f"(no <DE>, not in the registry) -- serving the blob "
                         f"with +0x40 = 0, so its first <DR> replays the whole "
                         f"log instead of claiming to be current")
            return _ptl_unknown_base(data)
        # WHO IS PRESENT comes from the room registry, not from who happens to
        # have sent a record -- see `tmroom.synth_member`. Measured 2026-08-18:
        # rooms-live.json held two people and tm-roster.json one, so the second
        # player was invisible in Table Members.
        # Anyone the IRC registry knows but who has not sent a record yet still
        # gets a row, via `synth_member`.
        who = ((corenames._live_rooms() or {}).get(chan) or {}).get("who") or []
        present = [(row.get("member_id"), row.get("name") or row.get("nick"))
                   for row in who]
        built = tmroom.build_ptl(chan, data, present=present or None,
                                 self_member=member)
        if built is data:
            # `build_ptl` bailed: this room has no records and no live tables, so
            # what we hold is still the authored fixture and its `+0x40` is an
            # authored constant. Same treatment as "no room at all".
            return _ptl_unknown_base(data)
        built = _ptl_event_hosts(chan, built)
        n = struct.unpack_from("<i", built, tmroom.MEMBER_COUNT_OFF)[0]
        t = struct.unpack_from("<i", built, tmroom.TABLE_COUNT_OFF)[0]
        corenames.log("lobby", f"  b/g/PTL: built from the LIVE roster of {chan} FOR "
                     f"MEMBER {member} -- {n} member(s) and {t} table(s) IN THE "
                     f"BLOB, sequence {tmroom.sequence(chan)}")
        # Conclusive line for the self-guard fix: what value 0 we served for the
        # fetcher's OWN row, next to the id the client's self-guard holds. If the
        # model is right these match (readself.py's TM self-id == this value).
        if os.environ.get("POL_TM_SELF_ROW_ID", "0") == "1":
            corenames.log("lobby", f"  b/g/PTL: self row for member {member} served value 0 "
                         f"= {tmroom.self_row_value0(member)} (client self-slot; "
                         f"readself.py's TM self-id should equal this)")
        return built
    except Exception as exc:
        # Blank the serial here too: we do not know what this blob describes, and
        # "I hold nothing" is the only claim that cannot mislead the delta path.
        corenames.log("lobby", f"  b/g/PTL: roster build failed ({exc!r}) -- serving the "
                     f"stored blob with +0x40 = 0")
        return _ptl_unknown_base(data)


#: MORE EVENT HOSTS, FEWER BOUNCES. The event join picks an 'E' table row at
#: random with trunc((N+1) * rand() / 32768) -- an index 0..N, so with N 'E'
#: rows it misses 1 time in N+1 and silently bounces the player back to the
#: Event List (PC 0x76258). With the fixture's one
#: host that is 50%. In an event room, for listed members, table rows from
#: `_PTL_EVENT_HOST_FROM` on become copies of the host row (same id, so every
#: pick reaches the same @EInitReq handler). Rows before it stay tables: the
#: matchmaker seats a pair at #TM0T001.
_PTL_EVENT_HOST_FROM = 6    # table slot 6 = #TM0T005 in the fixture


def _ptl_event_hosts(chan, blob):
    if not events._tm_event_test_member():
        return blob
    try:
        room = tmroom.room_id_for(chan)
        zid = events._EVENT_ZONE_ID.get(str(corenames._session_get("member_id") or ""))
        if zid is None or (room & 0xFFFF) != int(
                os.environ.get("POL_TM_EVENT_ROOM_ID", "81"), 0):
            return blob
        n = struct.unpack_from("<i", blob, tmroom.TABLE_COUNT_OFF)[0]
        rows = [blob[tmroom.TABLE_OFF + i * tmroom.TABLE_REC:
                     tmroom.TABLE_OFF + (i + 1) * tmroom.TABLE_REC] for i in range(n)]
        host = next((r for r in rows if tmroom.decode_table(r)[6][:1] == "E"), None)
        if host is None:
            return blob
        buf = bytearray(blob)
        swapped = 0
        for i in range(_PTL_EVENT_HOST_FROM, n):
            o = tmroom.TABLE_OFF + i * tmroom.TABLE_REC
            if o + tmroom.TABLE_REC > len(buf):
                break
            buf[o:o + tmroom.TABLE_REC] = host
            swapped += 1
        if swapped:
            corenames.log("lobby", f"  b/g/PTL: {chan}: {swapped} extra event host row(s) "
                         f"-> {swapped + 1} 'E' rows, the join now misses 1 in "
                         f"{swapped + 2} instead of 1 in 2")
        return bytes(buf)
    except Exception as exc:
        corenames.log("lobby", f"  b/g/PTL: event host rows not added ({exc!r})")
        return blob
