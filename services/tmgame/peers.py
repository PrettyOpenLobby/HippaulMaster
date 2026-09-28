"""Peer nicks and ids: the peer guid fold, table and room classes, and which table a peer nick
names.
"""


#: `/Shm=` IS A PEER ID, NOT A VERSION (2026-08-16). `tm_init_build` (TM.dll rva
#: 0x87F10) writes the parsed `/Shm=` u64 straight into the outgoing @Init
#: record's id field at `+0x10`/`+0x14` (`mov [esp+0x24],esi` at 0x881CF) and
#: then calls the send, 0x82060 -- which REFUSES to transmit a message with no
#: destination: `if ([rec+0x10]|[rec+0x14]) == 0 and rec+0x18 (the nick) == ""
#: and the global at 0x242958 == 0: return 0`, silently, nothing on the wire.
#: Serving sixteen zeros is why `@Init=` has never once appeared in a capture.
#:
#: The id is computable from the peer's NICK, with no lookup and no state:
#:
#:     nick --polnick.polid_for_nick--> POL ID --base-36 over polnick.ALPHA-->
#:     client_guid
#:
#: verified against `accounts.db`: one test account (POL ID EFGH5678, nick
#: UDXS6FWXX) has the stored `client_guid` 0x162E92CDC54, exactly b36(ALPHA).
#: The TM0 peer the client addresses is nick USH6MZJA7 = POL ID `AAAC0001`, a
#: SYNTHETIC service id the client makes up locally -- it is not a member of
#: ours, which is why nothing server-side has ever known about it.
#:
#: WARNING: ONE OPEN VARIABLE. In client memory these ids appear as `client_guid XOR K`
#: (K = `tmroom.CLIENT_KEY_DEFAULT`, constant across both peers -- the
#: client-local guid key). Whether the value we put in
#: `/Shm=` is expected in RAW or XORed form is NOT yet established. We serve RAW,
#: because raw is the form the server can compute for anyone; if the launch shows
#: `@Init=` addressed to a garbage nick instead of the peer, the XOR is needed and
#: that wrong nick hands us K directly. Either outcome is one launch.
#: VERIFIED: THE PEER NICK IS THE TABLE, AND IT IS THE FIELD THAT WAS MISSING.
#:
#: Measured 2026-08-20T00:59-01:00Z with a tester narrating the flow: they
#: reserved TABLE 1 with default settings, saved, then switched to TABLE 2 and
#: reserved that. The wire:
#:
#:   00:59:30  -> UE7QN1N9G   @Save=/Tbl=1/Game=0 ...   <- names table 1
#:   00:59:34  -> UE7QN1N9G   @Tet=/../tl=3 @Tab=/..    <- the custom rules
#:   01:00:37  -> UE7QN1N9N   @Tet=/.. @Tab=/..         <- table 2
#:
#: and the nicks decode (`polnick` + the base-36 fold below) to
#:
#:   UE7QN1N9G  ->  0xD0E4BCBB91     table 1
#:   UE7QN1N9N  ->  0xD0E4BCBB92     table 2
#:   UKXDBA266  ->  0xF0E4BCBB91     the ROOM's roster peer -- the nick every
#:                                   class-L `<DR>` is addressed to
#:
#: So a table peer is `0xD0` over the room's key and the room itself is `0xF0`
#: over the same key, one CLASS NIBBLE apart. `@Save=/Tbl=1` landing on the
#: `...91` peer is the cross-check, and it is why `Tbl` is parsed and compared
#: rather than trusted on its own.
#:
#: VERIFIED: AND IT CLOSES A STANDING UNKNOWN. `responders`'s `_SUBJECT_KEYED_PATHS`
#: note says "`b/g/PTL`'s 0xf0e4bcbb91 is unexplained -- it is NOT the room id the
#: client reports in its own `<DE>`". It is the ROOM PEER'S GUID. Same value,
#: arrived at from the other end.
#:
#: WARNING: THE INDEX FORMULA IS INFERRED FROM ONE ROOM. Two tables in `#TM0R001` fit
#: `index = table_low32 - room_low32 + 1`; a second room would either confirm it
#: or show the base is per-room in a way one room cannot reveal. That is why
#: `table_index_for_peer` returns None rather than a guess when it has no room
#: peer to measure against, and why every resolution logs what it used.
TABLE_PEER_CLASS = 0xD0
ROOM_PEER_CLASS = 0xF0


def peer_guid(peer_nick):
    """The u64 behind a TM0 peer nick, or None. The fold is `_teach_shm`'s, which
    was confirmed live by `@Init` reaching the card shop."""
    if not peer_nick:
        return None
    try:
        import polnick
        nick = (peer_nick.decode("ascii", "ignore")
                if isinstance(peer_nick, bytes) else peer_nick)
        pid = polnick.polid_for_nick(nick)
        if not pid:
            return None
        guid = 0
        for ch in pid:
            guid = guid * 36 + polnick.ALPHA.index(ch)
        return guid & 0xFFFFFFFFFFFFFFFF
    except Exception:
        return None


def peer_class(guid):
    """The class nibble-pair: 0xD0 for a table, 0xF0 for a room."""
    return None if guid is None else (guid >> 32) & 0xFF


def _is_table_class(cls, room_cls, room_no=None):
    """Is `cls` a TABLE peer class? ROOM- and era-invariant.

    WARNING: THE LOW NIBBLE IS THE ROOM NUMBER, NOT A FIXED "1" -- the fourth gate
    era, measured live 2026-08-21T16:24: room 3's table peer arrived class
    **0xD3** and was dropped because the rule wanted low nibble 1. Every
    measured table class fits one shape: high nibble = the re-allocation ERA
    (drifts F->E->D per restart), **low nibble = the ROOM NUMBER**:
        room 1 -> 0xF1 / 0xE1 / 0xD1   (low 1)
        room 3 -> 0xD3                 (low 3)
    and the ROOM peer is that minus one in the low nibble (room 1 -> 0xF0).
    So a table peer's class low nibble equals `room_no`; that is the stable
    fact across every era AND room. The old "low nibble == 1" only worked in
    room 1 by coincidence. Legacy 0xD0 and the room+1 relation are kept as
    fallbacks for environments where `room_no` is unknown.
    """
    if cls is None:
        return False
    if cls == TABLE_PEER_CLASS:
        return True                       # the legacy absolute (0xD0)
    # The ERA is the high nibble and drifts F -> E -> D per re-allocation; a
    # real table/room peer is in that band, which is what separates it from a
    # foreign family (e.g. 0xA2). Extend the band if it ever drifts below 0xD.
    hi = (cls >> 4) & 0x0F
    era_band = hi in (0x0D, 0x0E, 0x0F)
    if room_no is not None and room_no >= 1 and era_band \
            and (cls & 0x0F) == (room_no & 0x0F):
        return True                       # ERA band + low nibble == room number
    if room_no is None and era_band and (cls & 0x0F) == 0x01:
        return True                       # room unknown: the old family-bit
    if room_cls is not None and cls == ((room_cls + 1) & 0xFF):
        return True                       # the 2026-08-20 room+1 relation, kept
    return False


def _exact_table_index(guid, chan):
    """`(index, why)` from the id key, or None if this peer is not one of ours.

    `tmroom.app_id_for_guid` undoes the wire truncation exactly, so this is a
    LOOKUP, not an inference: the reconstructed id either equals a table id we
    published in this room or it does not. Returns None (never a guess) so the
    caller's heuristic still gets its turn -- which matters while `K` is a
    default rather than a value derived at runtime.
    """
    if not chan:
        return None
    try:
        import tmroom
        room_no = tmroom._room_no(chan)
        if not room_no:
            return None
        app = tmroom.app_id_for_guid(guid)
        if app == tmroom.room_id_for(chan):
            return None                   # the ROOM peer, not a table
        for i in range(64):
            if app == tmroom.canonical_table_id(i, room_no):
                return i + 1, ("peer %#x ^ K -> id %#018x = table %d (exact)"
                               % (guid, app, i + 1))
    except Exception:
        return None
    return None


def table_index_for_peer(peer_nick, room_peer_guid, tbl_hint=None, chan=None):
    """`(index, why)` -- which table this peer is, and how we decided.

    `index` is None when we cannot say. `tbl_hint` is `@Save=`'s own `/Tbl=`,
    which is GROUND TRUTH when present: it is used, and any disagreement with
    the computed value is reported rather than quietly preferred either way.
    """
    guid = peer_guid(peer_nick)
    if guid is None:
        return (tbl_hint, "no peer nick; /Tbl= only") if tbl_hint else (None, "no peer nick")
    # VERIFIED: THE EXACT PATH (2026-08-22). A peer nick carries the low 40 bits of
    # `app_id ^ K`, so `guid ^ K` IS the id we published -- no eras, no block
    # bases, no class nibbles. Try that first and fall through to the measured
    # heuristic below when it does not land, so nothing that used to resolve
    # stops resolving. See `tmroom.CLIENT_KEY_DEFAULT` for the derivation.
    exact = _exact_table_index(guid, chan)
    if exact is not None:
        idx, why = exact
        if tbl_hint is not None and tbl_hint != idx:
            return (tbl_hint, "%s but /Tbl= says %d -- TAKING /Tbl="
                    % (why, tbl_hint))
        return idx, why + (" (/Tbl= agrees)" if tbl_hint is not None else "")
    # WARNING: THE CLASS DRIFTS WITH THE ALLOCATION ERA -- the third gate era in two
    # days, each measured live off a dropped reservation:
    #   0xD0            the legacy absolute (earlier environment)
    #   0xF1            2026-08-20T22:48 -- room 0xF0 + 1, the RELATION fix
    #   0xD1            2026-08-21T12:33:09 -- room peer STILL 0xF0, so the
    #                   room+1 relation ALSO broke: the client re-allocates its
    #                   TABLE peers into a new era (high nibble F->E->D, one
    #                   step per re-allocation/restart -- two deploys that day)
    #                   while it keeps ADDRESSING the room by its ORIGINAL
    #                   peer. Same whack-a-mole as the k*0x1000 base drift the
    #                   block below already survives, one field over.
    # What is stable across every measured value is the LOW NIBBLE: rooms are
    # x0, tables x1. `_is_table_class` holds the whole rule; the constants are
    # pinned in `_selftest_peer_class`, including 0xD1 (the live reject).
    cls = peer_class(guid)
    room_cls = peer_class(room_peer_guid) if room_peer_guid is not None else None
    _rn = None
    try:
        cn = chan.decode("latin1") if isinstance(chan, bytes) else (chan or "")
        if cn.startswith("#TM0R"):
            _rn = int(cn[5:])
    except (ValueError, AttributeError):
        _rn = None
    if not _is_table_class(cls, room_cls, _rn):
        return (None, "peer %#x is class %#04x, not a table (low nibble %#x != "
                "room %s, not %#04x%s)"
                % (guid, cls or 0, (cls or 0) & 0x0F, _rn, TABLE_PEER_CLASS,
                   ", not room+1 %#04x" % ((room_cls + 1) & 0xFF)
                   if room_cls is not None else ""))
    if room_peer_guid is None:
        return (tbl_hint,
                "peer %#x is a table but no room peer is known to index it "
                "against%s" % (guid, "; /Tbl= used" if tbl_hint else ""))
    # WARNING: TWO BASES, ONE MEASURED LIVE. The original formula indexed tables from
    # the room peer's own low half (base +0). On 2026-08-20T22:57 a tester
    # clicked TABLE 1 and the peer arrived as 0xF1E4BCAB91 against a fresh room
    # peer 0xF0E4BCBB91 -- the table block sits 0x1000 BELOW the room peer in
    # this allocation era, and the old base computed -4095. Both bases are tried
    # and at most one can land in 1..64 (they are 0x1000 apart), so this cannot
    # mis-index a peer the old scheme handled. Which base matched is in the
    # `why`, so the day a third era appears it names itself.
    low, room_low = guid & 0xFFFFFFFF, room_peer_guid & 0xFFFFFFFF
    # VERIFIED: RESTART-RESILIENT INDEXING (2026-08-21). SEATING MUST SURVIVE AN
    # authsess RESTART -- the server owner's standing requirement, and the bug that
    # broke it: the table-peer block sits a WHOLE number of 0x1000 blocks below
    # room 1's peer, and that offset DRIFTS by another 0x1000 every time the
    # client re-allocates its peers, which is exactly what a restart forces (the
    # client resumes on a NEW allocation while we kept the OLD room peer). The
    # table peers STEP per-table within a block; the block itself moves. Two
    # rooms measured 2026-08-21T00:02: room peers step with the room number
    # (room 1 low ...BB91, room 2 ...BB92) while table 1 is ...AB91 in BOTH, so
    # the block is GLOBAL at room1_low - k*0x1000.
    #
    # WARNING: THE OLD CODE HARD-CODED k = 1 (two bases) AND A THIRD ERA FELL THROUGH:
    # 2026-08-21T04:13, room 3 peer ...BB93, TABLE 2 arrived as ...9B92 (block at
    # -0x2000, k = 2) and dropped as "both out of range" -- every reservation of
    # the night. Hard-coding eras is whack-a-mole and re-breaks on the next
    # restart.
    #
    # THE INDEX DOES NOT DEPEND ON k. Only the position WITHIN the 0x1000 block
    # matters, and that is stable across any number of re-allocations. Normalise
    # room N's peer back to room 1's origin (undo the per-room +(N-1) step) and
    # reduce mod 0x1000: the k*0x1000 drift cancels, so seating survives every
    # restart. `/Tbl=` stays ground truth; an in-block offset past the table
    # count is still rejected as not-a-table-here. Verified vs every measured
    # peer: room1 t1 AB91->1, room1 t3 8B93->3, room3 t2 9B92->2 (the failing one).
    room_no = 0
    try:
        cn = chan.decode("latin1") if isinstance(chan, bytes) else (chan or "")
        if cn.startswith("#TM0R"):
            room_no = int(cn[5:])
    except (ValueError, AttributeError):
        room_no = 0
    base_used = (room_low - (room_no - 1 if room_no >= 1 else 0)) & 0xFFFFFFFF
    offset = (low - base_used) & 0xFFF          # position within the 0x1000 block
    idx = offset + 1
    if not (1 <= idx <= 64):
        return (tbl_hint, "peer %#x in-block offset %d against room %#x (norm "
                          "base %#x) is out of 1..64 -- not a table in this room"
                % (guid, offset, room_peer_guid, base_used))
    if tbl_hint is not None and tbl_hint != idx:
        # BOTH SOURCES SPOKE AND THEY DISAGREE. Say so loudly and take the
        # client's own number -- the formula is the inferred half.
        return (tbl_hint, "peer %#x indexes to %d but /Tbl= says %d -- TAKING "
                          "/Tbl=, the formula is what is inferred here"
                          % (guid, idx, tbl_hint))
    return idx, "peer %#x - base %#x -> table %d%s" % (
        guid, base_used, idx, " (/Tbl= agrees)" if tbl_hint is not None else "")
