"""Zones on the lobby band: occupancy, live player and room counts, the dial host and zone names per
build.
"""
import os
import struct
import time
import tmroom
from . import corenames, templates


#: *** ZONE OCCUPANCY -- WHY JOINING A ZONE NEVER MOVED THE COUNT. ***
#: `b/g/ZL`'s player count has always been "sum the headcounts of the rooms this
#: zone's `b/g/RL%03d` names", which is what `janlobby.zone_players` computes too.
#: That is not wrong, it is INCOMPLETE, and the live report is the proof:
#: entering a zone changed nothing, because a player standing on the ROOM LIST
#: has not joined an IRC channel and so is in no room, in no headcount, and
#: invisible to every count we serve. Zone occupancy is not room occupancy.
#:
#: THE STATE MACHINE IS IN THE FETCH LOG, not inferred. One session, 2026-08-18:
#:
#:     07:48:28  b/g/ZL     the zone-select screen  -> in NO zone
#:     07:48:54  b/g/ZL     a refresh of it
#:     07:48:57  b/g/RL000  ENTERED zone 0
#:     07:49:47  b/g/ZL     backed out to the zone list again
#:     07:49:55  b/g/RL000  back into zone 0
#:     08:03:13  b/g/ZL     out
#:
#: So `b/g/RL%03d` IS the zone-entry event and `b/g/ZL` is the zone-exit event.
#: Both are 3:0 fetches on the LOBBY band, and so is the `b/g/ZL` we have to
#: answer -- observation and consumption are in ONE process (`login`), which is
#: why this needs no published snapshot file the way the room registry does.
#:
#: WARNING: A ZONE'S PLAYERS ARE A UNION, NOT A SUM. Somebody who walked through the
#: room list into a room is in BOTH sets and must be counted once. Rooms are the
#: authority on identity here (they carry `member_id`); a session we cannot name
#: is counted as a room occupant only, which is the pre-existing behaviour.
#:
#: WARNING: AND IT IS LEASED, NOT LATCHED. A client that crashes on the room list sends
#: no exit event, and a latched entry would inflate the zone for the life of the
#: process. `POL_TM_ZONE_LEASE` seconds (0 disables the whole mechanism and
#: restores the room-sum).
_ZONE_OF = {}                       # member_id -> [zone_id, last_seen monotonic]


def _zone_lease():
    return float(os.environ.get("POL_TM_ZONE_LEASE", "1800") or 0)


def _note_zone_presence(zone):
    """`zone` is the id from a `b/g/RL%03d` fetch, or None for `b/g/ZL` (exit)."""
    if _zone_lease() <= 0:
        return
    mid = corenames._session_get("member_id")
    try:
        mid = int(mid or 0)
    except (TypeError, ValueError):
        mid = 0
    if not mid:
        return                      # unnamed session: the room sum still has it
    if zone is None:
        if _ZONE_OF.pop(mid, None) is not None:
            corenames.log("lobby", f"  zone: member {mid} is back at the zone list")
        return
    was = _ZONE_OF.get(mid)
    _ZONE_OF[mid] = [int(zone), time.monotonic()]
    if not was or was[0] != int(zone):
        corenames.log("lobby", f"  zone: member {mid} entered zone {zone}")


def _zone_occupants(zone):
    """Members whose last zone event put them in `zone` and whose lease is live."""
    lease = _zone_lease()
    if lease <= 0:
        return set()
    now = time.monotonic()
    for mid in [m for m, (_z, t) in _ZONE_OF.items() if now - t > lease]:
        _ZONE_OF.pop(mid, None)
    return {m for m, (z, _t) in _ZONE_OF.items() if z == int(zone)}


def _lobby_counts_live(path, data, subject=0):
    """Live player/room counts in `b/g/ZL` and `b/g/RL%03d` for TETRA MASTER.

    The zone and room screens have always shown placeholders because both blobs
    are authored files that nobody updated. `janlobby` builds Jan's from the room
    registry already, but it is gated to Jan's content id and its topology is not
    TM's -- see the banner on `_jan_lobby_blob`. This does the counts only, by
    PATCHING the authored blob, so every other field stays as the TM track
    authored it. Field map and the cross-check that confirms it on TM: see
    `tmroom`'s "THE ZONE AND ROOM LISTS" banner.
    """
    if tmroom is None or os.environ.get("POL_TM_LOBBY_COUNTS", "1") != "1":
        return data
    if path != "b/g/ZL" and not (path.startswith("b/g/RL")
                                 and path[6:].isdigit()):
        return data
    try:
        live = corenames._live_rooms() or {}
        room_members = {
            c: {int(r.get("member_id") or 0)
                for r in ((e or {}).get("who") or []) if r.get("member_id")}
            for c, e in live.items()}
        # COUNT THE SAME PEOPLE `build_ptl` DRAWS, OR THE TWO SCREENS DISAGREE.
        # This used to take the registry's own `members` field, which counts ROWS
        # -- and the registry holds a duplicate row after a reconnect, measured
        # 2026-08-20 (`members=3` for two people, and the member pane drew
        # one tester twice until `build_ptl` was made immune to it). The
        # snapshot de-duplicates on member id and this did not, so the room list
        # could say 3 while Table Members showed 2, from one registry.
        #
        # The duplicate is its own bug; a COUNTER should be immune to a repeated
        # input regardless -- the same argument `build_ptl`'s "`have` MUST GROW
        # AS WE GO" banner makes, one screen over.
        #
        # WARNING: FALL BACK, DO NOT ZERO. A channel whose rows carry no member id yet
        # is not empty -- ids bind slightly after the JOIN -- so it keeps the
        # registry's count rather than dropping to 0 and flickering.
        head = {c: (len(room_members.get(c) or ())
                    or int((e or {}).get("members", 0)))
                for c, e in live.items()}
        if path.startswith("b/g/RL"):
            # THE ROOM-LIST FETCH IS THE ZONE-ENTRY EVENT. See `_ZONE_OF`.
            _note_zone_presence(int(path[6:]))
            # THE TABLE COLUMNS, from the same rows `build_ptl` serves that room
            # -- one source, so the room list and the table screen cannot
            # disagree. See `tmroom.RL_F_TABLES`: those offsets are SE's own
            # names via janlobby and are NOT confirmed on TM, so this is also the
            # measurement that settles them. The screen shows 15/16 today and the
            # authored bytes at both offsets are 1/0 and 0x00, so 15/16 comes
            # from neither -- if the column does not move, the read site has to
            # come out of TM.dll before anything else is written there.
            # WARNING: DEFAULT OFF AGAIN, 2026-08-20, SAME DAY IT WENT IN. Writing
            # these two offsets made the room list "horrifically off" on screen
            # (live report). That is a RESULT, not a failure: it proves the client
            # does read at least one of them, so `0x18`/`0x86` are live fields --
            # and it proves they do not mean what janlobby's names say they mean
            # for THIS title, because the authored bytes there were 1/0 and 0x00
            # while the screen read 15/16.
            #
            # Standing rule: match the original, do not ship the stopgap. Until the read
            # site is found in TM.dll, the authored placeholder is a KNOWN wrong
            # number and this was an UNKNOWN wrong number, which is worse -- it
            # cannot be told from a protocol fault by anyone looking at the
            # screen. `POL_TM_ROOM_TABLE_COUNTS=1` re-enables it for whoever goes
            # looking, and the log line prints exactly what it wrote.
            tbl = {}
            if os.environ.get("POL_TM_ROOM_TABLE_COUNTS", "0") == "1":
                base = templates._tm_template_blob("b/g/PTL")
                for _i, chan in tmroom._room_channels(data):
                    counts = tmroom.room_table_counts(chan, base) if base else None
                    if counts:
                        tbl[chan] = counts
            out = tmroom.patch_room_list(data, head, tables_by_room=tbl)
            if out != data:
                corenames.log("lobby", f"  {path}: live headcounts patched in "
                             f"({sum(head.values())} player(s) across "
                             f"{len([c for c in head if head[c]])} room(s)); "
                             f"table columns -> "
                             f"{sorted(set(tbl.values())) or 'not written'} "
                             f"(total, open) for {len(tbl)} room(s)")
            return out
        # ...AND THE ZONE LIST IS THE EXIT EVENT: this screen is where you stand
        # when you are in no zone at all.
        _note_zone_presence(None)
        # For the zone list we need each zone's OWN room list to know which
        # channels belong to it.
        #
        # WARNING: READ THEM THE WAY THE CLIENT IS ACTUALLY SERVED, WHICH IS NOT THE
        # SAME AS READING THE STORED FILE. This loop used to `open()` the stored
        # resource only, and on prod `data/resources/` holds NO `b_g_RL*.bin` at
        # all -- the room lists are server-authored fixtures that ship in
        # `services/tmdata/`. So every zone missed, `rls` came out EMPTY,
        # `patch_zone_list` found no room list for any row and patched nothing,
        # and the zone screen showed its authored placeholders for ever.
        #
        # WARNING: AND IT FAILED SILENTLY, which is why it survived: the log line is
        # gated on `out != data`, so "patched nothing" and "nothing needed
        # patching" printed the same thing -- nothing. Measured 2026-08-20: five
        # `b/g/ZL` serves in one session, not one count line, while `b/g/RL000`
        # on the very same fetches logged "2 player(s) across 1 room(s)".
        #
        # This is the FOURTH time a server-authored fixture has been read from a
        # dev-only stored path (`b/g/ZL`, `b/g/PTL`, `U/g/TM0_RANKLIST`, now the
        # zone list's view of `b/g/RL`) -- see `_tm_template_blob`'s banner:
        # server-authored fixtures must ship. Stored copy first so a
        # per-member override still wins, shipped fixture behind it.
        rls = {}
        for zid in range(0, 32):
            rl_path = "b/g/RL%03d" % zid
            blob = None
            try:
                with open(corenames._resource_read_file(rl_path, subject), "rb") as f:
                    blob = f.read()
            except OSError:
                blob = templates._tm_template_blob(rl_path)
            if blob:
                rls[zid] = blob
        zone_members = {}
        for zid in rls:
            who = _zone_occupants(zid)
            if who:
                zone_members[zid] = who
        out = tmroom.patch_zone_list(data, head, rls,
                                     zone_members=zone_members,
                                     room_members=room_members)
        standing = sum(len(v) for v in zone_members.values())
        if not rls:
            # NEVER SILENT AGAIN. "Found no room list" and "the counts were
            # already right" used to print identically, and that is the whole
            # reason the bug above went unnoticed through five serves.
            corenames.log("lobby", f"  b/g/ZL: WARNING: NO room list resolved for any zone -- "
                         f"neither a stored b/g/RL### nor a shipped fixture. "
                         f"Every zone keeps its AUTHORED placeholder count.")
        elif out == data:
            corenames.log("lobby", f"  b/g/ZL: live counts already match the authored "
                         f"blob ({len(rls)} room list(s) resolved, "
                         f"{sum(head.values())} player(s) in rooms) -- nothing "
                         f"to patch")
        else:
            corenames.log("lobby", f"  b/g/ZL: live counts patched in from "
                         f"{len(rls)} room list(s)"
                         + (f", including {standing} player(s) standing in a "
                            f"zone but in no room" if standing else ""))
        return out
    except Exception as exc:
        corenames.log("lobby", f"  {path}: live count patch failed ({exc!r}) -- serving "
                     f"the authored blob")
        return data


def _client_host():
    """The address to write into a zone row for THE CLIENT BEING SERVED.

    A core that exports `_advertise_configured` also makes `_self_ip()` answer
    per client: a console on the LAN gets the LAN address it reached us on, not
    the one box-wide POL_ADVERTISE, which it may not be able to route to. An
    older core has neither, and there `_self_ip()` ignores POL_ADVERTISE
    altogether, so the environment has to come first exactly as before."""
    if corenames._advertise_configured is not None:
        return corenames._self_ip() if corenames._advertise_configured() else ""
    return (os.environ.get("POL_ADVERTISE") or "").strip()


def _zone_host_live(path, data):
    """`b/g/ZL`'s dial target is THIS server, whatever the fixture says.

    Each zone row carries the host the client dials for the zone's game
    connection (`tmroom.ZL_F_HOST`, +0x2C), and the authored fixture carries
    the box it was CAPTURED on -- dev's LAN address. Served from prod, that
    field put both testers in SYN_SENT to 127.0.0.1:51241 and errored
    TRM-8196-37130 (measured live 2026-08-19). Deliberately NOT behind
    POL_TM_LOBBY_COUNTS: a wrong count is cosmetic, a wrong address is a dead
    rooms list. POL_TM_ZONE_HOST overrides the value (a hostname is legal when
    the client can resolve it); the default is the address we advertise
    everywhere else, which a hosts-file-less client can always dial.
    """
    if tmroom is None or path != "b/g/ZL":
        return data
    # \U0001f534 POL_ADVERTISE BEFORE _self_ip(). The docstring above has always
    # said "the address we advertise everywhere else", and this knob is
    # documented as "override the zone host (else POL_ADVERTISE)" -- but the code
    # fell straight through to `_self_ip()`, which is `_SELF_IP[0] or 127.0.0.1`
    # and is UNSET inside the container. So every zone row went out dialling
    # **127.0.0.1**: the PC clicked a zone, tried to connect to itself, failed
    # and dropped back to the title screen (observed live, 2026-09-09). Exactly the
    # failure this function was written to prevent, one address further along.
    host = (os.environ.get("POL_TM_ZONE_HOST")
            or _client_host() or corenames._self_ip())
    try:
        out, replaced = tmroom.patch_zone_hosts(data, host)
    except ValueError as exc:
        corenames.log("lobby", f"  b/g/ZL: zone host NOT patched ({exc}) -- serving as "
                     f"authored")
        return data
    if replaced:
        corenames.log("lobby", f"  b/g/ZL: zone dial target -> {host} "
                     f"(fixture said {', '.join(replaced)})")
    return out


#: `b/g/ZL` record +0x2C is a HOST, not a channel name -- rewrite it at serve
#: time so it can never go stale.
#:
#: The shipped file had **127.0.0.1** baked into it, an address on a LAN the
#: console is not on (it reaches us over the tailnet), so the string was
#: unreachable for every client that has ever read it.
#:
#: It is a host because of what consumes it: accessor 0x003a7320 hands the field
#: to TMaster.pex **0x00299220**, which is structurally identical to the COM
#: screen's opener at 0x00298f70 -- same shape, same 'IRC Session start already.'
#: string, same "log and return 1000" arm -- differing only in which state global
#: it guards (0x0043CB58 here, 0x0043CB60 there). Both take a host string and
#: open a session; see `_TM0CQL_INIT` for the other one, where the same argument
#: is proved to be text by the client's own first-byte digit test.
#:
#: Layout from tools/tmzonelist.py, measured off five leaf accessors: 0x48
#: header + 32 records * 64, count at +0x40, host at record +0x2C, 16 bytes.
#: POL_TM_ZONE_HOST overrides; unset falls back to POL_ADVERTISE; neither set
#: leaves the file's own bytes alone.
def _zl_with_live_host(data):
    host = (os.environ.get("POL_TM_ZONE_HOST") or _client_host()).strip()
    if not host or len(data) < 0x48:
        return data
    HDR, REC, COUNT_OFF, F_CHANNEL, F_BYTE = 0x48, 64, 0x40, 0x2C, 0x3C
    width = F_BYTE - F_CHANNEL
    enc = host.encode("ascii", "replace")[:width - 1].ljust(width, b"\x00")
    buf = bytearray(data)
    try:
        n = struct.unpack_from("<I", buf, COUNT_OFF)[0]
    except struct.error:
        return data
    for i in range(min(n, (len(buf) - HDR) // REC)):
        o = HDR + i * REC + F_CHANNEL
        if buf[o:o + width] != enc:
            buf[o:o + width] = enc
    return bytes(buf)


#: \U0001f534\U0001f534 THE BUILD DISCRIMINATOR IS NOT TRUSTWORTHY FOR PAYLOAD SHAPE.
#:
#: `_peer_is_ps2()` reads `magic[+0x09]` from the lobby hello, and it was
#: introduced for SEND PACING -- `_lobby_ps2_send`, explicitly "payload-neutral,
#: identical bytes, only the send cadence changes". A false positive there costs
#: nothing, so it was never validated as a build test. Reusing it to choose a
#: RECORD LAYOUT is a different bar, and it does not clear it.
#:
#: MEASURED 2026-09-09 on prod: **every** hello in the log reports
#: `magic[+0x09]=0x00 (PS2)` -- 23 of 23 -- including the ones from the PC
#: Tetra Master that was on screen at the time, and `0xfa` (the value the comment
#: at the parse site calls "PC Viewer") has NEVER been observed. So the PC is
#: classified PS2 and gets the console's layout: live testing showed
#: "maids' Dreamworld" on the PC even AFTER the zone file was restored, because
#: the transform fired on the PC's own fetch (lobby.log 03:44:22).
#:
#: \u26a0 `_rkdata_for_build` (b/g/TM0RkData) rides the SAME gate and is still
#: default-on. It was proven on the console; it has NOT been checked on the PC,
#: and if the gate misfires there too the PC is being served the console's
#: 24-byte ranking header. That is the next thing to verify.
#:
#: Until there is a signal that has actually been shown to separate the builds
#: (the login NICK's client field is the documented one --
#: `accounts.get_client_token` / `nick_client_sig`, but it lives on the auth band
#: and the resource fetch is served by the `login` container), the two NAME
#: transforms below default OFF. That serves the PC layout -- which is what the
#: stored files now hold -- to everyone. The console gets a cosmetic prefix back
#: ("AAAMermaids' Dreamworld", "AFreewheeler Room 1"); the PC gets working zone
#: and room lists. Set POL_TM_ZL_NAME_PS2=1 / POL_TM_RL_NAME_PS2=1 to force the
#: console layout once the gate is sound.
#:
#: `b/g/RL%03d` room NAME: the PS2 reads it one byte EARLIER than the PC does.
#:
#: Reported from a screenshot: "still got the A in front of room names". Our
#: file is not corrupt -- it holds a clean "Freewheeler Room 1" at +0x2A -- and
#: the console renders "AFreewheeler Room 1". The only way to get exactly that
#: is to read the string starting at **+0x29**, so on this build the name field
#: begins there and the byte we were writing at +0x29 is its FIRST CHARACTER.
#:
#: +0x29 was set to 'A' (0x41) deliberately, as `tools/tmroomlist.py --set-vscom`,
#: on the strength of `0x45D78 cmp dword [0x52454FC], 0x41` -- a VS. COM gate
#: measured in **TM.dll, the PC build**. That reading may well be right there. It
#: is not right here, and this is the same class of mistake as answering the PS2
#: shop with `@EQuit`: a PC-derived offset applied to a build that lays the
#: record out differently. `tmroomlist.py`'s own docstring already warned that
#: Janhourou reads this record with a different map in which +0x29 is a name
#: byte -- the PS2's TM map turns out to agree with Janhourou here, not the PC.
#:
#: Shifting the name down one byte fixes the display AND retires the 0x41, which
#: on this build was never a gate -- which may matter, because VS. COM is exactly
#: what is failing.
#:
#: Guarded three ways: only `b/g/RL*`, only records whose +0xB8 channel starts
#: `#TM0` (RL001..004 in the same directory are JANHOUROU's and have their own
#: map -- writing here once turned "Test Room 1-1" into "TAst Room 1-1"), and
#: only when POL_TM_RL_NAME_PS2 is on (default 1; set 0 for the PC layout).
def _rl_name_ps2(data):
    # \U0001f534 PER-BUILD, not unconditional. This shifts the room name one byte
    # EARLIER because the console reads it at +0x29 -- but the PC reads it at
    # +0x2A, and +0x29 is where the PC's 0x41 sits. Applied to everyone it does
    # to room names exactly what `7b278b01` did to zone names: fixes the console
    # and eats the PC's first character. That regression was reported on the zone
    # list 2026-09-09 ("maids' Dreamworld"); this is the same bug one field over,
    # fixed before it was reported rather than after.
    if not corenames._peer_is_ps2():
        return data
    if os.environ.get("POL_TM_RL_NAME_PS2", "0") != "1":
        return data
    HDR, REC, COUNT_OFF = 0x48, 200, 0x40
    F_VSCOM, F_NAME, F_CHAN, NAME_LEN = 0x29, 0x2A, 0xB8, 32
    if len(data) < HDR + REC:
        return data
    buf = bytearray(data)
    try:
        n = struct.unpack_from("<I", buf, COUNT_OFF)[0]
    except struct.error:
        return data
    n = min(n, (len(buf) - HDR) // REC)
    moved = 0
    for i in range(n):
        e = HDR + i * REC
        if bytes(buf[e + F_CHAN:e + F_CHAN + 4]) != b"#TM0":
            return data                      # not Tetra Master's -- hands off
        name = bytes(buf[e + F_NAME:e + F_NAME + NAME_LEN])
        if not name.split(b"\x00")[0]:
            continue
        buf[e + F_VSCOM:e + F_VSCOM + NAME_LEN] = name
        buf[e + F_VSCOM + NAME_LEN] = 0      # re-terminate the shifted field
        moved += 1
    return bytes(buf) if moved else data


#: `b/g/ZL` zone NAME: the PC eats FIVE leading bytes, the PS2 eats TWO.
#:
#: WARNING: THIS IS A REGRESSION I CAUSED AND THEN HAD TO UNDO. `7b278b01`
#: "dropped the stray name prefix" by EDITING THE STORED FILE -- turning
#: `EN` `AAA` `Mermaids' Dreamworld` into `EN` `Mermaids' Dreamworld`. That fixed
#: the console, which had been rendering `AAAMermaids' Dreamworld`, and it broke
#: the PC, which then rendered `maids' Dreamworld`: reported 2026-09-09 as
#: "zone names are weird now, cut off with a odd icon".
#:
#: The `AAA` was never stray. The record is
#:
#:     +0x0C  "EN"    language
#:     +0x0E  "AAA"   THREE bytes the PC consumes and the PS2 does not
#:     +0x11  name    (PC reads here; the PS2 reads from +0x0E)
#:     +0x2C  host    rewritten live by `_zone_host_live`
#:
#: so the two builds start the string three bytes apart. A file edit cannot serve
#: both -- it picks for everyone, which is the same trap `POL_TM_SHOPQUIT_PS2`
#: documents. The stored file is back to the shape it shipped in and the CONSOLE
#: gets a transform instead, keyed on the same `_peer_is_ps2()` the rankings
#: layout already uses.
#:
#: Only `b/g/ZL`, only records whose +0x0E is literally `AAA` (so it is
#: idempotent and a no-op if the file is ever reshaped), and it stops at +0x2C so
#: it can never walk into the host field. `POL_TM_ZL_NAME_PS2=0` disables.
def _zl_name_for_build(path, data):
    if path != "b/g/ZL" or not corenames._peer_is_ps2():
        return data
    if os.environ.get("POL_TM_ZL_NAME_PS2", "0") != "1":
        return data
    HDR, REC, COUNT_OFF = 0x48, 64, 0x40
    # The pad is now three SPACES, not "AAA" (see the zone file); both are
    # accepted so this still works if either shape is ever restored.
    F_PAD, F_NAME, F_HOST = 0x0E, 0x11, 0x2C
    PADS = (b"AAA", b"   ")
    if len(data) < HDR + REC:
        return data
    buf = bytearray(data)
    try:
        n = struct.unpack_from("<I", buf, COUNT_OFF)[0]
    except struct.error:
        return data
    n = min(n, (len(buf) - HDR) // REC)
    moved = 0
    for i in range(n):
        e = HDR + i * REC
        if bytes(buf[e + F_PAD:e + F_NAME]) not in PADS:
            continue
        buf[e + F_PAD:e + F_HOST] = (bytes(buf[e + F_NAME:e + F_HOST])
                                     + b"\x00" * 3)
        moved += 1
    if moved:
        corenames.log("lobby", f"  3:0 {path!r}: PS2 layout -- name shifted 3B earlier in "
                     f"{moved} zone record(s) (the console reads it at +0x0E, "
                     f"the PC at +0x11; POL_TM_ZL_NAME_PS2=0 disables)")
    return bytes(buf)


#: THE TOURNAMENT DOOR IS A ZONE NAME. Each `b/g/ZL`
#: name is `EN` + three letters + the display name, and the client stores each
#: letter minus 'A' in the zone struct: the fifth byte lands at +0x32
#: (TMaster.pex 20040908 0x437FD0) and zone select branches on it (0x2DA7B0):
#: 1 builds the Event List (cmd 0x0300032A) instead of the room list. Every zone
#: we author is `ENAAA...`, which is why no event screen ever opened.
#:
#: This appends ONE `ENAAB<name>` zone, cloned from record 0 so the dial host and
#: every unmeasured byte stay as served, for the members in
#: POL_TM_EVENT_ZONE_MEMBERS only (comma list, or `*`). Empty = off, the default.
#: Its zone id is always its own slot (see below), so its room list is
#: `b/g/RL%03d` of that slot.
#: A console that takes the door logs `3:0 'b/g/TM0EventList'` in lobby.log.
_ZL_HDR, _ZL_REC, _ZL_COUNT_OFF, _ZL_MAX = 0x48, 0x40, 0x40, 32
_ZL_F_NAME, _ZL_F_HOST, _ZL_F_ID = 0x0C, 0x2C, 0x3C
