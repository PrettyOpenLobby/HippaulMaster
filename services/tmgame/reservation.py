"""The reservation scene: @GameQA/@GameEA answers and refusals, and the @GameML member and
reservation lists.
"""
import os
import re
import time
from . import boardrules, common, matchmaking, peers, protocol, pushqueue, seating, tablerow


def _gameqa_enabled():
    return os.environ.get("POL_TM_GAMEQA", "1") != "0"


def _gameqa_qt():
    """`/QT=`, which `0x841E0` RETURNS and whose caller reads as: > 0 success,
    0 -> Lobby.BIN 337, < 0 -> an error message. 1 is the least-committal value
    above the threshold. Tunable because the number may well mean something (a
    seat index, a table state) rather than a plain yes."""
    try:
        return int(os.environ.get("POL_TM_GAMEQA_QT", "1"), 0)
    except ValueError:
        return 1


def _gameqt_enabled():
    return os.environ.get("POL_TM_GAMEQT", "1") != "0"


def _gameea_enabled():
    return os.environ.get("POL_TM_GAMEEA", "1") != "0"


def _gameea_en():
    """`/EN=`, the reservation verdict. > 0 advances the scene, 0 becomes the
    client's own -99 ("Timed out while updating table data"), < 0 is an error
    code. 1 is the value TM.dll `0x90114` singles out; see `MSG_GAMEEA`."""
    try:
        return int(os.environ.get("POL_TM_GAMEEA_EN", "1"), 0)
    except ValueError:
        return 1


#: `/EN=` REFUSALS -- the four codes the reserve scene has its own words for.
#: Project Crystal Server answers `@GameEN=` with these (its Table.cs):
#:
#:     -32872  all reservations were cancelled
#:     -32871  cannot make a reservation
#:     -32870  this table is full
#:     -32869  already registered
#:
#: and TM.dll bears them out. The reserve scene stores the verdict at
#: `[scene+0x188]` (`0x8F96B`), clears the my-reservation global
#: 0x52461D0/D4/DC on any negative (`0x8FA56`/`0x8FA81`), sends -32871 to state
#: 0x136 and every other negative to state 0x12C, and state 0x12C (`0x8FE47`)
#: indexes `verdict + 0x8068` through a four-entry jump table (0x90344):
#: -32872 -> message 0x65, -32870 -> 0x64, -32869 -> 0x6D, anything else 0x68.
#: So each code has its own dialog; the text behind 0x64/0x6D comes from
#: Crystal's labels and has not been read off a screen here.
#:
#: We send two, and only where our seat list can tell:
#:   * FULL (-32870): THREE other members already hold seats here. A match is
#:     at most three players: the deal table has board sizes for 2 and 3 only
#:     (`TILES_BY_PLAYERS`, read off the image), and Crystal's table caps at 3
#:     too. `POL_TM_TABLE_MAX_SEATS` moves the number; the row's own capacity
#:     (`row[4]`, 8 in every fixture row) still bounds it. The client pre-checks
#:     a count of its own before sending (`0x8F8E9`, limit not decoded), so this
#:     is mostly the race guard for two joiners on one last seat.
#:     `POL_TM_GAMEEA_REFUSE=0` turns both refusals off.
#:   * ALREADY REGISTERED (-32869): the member already holds a seat at THIS
#:     table. WARNING: DEFAULT OFF (`POL_TM_GAMEEA_REFUSE_DUP=1` to enable). The reserve
#:     scene deliberately lets a player re-send `@GameEN=` for a tile that reads
#:     "reserved by me" (display codes 1/5 at `0x8F8BD` -> `[scene+0x198]=1`),
#:     and our seats outlive the client's own my-table global (scene teardown
#:     clears it). Refusing there would
#:     strand a player on their own table, which the current re-seat does not.
GAMEEA_TABLE_FULL = -32870
GAMEEA_ALREADY_REGISTERED = -32869


def _gameea_refusal(member_id, peer_nick):
    """A negative `/EN=` for this `@GameEN=`, or None to seat as before.

    Reads the seat list and the table row only; changes nothing. Anything it
    cannot place (no room, no table, no row) is None, so an unknown case keeps
    today's behaviour.
    """
    if member_id is None or common._env_int("POL_TM_GAMEEA_REFUSE", 1) == 0:
        return None
    try:
        import tmroom
        chan = tmroom.room_of(member_id)
        if not chan:
            return None
        index, _why = peers.table_index_for_peer(peer_nick, tmroom.room_peer(chan),
                                           chan=chan)
        if index is None:
            return None
        seats = seating._seats_of(chan).get(index) or []
        mine = any(m == member_id for m, _i in seats)
        if mine:
            if common._env_int("POL_TM_GAMEEA_REFUSE_DUP", 0):
                return GAMEEA_ALREADY_REGISTERED
            return None
        row, _authored = tablerow._table_rows(tmroom, chan, index)
        cap = common._env_int("POL_TM_TABLE_MAX_SEATS", 3)
        row_cap = int(row[4] or 0) if row and len(row) > 4 else 0
        if row_cap:
            cap = min(cap, row_cap) if cap > 0 else row_cap
        if cap > 0 and len(seats) >= cap:
            return GAMEEA_TABLE_FULL
    except Exception as exc:
        common._say("tm:   @GameEN= refusal check skipped (%r)" % (exc,))
    return None


def _gameml_enabled():
    return os.environ.get("POL_TM_GAMEML", "1") != "0"


def _gameml_rows():
    """How many members to claim are at the table. 0 -- an empty table -- is the
    truth at the table this was measured at, and the only answer that invents
    nothing. Set 1 to serve a probe row and find out which of `/Po=`, `/Lv=` and
    `/Rk=` is VS. Rating, Title, Card Level or Guild on the Member Info panel."""
    try:
        return max(0, min(16, int(os.environ.get("POL_TM_GAMEML_ROWS", "0"))))
    except ValueError:
        return 0


def _is_seated_here(member_id, peer_nick=None):
    """Is `member_id` seated at the table this `@GameM=` is about? Only a seated
    requester may have their reservation slot (+0x108) filled -- a looker's must
    stay empty or their Reserve button greys. See the @GameM= handler.

    WARNING: THIS GATE FEEDS +0x108 (the 'Table Members' sidebar AND the Start-Game
    gate), and BOTH feeders die when it wrongly returns False for a seated
    member (the @Pong re-feed at `_reservation_reply` and the @GameM= reply
    share it). Measured live 2026-08-21 with tm_store_probe: +0x10c=2 (View pane
    fine), +0x41c=1 (armed), store held ONLY @GameQA (0x41/6) and NO 0x41/0xC --
    so the reservation reply was never SENT, i.e. this returned False. The
    three-outcome log below is the observable: it prints member_id, the resolved
    room, and the actual seated ids on every FALSE, so an id-space or
    room-resolution mismatch is visible instead of silent (`POL_TM_SEATED_LOG=0`
    to quiet it)."""
    if member_id is None:
        return False
    reason = "room_of=None"
    try:
        import tmroom
        chan = tmroom.room_of(member_id)
        if chan:
            for _idx, seats in (tmroom.seats(chan) or {}).items():
                if any(int(m) == int(member_id) for m, _i in seats):
                    return True
            reason = ("not in seats: room=%s seated ids=%s"
                      % (chan, [int(m) for _i, s in (tmroom.seats(chan) or {})
                                .items() for m, _x in s]))
    except Exception as exc:
        reason = "raised %r" % (exc,)
    if os.environ.get("POL_TM_SEATED_LOG", "1") == "1":
        # Rate-limited so the @Pong re-feed (~1/2s) does not flood -- one line
        # per (member, reason) until the reason changes.
        key = (int(member_id) if member_id is not None else None, reason[:40])
        if _SEATED_LOG_SEEN.get(key) != reason:
            _SEATED_LOG_SEEN[key] = reason
            common._say("tm: _is_seated_here(member %s) = FALSE -- %s. No reservation "
                 "reply (+0x108) will be sent for this member." % (member_id, reason))
    return False


_SEATED_LOG_SEEN = {}


def _gameml_live_rows(member_id, peer_nick=None):
    """`[(member_id, name_bytes), ...]` -- the SEATED members of ONE TABLE.

    WARNING: THIS LISTED THE WHOLE ROOM UNTIL 2026-08-21 ~13:38Z, and a tester's
    report is what it did on screen: "when player two joins the room, it
    immediately pseudo-adds them to table 1... they appear in the table members
    list and it doesn't let them place a reservation." The pane is *View TABLE
    Members*; a room-joiner is not a table member. The table is resolved from
    the PEER the `@GameM=` was addressed to (the same `table_index_for_peer`
    every table-scoped command uses), falling back to the asker's own seated
    table; an empty seat list answers `/Num=0`, which is the truth."""
    if member_id is None or os.environ.get("POL_TM_GAMEML_LIVE", "1") != "1":
        return None
    try:
        import tmroom
    except ImportError:
        return None
    try:
        chan = tmroom.room_of(member_id)
        if not chan:
            return None
        seats_map = tmroom.seats(chan) or {}
        index = None
        if peer_nick:
            index, _why = peers.table_index_for_peer(
                peer_nick, tmroom.room_peer(chan), chan=chan)
        if index is None:
            for i, s in seats_map.items():
                if any(int(m) == int(member_id) for m, _x in s):
                    index = i
                    break
        names = {}
        for mid, vals in tmroom.members(chan):
            # v10 is name[15] + the 16-letter block; the name is the first 15,
            # space-padded, and `0x66A39` reads its length from the block's
            # first letter. Here we only need the text.
            names[int(mid)] = ((vals[10] or "")[:15].strip()
                               .encode("latin1", "replace"))
        out = []
        for mid, _ident in (seats_map.get(index) or []):
            nm = names.get(int(mid))
            if not nm:
                # A seat can outlive its member's ROOM presence (the silent-
                # seat expiry frees seats for members who crashed out), and a
                # row served nameless is the "Since  has cancelled their
                # reservation" popup with the blank name (Lobby.BIN 50 --
                # live testing, 2026-08-22). The durable name store keeps what
                # note_name taught even after they leave the channel.
                try:
                    nm = (tmroom._live_names().get(str(mid)) or "")[:15] \
                        .strip().encode("latin1", "replace")
                except Exception:
                    nm = b""
            out.append((int(mid), nm or b"?"))
        return out
    except Exception as exc:
        common._say("tm: @GameML live roster unavailable (%r) -- fixed count" % (exc,))
        return None


def _gameml_body(cmd, member_id=None, peer_nick=None):
    """`@GameML=` for a `@GameM=` request. COLUMNAR, PIPE-delimited, HEX names.

    WARNING: THE FORMAT WAS WRONG SINCE THE FIRST SESSION, AND IT IS THE WHOLE OF THE
    EMPTY "View Table Members" PANE (RE + minidump `pol (29).dmp`, 2026-08-21).
    The reply was ACCEPTED and drained
    into the members slot every time -- what was wrong was the ROW LAYOUT:

      * The client's field extractors (0xAAD60 string, 0xAB470 u64, 0xAB080 int)
        are COLUMNAR, not grouped. Each key appears ONCE and its value is a
        `|`-separated list, one element per member; the parse loop's index picks
        the i-th `|` element -- the exact shape of `@Decks=/D=220|55|0|...`. The
        old "repeat `/MLID=/MLNN=...` per row, read by occurrence index" made the
        client, for member index >= 1, find no `|`, fall back to element 0, and
        return every row as member 0.
      * `/MLNN=` and `/HN=` are UPPERCASE HEX of the name bytes. 0xAAD60 reads
        the value TWO hex chars at a time into one byte (`hi<<4|lo`), so plaintext
        `"Fox"` decoded to the single byte `0xC0+0x2A = 0xEA` -- the sentinel the
        dump showed for every name. The client's own `@GameEN=/HN=4C61...7432`
        (an 11-character name) already sends names this way.

    Each column is length `Num`, same member order, so MLID[i]/MLNN[i]/Po[i]/...
    line up by index. `/ID=` still echoes the request's `/ID=` (the recipient's
    local POL-ID, checked at 0x838E3) and `/Num=` is decimal.
    """
    m = re.search(rb"/ID=([0-9A-Fa-f]+)", cmd)
    if not m:
        common._say("tm: @GameM= with no /ID= -- cannot echo it, staying silent: %r" % cmd)
        return None
    ident = m.group(1)

    def _hexname(b):
        # UPPERCASE hex, 2 chars/byte -- 0xAAD60 hex-decodes; lowercase a-f and
        # an odd nibble both decode wrong, so bytes.hex().upper() is the contract.
        return b.hex().upper().encode("ascii")

    rows = _gameml_live_rows(member_id, peer_nick)
    if rows is not None:
        import tmroom as _tr2
        mlids, names = [], []
        for mid, name in rows:
            # WARNING: `/MLID=` IS A POL-ID, NOT OUR DATABASE ID (three-ids banner):
            # a db id like 0000000000000003 renders an empty pane. pol_id_of
            # returns a HEX STRING; base-10 int() on it hung the pane once
            # @Init taught a POL-ID, so parse it base 16.
            pid = None
            try:
                pid = _tr2.pol_id_of(mid)
            except Exception:
                pid = None
            if pid:
                try:
                    mlid = int(str(pid).replace("0x", "").replace("0X", ""), 16)
                except ValueError:
                    mlid = int(mid)
            else:
                common._say("tm:   @GameML row for member %s has NO recorded POL-ID "
                     "-- serving the db id, which the client may drop" % mid)
                mlid = int(mid)
            mlids.append(b"%016X" % (mlid & 0xFFFFFFFFFFFFFFFF))
            names.append(_hexname(name))
        n = len(rows)
        zeros = b"|".join([b"0"] * n)
        # `/Po=` `/Lv=` `/Rk=` are served 0 (VS.Rating/Title/CardLevel HINTS,
        # so a wrong column shows a 0, not an invented number).
        return (b"@GameML=/ID=%s/Num=%d/MLID=%s/MLNN=%s"
                b"/Po=%s/Lv=%s/Rk=%s/HN=%s"
                % (ident, n, b"|".join(mlids), b"|".join(names),
                   zeros, zeros, zeros, b"|".join(names)))

    # FALLBACK PROBE (no live roster, POL_TM_GAMEML_LIVE=0): same columnar shape,
    # names hex-encoded so the pane can render the probe at all.
    n = _gameml_rows()
    rng = range(n)
    return (b"@GameML=/ID=%s/Num=%d/MLID=%s/MLNN=%s/Po=%s/Lv=%s/Rk=%s/HN=%s"
            % (ident, n,
               b"|".join(b"%016X" % (0x2000000010 + i) for i in rng),
               b"|".join(_hexname(b"MLNN%d" % i) for i in rng),
               b"|".join(b"%d" % (10 + i) for i in rng),
               b"|".join(b"%d" % (20 + i) for i in rng),
               b"|".join(b"%d" % (30 + i) for i in rng),
               b"|".join(_hexname(b"HN%d" % i) for i in rng)))


def _push_reservation_list(tmroom, chan, index, who):
    """Push the reserved-player list (msgid 0xC @GameML) to each seated member.

    WARNING: THE OWNER'S OWN TILE LOCKS ON AN EMPTY RESERVATION SLOT, AND SO DOES
    START GAME -- ONE mechanism (disasm 2026-08-21). The owner's state-1 tile
    arm at `0x672E4` reads the reservation count `[0x51def2c]` (mirror of
    `screenobj+0x108`): 0 -> **display code 2 = LOCKED**, 1 -> 0x11, >=2 ->
    (n<<4)|5 (clickable, with Start Game enabled when `0x51defa0`==0). The gate
    `0x5C0D0` reads the same `+0x108`. That slot fills ONLY from a msgid-0xC
    `@GameML` (parser `0x83B80`); the members pane (msgid 4) fills a different
    slot. The client arms the reservation request (`+0x41c`) when the owner
    opens the table menu, but that request is not the plain `@GameM=` we answer,
    so `+0x108` stayed 0 -- and a 2-seated owner's tile then renders code 2, so
    the owner cannot re-open the menu to retrigger it. Measured live: dump with
    `+0x108`=0, `+0x41c` ARMED, tile code 2, both clients frozen.

    `+0x41c` is armed continuously while at the table, so a msgid-0xC reply
    sitting in the queue is consumed on the client's next copy-loop frame and
    `+0x108` fills WITHOUT any click -- breaking the deadlock. Pushed on every
    seat change (the same unpinned `_queue_push` path `_push_table_info` uses,
    proven to reach seated/idle members). `POL_TM_RESV_PUSH=0` disables.
    """
    if os.environ.get("POL_TM_RESV_PUSH", "1") != "1":
        return
    if not who:
        return
    # The reservation parser has NO /ID gate (0x83B80), so one body serves every
    # recipient; the /ID is cosmetic. Build it for the table's seated list.
    owner = next((m for m, _i in who), None)
    if owner is None:
        return
    try:
        pid = tmroom.pol_id_of(owner)
        idhex = (str(pid) if pid else ("%X" % owner)).replace(
            "0x", "").replace("0X", "").encode("ascii")
        body = _gameml_body(b"@GameM=/ID=" + idhex, member_id=owner)
    except Exception as exc:
        common._say("tm:   reservation list not built (%r)" % (exc,))
        return
    if not body:
        return
    line = protocol.encode_code(protocol._resv_gameml_code()) + body
    common._say("tm:   reservation list -> %d seated member(s) at table %d (msgid %s, "
         "fills +0x108: unlocks the owner tile AND Start Game)"
         % (len(who), index,
            "0xC" if protocol._resv_gameml_code() == protocol.MSG_GAMEML_RESV else "4"))
    for mid, _ident in who:
        pushqueue._queue_push(mid, line, "reservation list for table %d" % index)


def _reservation_reply(member_id):
    """The msgid-0xC `@GameML` reservation line for `member_id`'s seated table,
    or None -- what feeds the client's reservation list (+0x108/+0x110), the
    "table members" sidebar, and the Start-Game gate. RE 2026-08-21: the client
    polls (code 0x41, msgid 0xC) EVERY FRAME while +0x41c is armed, and its
    message store holds only a few entries, so a one-shot reply is evicted
    before the reservation branch (0xA940C -> 0x83B80) reads it. Re-emitting it
    keeps a fresh copy present. Only for SEATED members -- a looker's slot must
    stay empty or their Reserve greys."""
    if os.environ.get("POL_TM_RESV_PUSH", "1") != "1":
        return None
    # WARNING: NEVER INTO A RUNNING MATCH. The re-feed exists for the TABLE screen
    # (+0x41c armed); the match scene never reads (0x41, 4), and the client's
    # receive store is ONE GLOBAL RING -- so during a match every re-feed is a
    # ring slot that never frees. Measured live 2026-08-22T16:16Z (FRAMEWORK
    # probe, mid-COM-game): store_count parked at 24, the ring full of unread
    # re-feeds, and the COM's turn-7 @PutCard VANISHED ON ARRIVAL -- the
    # tester's "com seems to have stopped", ~3 minutes into a slow game at
    # one re-feed per @Pong. Fast games finished before the ring filled,
    # which is why four earlier full games never showed it.
    # The gates are the two precise "a match is RUNNING" signals -- NOT
    # `_match_of` (its roster snapshot includes pre-match seating, and
    # suppressing the re-feed at the table would resurrect the +0x108 bug):
    #   * a VS. COM match key exists (cleared on @GameExit=), or
    #   * this member is in `_MATCH_STARTED` for their table (set on
    #     @GameReady=, reset on CONFIRM, and now also cleared on @GameExit=).
    _k = pushqueue._push_key(member_id)
    if (None, _k) in boardrules._MATCH_TURN:
        return None
    _mc, _mi, _ms = matchmaking._match_of(member_id)
    if _mi is not None and _k in (matchmaking._MATCH_STARTED.get((_mc, _mi)) or set()):
        return None
    if not _is_seated_here(member_id):
        return None
    try:
        import tmroom
        pid = tmroom.pol_id_of(member_id)
        idhex = (str(pid) if pid else ("%X" % member_id)).replace(
            "0x", "").replace("0X", "").encode("ascii")
        body = _gameml_body(b"@GameM=/ID=" + idhex, member_id=member_id)
    except Exception:
        return None
    if not body:
        return None
    # OBSERVABLE (2026-08-21): the @Pong re-feed is the ONLY thing keeping a
    # msgid-0xC resident between seat changes, and it was SILENT -- so live, "the
    # reply stopped" and "the reply is firing but not landing" were
    # indistinguishable (the shim [resvdiag] saw no 0x41/0xC in the store while
    # armed). Log each fire, rate-limited per member (@Pong is ~15s, so a few
    # lines/min). `POL_TM_RESV_LOG=0` quiets it.
    if os.environ.get("POL_TM_RESV_LOG", "1") == "1":
        now = time.time()
        key = int(member_id)
        if now - _RESV_REPLY_LOG.get(key, 0.0) >= 8.0:
            _RESV_REPLY_LOG[key] = now
            common._say("tm: @Pong reservation re-feed -> member %s (msgid %s @GameML, "
                 "%d-byte body) -- meant to fill +0x108 (kick poll cannot eat "
                 "msgid 4; +0x414 not armed on a @Pong frame)"
                 % (member_id,
                    "0xC" if protocol._resv_gameml_code() == protocol.MSG_GAMEML_RESV else "4",
                    len(body)))
    return protocol.encode_code(protocol._resv_gameml_code()) + body


_RESV_REPLY_LOG = {}
